"""Allocation planning, normalization and live registration progress queries."""

from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from bise_normalize.config import load as normal_config
from bise_normalize.engine import born_on
from youthgames_shared.catalog import age_eligibility, exact, ensure_fresh as catalog_load, signature
from youthgames_register.config import load as registration_config, resolved
from .store import assign, opened, now


def load(path):
    path = Path(path).resolve()
    c = json.loads(path.read_text())
    required = {'schema_version','scheme','year','course','exam_type','source_institution','expected_records',
                'category','division','district','tehsil','uc_numbers','administrative_allocation_confirmed','paths'}
    if not required.issubset(c) or set(c)-required-{'source_pool','subcategory'} or c['schema_version'] != 1:
        raise ValueError('Use the complete schema-1 allocation profile')
    if 'source_pool' in c:
        pool=c['source_pool']
        if not isinstance(pool,dict) or set(pool)!={'batch','group'} or any(not isinstance(v,str) or not v.strip() for v in pool.values()):
            raise ValueError('Allocation source_pool requires explicit batch and group')
    if 'subcategory' in c and c['subcategory']!='Private':raise ValueError('Explicit allocated subcategory currently supports Private only')
    if (c['course'],c['exam_type']) != ('SSC','1') or type(c['year']) is not int:
        raise ValueError('Allocation currently supports explicit SSC Part-I cohorts')
    nums = c['uc_numbers']
    if not isinstance(nums,list) or not nums or any(type(n) is not int or n<1 for n in nums) or len(nums)!=len(set(nums)):
        raise ValueError('UC numbers must be unique positive integers')
    if not c['administrative_allocation_confirmed']:
        raise ValueError('Operator must confirm administrative allocation is allowed')
    keys = {'source_database','allocation_database','registration_database','catalog','normalization_base',
            'normalization_profile','registration_settings','registration_profile','output'}
    if set(c['paths']) != keys:
        raise ValueError('Allocation paths are incomplete')
    return c


def paths(config, profile):
    return {k:(Path(profile).resolve().parent / value).resolve() for k,value in config['paths'].items()}


def targets(c, catalog):
    data = catalog['data']
    division = exact(data['divisions'],c['division'])
    district = exact(data['districts_by_division'][str(division['id'])],c['district'])
    tehsil = exact(data['tehsils_by_district'][str(district['id'])],c['tehsil'])
    result=[]
    for number in c['uc_numbers']:
        choices=[u for u in data['ucs_by_district'][str(district['id'])]
                 if re.match(r'^UC No\.\s*'+str(number)+r'\s*,',u['name'])]
        if len(choices)!=1:
            raise ValueError(f'Official UC {number} missing or ambiguous')
        uc=choices[0]
        result.append({'number':number,'Division':division['name'],'division_id':str(division['id']),
                       'District':district['name'],'district_id':str(district['id']),
                       'Tehsil':tehsil['name'],'tehsil_id':str(tehsil['id']),'UC':uc['name'],'uc_id':str(uc['id'])})
    return result


def cohort(c,p,catalog):
    if c.get('source_pool'):
        with opened(p['allocation_database'],readonly=True) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT source_record FROM private_pool_members WHERE batch=? AND group_name=? ORDER BY roll',
                (c['source_pool']['batch'],c['source_pool']['group']))]
    db=sqlite3.connect(p['source_database'].as_uri()+'?mode=ro',uri=True)
    result=[]
    try:
        db.execute('BEGIN')
        for (raw,) in db.execute("SELECT result_json FROM jobs WHERE year=? AND course=? AND exam_type=? AND state='verified' AND upper(trim(json_extract(result_json,'$.institution')))=? ORDER BY roll",
                                (c['year'],c['course'],c['exam_type'],c['source_institution'].upper().strip())):
            row=json.loads(raw)
            try:dob=born_on(row.get('date_of_birth',''),'%d/%m/%Y').isoformat()
            except (ValueError,TypeError):continue
            if age_eligibility({'Category':c['category'],'DOB':dob},catalog)['state']=='eligible':
                result.append(row)
    finally:db.close()
    return result


def apply(profile):
    c=load(profile);p=paths(c,profile);catalog=catalog_load(p['catalog'])
    mapped=targets(c,catalog)
    records=cohort(c,p,catalog)
    with opened(p['allocation_database']) as db:
        result=assign(db,c,mapped,records,catalog['content_sha256'])
    result.update({'current_source_eligible':len(records),'database':str(p['allocation_database']),
                   'allocation_basis':'operator_confirmed_administrative_allocation','applicant_posts':0})
    return result


def write_profile(path,c):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    text=json.dumps(c,indent=2,ensure_ascii=False)+'\n'
    if path.exists():
        if json.loads(path.read_text())!=c:
            raise ValueError('Generated profile has operator edits; preserve them and use a new profile name')
        return
    handle,name=tempfile.mkstemp(prefix='.allocation-profile-',suffix='.json',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(handle,'w') as stream:
            stream.write(text);stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,path)  # Atomic publication; never overwrite another writer/operator.
        except FileExistsError:
            if json.loads(path.read_text())!=c:
                raise ValueError('Generated profile has operator edits; preserve them and use a new profile name')
    finally:temporary.unlink(missing_ok=True)


def normalization_settings(profile, participation=None):
    c=load(profile);p=paths(c,profile)
    base=normal_config(p['normalization_base'])
    # Rebase all path settings before changing the source/output.
    for section,key in [('target','template'),('validation','catalog'),('portal','normalization_database'),('portal','registration_database')]:
        if base[section][key]:base[section][key]=os.path.relpath(resolved(p['normalization_base'],base[section][key]),p['normalization_profile'].parent)
    if base['enrichment']['sources'] or base['enrichment']['overrides']:
        raise ValueError('Allocation profile requires a base without unreviewed enrichment paths')
    base['profile']=c['scheme']
    base['source'].update(path=os.path.relpath(p['allocation_database'],p['normalization_profile'].parent),format='sqlite',allocation_scheme=c['scheme'])
    base['source']['exam'].update(board='BISE Lahore',year=c['year'],course=c['course'],part='I',session='Annual')
    base['selection'].update(institutions=[] if c.get('source_pool') else [c['source_institution']],rolls=[],row_ranges=[],roll_bounds=None,limit=None)
    base['defaults'].update(Category=c['category'],Division=c['division'],District=c['district'],Tehsil=c['tehsil'],UC=None)
    if c.get('subcategory')=='Private':
        base['defaults']['SubCategory']='Private'
        base['mapping']['Institute']='Institution / District'
    base['output'].update(folder=os.path.relpath(p['output'],p['normalization_profile'].parent),write_excel=False)
    if resolved(p['normalization_profile'],base['portal']['registration_database'])!=p['registration_database']:
        raise ValueError('Allocation and normalization must share the registration database')
    from .participation import current
    override=participation if participation is not None else current(p['allocation_database'],c['scheme'])
    if override:
        base['defaults']['SubCategory']=override['subcategory']
        base['rules'].extend(override['preserve_rules'])
    return base


def normalization_profile(profile):
    c=load(profile);p=paths(c,profile)
    write_profile(p['normalization_profile'],normalization_settings(profile))
    return p['normalization_profile']


def normalize(profile):
    from bise_normalize.app import execute
    c=load(profile);p=paths(c,profile);catalog_load(p['catalog'])
    return execute(normalization_profile(profile))


def progress(profile,roll=None,include_jobs=False):
    c=load(profile);p=paths(c,profile)
    settings=registration_config(p['registration_settings'])
    with opened(p['allocation_database'],readonly=True) as db:
        db.execute('ATTACH DATABASE ? AS registrations',(p['registration_database'].as_uri()+'?mode=ro',))
        normalization_join = ''
        normalization_fields = ',NULL AS normalization_eligible,NULL AS normalization_issues'
        args=[settings['portal'].split('#')[0].rstrip('/'),settings['campaign']]
        if p['normalization_profile'].exists():
            normal=normal_config(p['normalization_profile'])
            ledger=resolved(p['normalization_profile'],normal['portal']['normalization_database'])
            if ledger.exists():
                db.execute('ATTACH DATABASE ? AS normalization',(ledger.as_uri()+'?mode=ro',))
                # Evaluations are keyed by (profile, exam-fingerprint:roll).
                # A profile-only index can scan all students once per assignment.
                normalization_join = " LEFT JOIN normalization.evaluations e ON e.profile=? AND e.person=(? || a.roll) AND e.bform=a.applicant_key"
                normalization_fields = ",json_extract(e.payload,'$.portal.eligible') AS normalization_eligible,json_extract(e.payload,'$.issues') AS normalization_issues"
                args.extend([c['scheme'],signature(normal['source']['exam'])+':'])
        db.execute('BEGIN')
        where=' AND a.roll=?' if roll else ''
        args += [c['scheme']]+([roll] if roll else [])
        rows=db.execute('''SELECT a.roll,a.uc_number,t.tehsil,t.uc,t.uc_id,j.id AS registration_job,
                coalesce(j.state,'not_prepared') AS registration_state,j.tracking,
                json_extract(j.payload,'$.data.Tehsil') AS submitted_tehsil,
                json_extract(j.payload,'$.data.UC') AS submitted_uc,j.receipt'''+normalization_fields+'''
            FROM allocations a JOIN allocation_targets t ON a.scheme=t.scheme AND a.uc_number=t.uc_number
            LEFT JOIN registrations.jobs j ON j.applicant_key=a.applicant_key AND j.portal=? AND j.campaign=?
            '''+normalization_join+' WHERE a.scheme=?'+where+' ORDER BY a.uc_number,a.roll',args).fetchall()
        from youthgames_register.progress import bucket
        summaries={};buckets=Counter();progress_jobs={}
        for row in rows:
            group=bucket(row['registration_state'],row['normalization_eligible']==1)
            buckets[group]+=1
            if include_jobs and row['registration_job'] is not None:
                key=str(row['registration_job'])
                progress_jobs.setdefault(key,[]).append({'bucket':group,'eligible':row['normalization_eligible']==1,'rows':1})
            out=summaries.setdefault(row['uc_number'],{'uc_number':row['uc_number'],'uc':row['uc'],'uc_id':row['uc_id'],
                'tehsil':row['tehsil'],'assigned':0,'registered':0,'registered_in_assigned_uc':0,'registered_elsewhere':0,'tracking_ids_saved':0,
                'normalization_blocked':0,'ready_remaining':0,'states':Counter()})
            out['assigned']+=1;out['states'][row['registration_state']]+=1
            out['normalization_blocked'] += row['normalization_eligible'] == 0
            out['ready_remaining'] += row['normalization_eligible'] == 1 and row['registration_state'] in ('not_prepared','ready')
            if row['registration_state']=='registered':
                out['registered']+=1;out['tracking_ids_saved']+=bool(row['tracking'])
                out['registered_in_assigned_uc' if row['submitted_uc']==row['uc'] and row['submitted_tehsil']==row['tehsil'] else 'registered_elsewhere']+=1
        result={'scheme':c['scheme'],'assigned':len(rows),'registered':sum(v['registered'] for v in summaries.values()),
                'tracking_ids_saved':sum(v['tracking_ids_saved'] for v in summaries.values()),'ucs':list(summaries.values()),
                'ready_remaining':sum(v['ready_remaining'] for v in summaries.values()),
                'normalization_blocked':sum(v['normalization_blocked'] for v in summaries.values()),
                'completed_total':buckets['completed'],'remaining_total':buckets['remaining'],'held_total':buckets['held'],
                'note':'Current registration database joined by portal/campaign/BForm; tracking and receipts remain authoritative there.'}
        if include_jobs:result['progress_jobs']=progress_jobs
        if roll:result['records']=[dict(r) for r in rows]
        return result


def batch_progress(profile):
    result=progress(profile,include_jobs=True)
    return {'total':result['assigned'],'completed':result['completed_total'],
        'remaining':result['remaining_total'],'held':result['held_total'],'jobs':result['progress_jobs']}


def sync(profile):
    from youthgames_register.handoff import sync as handoff
    c=load(profile);p=paths(c,profile);catalog_load(p['catalog']);normalization_profile(profile)
    if not p['registration_profile'].exists():
        settings=deepcopy(registration_config(p['registration_settings']))
        for section,key in [('photos','shared_file'),('state','database')]:
            settings[section][key]=os.path.relpath(resolved(p['registration_settings'],settings[section][key]),p['registration_profile'].parent)
        settings['profile']=c['scheme']
        settings['state'].update(output=os.path.relpath(p['output'].parent.parent/'registration_output'/c['scheme'],p['registration_profile'].parent),write_reports=False)
        settings['browser'].update(workers=18,headless=False)
        settings['selection']['rolls']=[]
        if c.get('source_pool'):
            target=json.loads(p['normalization_profile'].read_text())['defaults']
            settings['selection'].update(category=target['Category'],subcategory=target['SubCategory'])
            from .store import opened
            with opened(p['allocation_database']) as allocation_db:
                sports=[r[0] for r in allocation_db.execute('SELECT DISTINCT sport FROM sport_assignments WHERE scheme=? ORDER BY sport',(c['scheme'],))]
            if not sports:raise ValueError('Private sports assignments are missing')
            settings['selection']['sport']=sports
            settings['confirmation']['fields']=list(dict.fromkeys(settings['confirmation']['fields']+['Category','SubCategory','Institute','Sport']))
        settings['confirmation']['fields'] = list(dict.fromkeys(settings['confirmation']['fields'] + ['Division','District','Tehsil','UC']))
        write_profile(p['registration_profile'],settings)
    return handoff(p['registration_profile'],p['normalization_profile'],p['registration_settings'])


def select_pilots(profile, transport='browser'):
    """Pin one never-uploaded ready identity per UC; reruns retain this selection."""
    tables = {'browser':'allocation_pilots', 'http':'allocation_http_pilots'}
    if transport not in tables:
        raise ValueError('Unsupported pilot transport')
    pilot_table = tables[transport]
    c=load(profile);p=paths(c,profile);normal=normal_config(p['normalization_profile'])
    ledger=resolved(p['normalization_profile'],normal['portal']['normalization_database'])
    with opened(p['allocation_database']) as db:
        db.execute('ATTACH DATABASE ? AS normalization',(ledger.as_uri()+'?mode=ro',))
        db.execute('ATTACH DATABASE ? AS registrations',(p['registration_database'].as_uri()+'?mode=ro',))
        settings=registration_config(p['registration_settings'])
        portal=settings['portal'].split('#')[0].rstrip('/')
        with db:
            for target in db.execute('SELECT uc_number FROM allocation_targets WHERE scheme=? ORDER BY uc_number',(c['scheme'],)).fetchall():
                number=target[0]
                if db.execute(f'SELECT 1 FROM {pilot_table} WHERE scheme=? AND uc_number=?',(c['scheme'],number)).fetchone():continue
                row=db.execute('''SELECT a.roll,a.applicant_key FROM allocations a
                    JOIN normalization.evaluations e ON e.profile=? AND e.person=(? || a.roll)
                      AND e.bform=a.applicant_key AND e.ready=1
                    LEFT JOIN registrations.jobs j ON j.applicant_key=a.applicant_key AND j.portal=? AND j.campaign=?
                    WHERE a.scheme=? AND a.uc_number=? AND (j.id IS NULL OR j.state='ready')
                      AND NOT EXISTS(SELECT 1 FROM allocation_pilots z WHERE z.scheme=a.scheme AND z.applicant_key=a.applicant_key)
                      AND NOT EXISTS(SELECT 1 FROM allocation_http_pilots z WHERE z.scheme=a.scheme AND z.applicant_key=a.applicant_key)
                    ORDER BY a.roll LIMIT 1''',(c['scheme'],signature(normal['source']['exam'])+':',portal,settings['campaign'],c['scheme'],number)).fetchone()
                if row is None:raise ValueError(f'UC {number} has no new registration-ready applicant; no pilot was dispatched')
                db.execute(f'INSERT INTO {pilot_table} VALUES(?,?,?,?,?)',(c['scheme'],number,row['roll'],row['applicant_key'],now()))
        return [dict(r) for r in db.execute(f'SELECT uc_number,roll FROM {pilot_table} WHERE scheme=? ORDER BY uc_number',(c['scheme'],))]


async def pilot(profile,workers=18,headless=False):
    from youthgames_register.app import run
    from youthgames_register.store import check_receipt
    c=load(profile);p=paths(c,profile)
    recovered = recover_pilot_loads(profile)
    sync(profile)
    pilots=select_pilots(profile)
    result=await run(p['registration_profile'],'submit',limit=0,rolls=[r['roll'] for r in pilots],workers=workers,headless=headless,captcha_mode='auto')
    report=progress(profile)
    checks=[]
    for selected in pilots:
        row=progress(profile,selected['roll'])['records'][0]
        checks.append({'uc_number':selected['uc_number'],'roll':selected['roll'],'registration_job':row['registration_job'],
            'state':row['registration_state'],'tracking_saved':bool(row['tracking']),
            'assigned_uc_matches_submitted':row['uc']==row['submitted_uc'] and row['tehsil']==row['submitted_tehsil'],
            'receipt_valid':check_receipt(row['receipt']) if row['registration_state']=='registered' else False})
    report['pilot_checks']=checks
    report['pilot_passed']=len(checks)==len(c['uc_numbers']) and all(x['state']=='registered' and x['tracking_saved'] and x['assigned_uc_matches_submitted'] and x['receipt_valid'] for x in checks)
    report['attempted_now']=result['attempted_now']
    report['recovered_pre_submission_loads']=recovered
    registration = registration_config(p['registration_profile'])
    destination = resolved(p['registration_profile'],registration['state']['output'])
    destination.mkdir(parents=True,exist_ok=True)
    (destination/'UC Pilot Verification.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def recover_pilot_loads(profile):
    """Retry only proven pre-intent GET timeouts on the same pinned identities."""
    from youthgames_register.store import opened as registration_store, check_receipt
    c=load(profile);p=paths(c,profile)
    with opened(p['allocation_database'],readonly=True) as db:
        people=[r[0] for r in db.execute('SELECT applicant_key FROM allocation_pilots WHERE scheme=?',(c['scheme'],))]
    if not people:return []
    settings=registration_config(p['registration_settings'])
    recovered=[]
    with registration_store(p['registration_database']) as db, db:
        for key in people:
            row=db.execute('SELECT * FROM jobs WHERE applicant_key=? AND campaign=? AND portal=?',
                (key,settings['campaign'],settings['portal'].split('#')[0].rstrip('/'))).fetchone()
            if row is None or row['state']!='blocked_input' or not (row['detail'] or '').startswith('TimeoutError: Page.goto:'):
                continue
            attempt=db.execute('SELECT * FROM attempts WHERE job=? ORDER BY id DESC LIMIT 1',(row['id'],)).fetchone()
            if not attempt or attempt['intent_at'] or attempt['dispatched_at'] or attempt['outcome']!='blocked_input' or not check_receipt(row['receipt']):
                continue
            receipt=json.loads(Path(row['receipt']).read_text())
            if receipt.get('post_dispatched') is not False or receipt.get('payload_hash')!=row['payload_hash'] or receipt.get('attempt')!=attempt['id']:
                continue
            db.execute("UPDATE jobs SET state='ready',detail=?,updated_at=? WHERE id=?",
                (f"Retry page GET after attempt {attempt['id']}; verified no submission intent or dispatch",now(),row['id']))
            recovered.append(row['id'])
    return recovered
