"""Stable operator-approved sport allocation for never-dispatched applicants."""
from collections import Counter,defaultdict
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from . import app
from .store import opened as allocation_store,now
from youthgames_register.config import load as registration_config,resolved
from youthgames_register.store import opened as registration_store
from youthgames_shared.catalog import ensure_fresh as load_catalog,signature


def quotas(total,weights,salt=''):
    """Integer largest remainders; no floating-point rounding drift."""
    positive={k:v for k,v in weights.items() if v>0}
    denominator=sum(positive.values())
    if total==0:return {k:0 for k in positive}
    if not positive or total<0:raise ValueError('Positive sport weights required')
    result={k:total*v//denominator for k,v in positive.items()}
    order=sorted(positive,key=lambda k:(-(total*positive[k]%denominator),signature([salt,k])))
    for key in order[:total-sum(result.values())]:result[key]+=1
    return result


def distribute(candidates,weights,seed):
    """Exact gender totals; spread their quotas proportionally across UCs."""
    groups=defaultdict(list)
    for row in candidates:groups[(row['gender'],row['uc_number'])].append(row)
    result=[]
    for gender in sorted({r['gender'] for r in candidates}):
        remaining=quotas(sum(len(v) for (g,_),v in groups.items() if g==gender),weights[gender],seed+gender)
        ucs=sorted((uc for g,uc in groups if g==gender),key=lambda uc:signature([seed,gender,uc]))
        for uc in ucs:
            group=groups[(gender,uc)]
            share=quotas(len(group),remaining,signature([seed,gender,uc]))
            slots=[(sport,index) for sport,count in share.items() for index in range(count)]
            slots.sort(key=lambda slot:signature([seed,gender,uc,*slot]))
            group=sorted(group,key=lambda r:signature([seed,r['applicant_key']]))
            for row,(sport,_) in zip(group,slots):result.append({**row,'sport':sport})
            for sport,count in share.items():remaining[sport]-=count
        if any(remaining.values()):raise ValueError('Sport quota conservation failed')
    return sorted(result,key=lambda r:r['roll'])


def policy(path,scheme,campaign,catalog):
    value=json.loads(Path(path).read_text())
    required={'schema_version','scheme','campaign','category','version','seed','weights'}
    if set(value)!=required or value['schema_version']!=1:raise ValueError('Use the schema-1 sport ratio profile')
    if value['scheme']!=scheme or value['campaign']!=campaign:raise ValueError('Sport policy differs from allocation/campaign')
    if not all(isinstance(value[k],str) and value[k].strip() for k in ('version','seed','category')):raise ValueError('Sport policy labels must be explicit')
    allowed=catalog['data']['participation']['sportsByCategoryName'].get(value['category'],[])
    weights=value['weights']
    if set(weights)!={'Male','Female'}:raise ValueError('Explicit Male and Female sport ratios required')
    for gender,ratios in weights.items():
        if not isinstance(ratios,dict) or not ratios or any(s not in allowed or type(n) is not int or not 0<=n<=100 for s,n in ratios.items()) or sum(ratios.values())!=100:
            raise ValueError('Sport percentages must total 100 for each gender, using allowed category sports')
        if gender=='Female' and any(n and 'boys only' in s.casefold() for s,n in ratios.items()):raise ValueError('Boys-only sport cannot be assigned to Female')
    return value


def bind_selection(path,sports):
    """Explicit allow-list: every submitted row still validates against live options."""
    path=Path(path).resolve();config=registration_config(path)
    config['selection']['sport']=sorted(sports)
    config['confirmation']['fields']=list(dict.fromkeys(config['confirmation']['fields']+['Sport']))
    if json.loads(path.read_text())==config:return
    handle,name=tempfile.mkstemp(prefix='.sport-selection-',suffix='.json',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(handle,'w') as stream:stream.write(json.dumps(config,indent=2,ensure_ascii=False)+'\n')
        registration_config(temporary)
        temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)


def apply(profile,ratios):
    config=app.load(profile);paths=app.paths(config,profile)
    normal=app.normal_config(paths['normalization_profile'])
    settings=registration_config(paths['registration_profile'])
    catalog=load_catalog(paths['catalog'])
    plan=policy(ratios,config['scheme'],settings['campaign'],catalog)
    if plan['category']!=config['category']:raise ValueError('Sport category differs from allocation')
    normal_db=resolved(paths['normalization_profile'],normal['portal']['normalization_database'])
    skipped=Counter();candidates=[]
    # Same exclusive lock as submissions: an active runner cannot race this
    # snapshot. Never write any registration job/attempt/receipt here.
    with registration_store(paths['registration_database']) as registrations,allocation_store(paths['allocation_database']) as allocations:
        known={r[0] for r in registrations.execute('SELECT applicant_key FROM registered_applicants WHERE portal=? AND campaign=?',
            (settings['portal'].split('#')[0].rstrip('/'),settings['campaign']))}
        jobs={r['applicant_key']:dict(r) for r in registrations.execute('SELECT id,applicant_key,state,payload_hash FROM jobs WHERE portal=? AND campaign=?',
            (settings['portal'].split('#')[0].rstrip('/'),settings['campaign']))}
        posted={r[0] for r in registrations.execute('SELECT DISTINCT job FROM attempts WHERE intent_at IS NOT NULL OR dispatched_at IS NOT NULL')}
        existing={r[0] for r in allocations.execute('SELECT applicant_key FROM sport_assignments WHERE scheme=?',(config['scheme'],))}
        rows=[dict(r) for r in allocations.execute('SELECT roll,applicant_key,uc_number FROM allocations WHERE scheme=? ORDER BY roll',(config['scheme'],))]
        repeats=Counter(r['applicant_key'] for r in rows)
        db=sqlite3.connect(normal_db.as_uri()+'?mode=ro',uri=True)
        try:
            evaluated={r[0]:(r[1],r[2],r[3]) for r in db.execute('''SELECT p.roll,e.bform,json_extract(e.payload,'$.data.Gender'),
                json_extract(e.payload,'$.portal.eligible') FROM evaluations e JOIN people p ON p.person=e.person WHERE e.profile=?''',(config['scheme'],))}
        finally:db.close()
        for row in rows:
            key=row['applicant_key'];job=jobs.get(key)
            if key in existing:skipped['already_assigned']+=1;continue
            if key in known or job and job['state'] in ('registered','already_registered_external'):skipped['registered']+=1;continue
            if job and (job['id'] in posted or job['state'] not in ('ready',)):skipped['posted_or_held']+=1;continue
            info=evaluated.get(row['roll'])
            if repeats[key]!=1 or not info or info[0]!=key or not info[2] or info[1] not in plan['weights']:
                skipped['duplicate_or_ineligible']+=1;continue
            row['gender']=info[1];row['prior_payload_hash']=job['payload_hash'] if job else None
            candidates.append(row)
        assignments=distribute(candidates,plan['weights'],plan['seed'])
        batch=signature({'policy':plan,'candidates':candidates})
        if assignments:
            timestamp=now()
            allocations.execute('INSERT INTO sport_batches VALUES (?,?,?,?,?)',(batch,config['scheme'],signature(plan),json.dumps(plan,ensure_ascii=False),timestamp))
            allocations.executemany('INSERT INTO sport_assignments VALUES (?,?,?,?,?,?,?,?)',
                ((config['scheme'],r['applicant_key'],r['roll'],r['gender'],r['uc_number'],r['sport'],batch,timestamp) for r in assignments))
        allowed={sport for ratios in plan['weights'].values() for sport,weight in ratios.items() if weight}
        allowed.update(r[0] for r in allocations.execute('SELECT DISTINCT sport FROM sport_assignments WHERE scheme=?',(config['scheme'],)))
        allowed.add(normal['defaults']['Sport'])  # Frozen prior POSTs retain their original sport.
        bind_selection(paths['registration_profile'],allowed)
        totals=[dict(r) for r in allocations.execute('SELECT gender,sport,count(*) AS records FROM sport_assignments WHERE scheme=? GROUP BY gender,sport ORDER BY gender,sport',(config['scheme'],))]
    return {'status':'assigned' if assignments else 'already_assigned','scheme':config['scheme'],'assigned_now':len(assignments),
        'assigned_total':sum(r['records'] for r in totals),'by_gender_sport':totals,'skipped':dict(skipped),'applicant_posts':0}
