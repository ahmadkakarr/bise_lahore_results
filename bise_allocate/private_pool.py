"""Freeze one disjoint named-school cohort; normalize either administrative group.

This command never opens the registration database for writing or submits forms.
School ownership is an operator-selected classification, not inferred proof.
"""
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from bise_normalize.engine import born_on, identifier
from bise_normalize.state import registration_snapshot
from youthgames_shared.catalog import age_eligibility, ensure_fresh, signature
from youthgames_shared.names import canonical_name
from . import app
from .sports import distribute, policy
from .store import opened, now

SELECTION_POLICY = 'named-lahore-private-candidates-v1'
PUBLIC = re.compile(r'\b(?:GOVT|GOVERNMENT)\b', re.I)
REVIEW = re.compile(r'\b(?:DIVISIONAL\s+PUBLIC|ARMY\s+PUBLIC|GARRISON|CANTONMENT\s+BOARD|CANTT\s+BOARD|WORKERS?\s+WELFARE|DAANISH|DANISH|F\.?\s*G\.?\s+(?:PUBLIC|MODEL|BOYS|GIRLS))\b', re.I)
LAHORE_SUFFIX = re.compile(r'\bLAHORE(?:\s+CANTT)?[\s.,;()]*$', re.I)


def load(path):
    plan=json.loads(Path(path).read_text())
    fields={'schema_version','batch','year','category','subcategory','division','district','campaign','seed',
            'administrative_allocation_confirmed','selection_policy','groups','paths'}
    if not isinstance(plan,dict) or set(plan)!=fields or type(plan['schema_version']) is not int or plan['schema_version']!=1:
        raise ValueError('Use the complete schema-1 private split plan')
    if plan['category']!='Schools (Secondary)' or plan['subcategory']!='Private' or type(plan['year']) is not int:
        raise ValueError('Private split requires an explicit year and Schools (Secondary) / Private')
    if plan['selection_policy']!=SELECTION_POLICY or plan['administrative_allocation_confirmed'] is not True:
        raise ValueError('Explicit selection policy and administrative-allocation confirmation required')
    for key in ('batch','campaign','seed','division','district'):
        if not isinstance(plan[key],str) or not plan[key].strip():raise ValueError('Private plan labels must be explicit')
    if plan['district']!='Lahore':raise ValueError('This named-school selector is scoped to Lahore')
    if set(plan['groups'])!={'raiwind','model_town'}:raise ValueError('Both disjoint groups must be defined together')
    for name,group in plan['groups'].items():
        if set(group)!={'scheme','tehsil','maximum_records','uc_numbers','allocation_profile'}:
            raise ValueError('Private group definition is incomplete')
        if group['tehsil']!=('Raiwind' if name=='raiwind' else 'Model Town'):
            raise ValueError('Private group Tehsil differs from its identity')
        if type(group['maximum_records']) is not int or group['maximum_records']<1:
            raise ValueError('Private group maximum_records must be positive')
        nums=group['uc_numbers']
        if not isinstance(nums,list) or not nums or any(type(n) is not int or n<1 for n in nums) or len(set(nums))!=len(nums):
            raise ValueError('Private group UC numbers must be unique positive integers')
    if len({g['scheme'] for g in plan['groups'].values()})!=2 or len({g['allocation_profile'] for g in plan['groups'].values()})!=2:
        raise ValueError('Private groups need separate schemes and profile files')
    if set(plan['paths'])!={'source_database','allocation_database','registration_database','normalization_database','catalog',
                           'normalization_base','registration_settings','sports_policy','normalization_profiles','output'}:
        raise ValueError('Private plan paths are incomplete')
    return plan


def paths(plan,path):
    return {key:(Path(path).resolve().parent/value).resolve() for key,value in plan['paths'].items()}


def catalog(path):
    return ensure_fresh(path)


def definitions(plan,p,path,counts=None):
    result={}
    for name,group in plan['groups'].items():
        profile=(Path(path).resolve().parent/group['allocation_profile']).resolve()
        normal=p['normalization_profiles']/(profile.stem+'.json')
        target_paths={'source_database':p['source_database'],'allocation_database':p['allocation_database'],
            'registration_database':p['registration_database'],'catalog':p['catalog'],'normalization_base':p['normalization_base'],
            'normalization_profile':normal,'registration_settings':p['registration_settings'],
            'registration_profile':p['registration_settings'].parent/(profile.stem+'.json'),
            'output':p['output']/group['tehsil']}
        result[name]=(profile,{'schema_version':1,'scheme':group['scheme'],'year':plan['year'],'course':'SSC','exam_type':'1',
            'source_institution':'NAMED LAHORE PRIVATE CANDIDATES','expected_records':counts[name] if counts else group['maximum_records'],
            'source_pool':{'batch':plan['batch'],'group':name},'category':plan['category'],'subcategory':plan['subcategory'],
            'division':plan['division'],'district':plan['district'],'tehsil':group['tehsil'],'uc_numbers':group['uc_numbers'],
            'administrative_allocation_confirmed':True,'paths':{k:os.path.relpath(v,profile.parent) for k,v in target_paths.items()}})
    return result


def sports_plan(plan,p,definition):
    value=json.loads(p['sports_policy'].read_text())
    value.update(scheme=definition['scheme'],campaign=plan['campaign'],category=plan['category'],seed=plan['seed']+':'+definition['scheme'])
    return value


def candidates(plan,p,allocations,current_catalog):
    excluded=Counter();result=[]
    # An applicant already normalized, allocated or imported anywhere is not new.
    used=set(registration_snapshot(p['registration_database'],plan['campaign']))
    used.update(r[0] for r in allocations.execute('SELECT applicant_key FROM allocations'))
    if allocations.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_pool_members'").fetchone():
        # The other group is reserved even before its normalization command.
        # A later batch must not claim those as-yet unallocated identities.
        used.update(r[0] for r in allocations.execute('SELECT applicant_key FROM private_pool_members'))
    if p['normalization_database'].exists():
        with closing(sqlite3.connect(p['normalization_database'].as_uri()+'?mode=ro',uri=True)) as db:
            used.update(r[0] for r in db.execute('SELECT DISTINCT bform FROM evaluations WHERE campaign=?',(plan['campaign'],)))
    with closing(sqlite3.connect(p['source_database'].as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        rows=db.execute("SELECT roll,result_json FROM jobs WHERE year=? AND course='SSC' AND exam_type='1' AND state='verified' AND upper(json_extract(result_json,'$.institution')) LIKE '%LAHORE%' ORDER BY roll",(plan['year'],))
        for roll,raw in rows:
            row=json.loads(raw);inst=(row.get('institution') or '').strip()
            if inst.upper()=='LAHORE':excluded['district_only_no_institute']+=1;continue
            if not LAHORE_SUFFIX.search(inst):excluded['district_not_confirmed_lahore']+=1;continue
            if PUBLIC.search(inst):excluded['government']+=1;continue
            if REVIEW.search(inst.split(',')[0]):excluded['public_or_ownership_review']+=1;continue
            if row.get('roll')!=roll or row.get('year')!=plan['year'] or (row.get('course'),row.get('exam_type'))!=('SSC','1'):
                raise ValueError('Source identity/exam differs from its database key')
            try:birth=born_on(row.get('date_of_birth',''),'%d/%m/%Y').isoformat()
            except (ValueError,TypeError):excluded['invalid_dob']+=1;continue
            if age_eligibility({'Category':plan['category'],'DOB':birth},current_catalog)['state']!='eligible':
                excluded['age_ineligible']+=1;continue
            key=identifier(row.get('student_cnic',''))
            if not key:excluded['invalid_bform']+=1;continue
            if key in used:excluded['already_normalized_allocated_or_imported']+=1;continue
            try:canonical_name(row.get('name',''));canonical_name(row.get('father_name',''))
            except ValueError:excluded['identity_name_needs_correction']+=1;continue
            result.append(row)
    repeats=Counter(identifier(r['student_cnic']) for r in result)
    duplicates=sum(repeats[identifier(r['student_cnic'])]>1 for r in result)
    excluded['duplicate_identity_rows']=duplicates
    result=[r for r in result if repeats[identifier(r['student_cnic'])]==1]
    result.sort(key=lambda r:signature([plan['seed'],identifier(r['student_cnic'])]))
    return result,dict(excluded)


def freeze(plan,p,current_catalog):
    semantic={k:v for k,v in plan.items() if k!='paths'}
    sports=json.loads(p['sports_policy'].read_text())
    semantic['sports_weights']=sports['weights']
    definition_hash=signature(semantic)
    with opened(p['allocation_database']) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS private_pools(batch TEXT PRIMARY KEY,definition_hash TEXT NOT NULL,
                definition TEXT NOT NULL,summary TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS private_pool_members(batch TEXT NOT NULL REFERENCES private_pools(batch),
                applicant_key TEXT NOT NULL,group_name TEXT NOT NULL,roll TEXT NOT NULL,source_record TEXT NOT NULL,
                PRIMARY KEY(batch,applicant_key),UNIQUE(batch,roll));
            CREATE INDEX IF NOT EXISTS private_pool_group ON private_pool_members(batch,group_name,roll);
        ''')
        for table in ('private_pools','private_pool_members'):
            for operation in ('UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS protect_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Private cohort is immutable'); END")
        db.executescript('''
            CREATE TRIGGER IF NOT EXISTS protect_private_pool_replace BEFORE INSERT ON private_pools
            WHEN EXISTS(SELECT 1 FROM private_pools WHERE batch=NEW.batch)
            BEGIN SELECT RAISE(ABORT,'Private cohort already exists'); END;
            CREATE TRIGGER IF NOT EXISTS protect_private_member_replace BEFORE INSERT ON private_pool_members
            WHEN EXISTS(SELECT 1 FROM private_pool_members WHERE batch=NEW.batch AND (applicant_key=NEW.applicant_key OR roll=NEW.roll))
            BEGIN SELECT RAISE(ABORT,'Private cohort member already exists'); END;
        ''')
        existing=db.execute('SELECT * FROM private_pools WHERE batch=?',(plan['batch'],)).fetchone()
        if existing:
            if existing['definition_hash']!=definition_hash:raise ValueError('Private split plan changed; use a new batch instead of reshuffling students')
            summary=json.loads(existing['summary']);summary['pool_status']='already_frozen';return summary
        records,excluded=candidates(plan,p,db,current_catalog)
        if len(records)<plan['groups']['raiwind']['maximum_records']:
            raise ValueError(f"Only {len(records)} unused eligible candidates; Raiwind requires {plan['groups']['raiwind']['maximum_records']}. No cohort created")
        maximum=sum(g['maximum_records'] for g in plan['groups'].values())
        available=len(records);records=records[:maximum];cut=plan['groups']['raiwind']['maximum_records']
        counts={'raiwind':cut,'model_town':len(records)-cut}
        if not counts['model_town']:raise ValueError('No remaining candidates for Model Town; no cohort created')
        summary={'pool_status':'frozen','batch':plan['batch'],'selected_pool':len(records),'available_candidates':available,'group_counts':counts,
            'maximum_requested':maximum,'unfilled_slots':maximum-len(records),'excluded':excluded,
            'school_classification':'operator_selected_Private; named non-government candidates, ownership not independently verified',
            'source_fingerprint':signature(records),'sports_weights':sports['weights']}
        with db:
            db.execute('INSERT INTO private_pools VALUES(?,?,?,?,?)',(plan['batch'],definition_hash,json.dumps(semantic),json.dumps(summary),now()))
            db.executemany('INSERT INTO private_pool_members VALUES(?,?,?,?,?)',[(plan['batch'],identifier(r['student_cnic']),
                'raiwind' if i<cut else 'model_town',r['roll'],json.dumps(r,ensure_ascii=False)) for i,r in enumerate(records)])
        return summary


def assign_sports(plan,p,definition,current_catalog):
    sports=sports_plan(plan,p,definition)
    path=p['normalization_profiles'].parent/'sports_profiles'/(Path(definition['paths']['normalization_profile']).stem+'.json')
    app.write_profile(path,sports);sports=policy(path,definition['scheme'],plan['campaign'],current_catalog)
    with opened(p['allocation_database']) as db:
        assigned=db.execute('SELECT count(*) FROM sport_assignments WHERE scheme=?',(definition['scheme'],)).fetchone()[0]
        if assigned:
            if assigned!=definition['expected_records']:raise ValueError('Incomplete existing private sports assignment; preserve and inspect it')
            return
        rows=[]
        for row in db.execute('SELECT roll,applicant_key,uc_number,source_record FROM allocations WHERE scheme=?',(definition['scheme'],)):
            record=json.loads(row['source_record']);gender=(record.get('gender') or '').strip().title()
            if gender not in ('Male','Female'):gender='Male' if int(row['applicant_key'][-1])%2 else 'Female'
            rows.append({'roll':row['roll'],'applicant_key':row['applicant_key'],'uc_number':row['uc_number'],'gender':gender})
        mapped=distribute(rows,sports['weights'],sports['seed']);batch=signature({'policy':sports,'candidates':rows})
        with db:
            db.execute('INSERT INTO sport_batches VALUES(?,?,?,?,?)',(batch,definition['scheme'],signature(sports),json.dumps(sports),now()))
            db.executemany('INSERT INTO sport_assignments VALUES(?,?,?,?,?,?,?,?)',[(definition['scheme'],r['applicant_key'],r['roll'],r['gender'],r['uc_number'],r['sport'],batch,now()) for r in mapped])


def run(path,group,emit=None):
    emit=emit or (lambda message:None)
    plan=load(path);p=paths(plan,path)
    if group not in plan['groups']:raise ValueError('Unknown private normalization group')
    emit('Checking saved catalog and the two administrative UC lists...')
    current_catalog=catalog(p['catalog'])
    base=app.normal_config(p['normalization_base'])
    from youthgames_register.config import resolved
    if (resolved(p['normalization_base'],base['portal']['normalization_database'])!=p['normalization_database']
            or resolved(p['normalization_base'],base['portal']['registration_database'])!=p['registration_database']
            or base['portal']['campaign']!=plan['campaign']):
        raise ValueError('Private plan and normalization base must share campaign and state databases')
    if plan['subcategory'] not in current_catalog['data']['participation']['subCategoriesByCategoryName'].get(plan['category'],[]):
        raise ValueError('Private participation category is not currently accepted by the portal catalog')
    # Fail on unknown UC IDs before reserving any cohort identities.
    with tempfile.TemporaryDirectory(prefix='private-policy-check-') as work:
        for _,definition in definitions(plan,p,path).values():
            app.targets(definition,current_catalog)
            checked=Path(work)/'sports.json';checked.write_text(json.dumps(sports_plan(plan,p,definition)))
            policy(checked,definition['scheme'],plan['campaign'],current_catalog)
    emit('Checking the shared frozen pool; first run selects unused eligible identities...')
    summary=freeze(plan,p,current_catalog)
    emit(f"Pool {summary['pool_status']}: {summary['selected_pool']:,} total; Raiwind {summary['group_counts']['raiwind']:,}; Model Town {summary['group_counts']['model_town']:,}")
    generated=definitions(plan,p,path,summary['group_counts'])
    for profile,definition in generated.values():app.write_profile(profile,definition)
    profile,definition=generated[group]
    allocation=app.apply(profile)
    assign_sports(plan,p,definition,current_catalog)
    effective=app.normalization_settings(profile)['defaults']['SubCategory']
    emit(f"Normalizing {allocation['assigned']:,} records for {definition['tehsil']} / {effective}; preserving institute names...")
    normalized=app.normalize(profile)
    result={'status':normalized['status'],'group':group,'tehsil':definition['tehsil'],'category':plan['category'],
        'subcategory':effective,'assigned':allocation['assigned'],'uc_count':len(definition['uc_numbers']),
        'pool':summary,'summary':normalized['summary'],'output':normalized['output'],
        'ready_master':str(Path(normalized['output'])/'Ready to Register.csv'),
        'allocation_profile':str(profile),'applicant_posts':0,'registration_database_written':False,
        'note':'Normalization only; use the explicit private-pilot command for three pinned live registrations.'}
    from youthgames_register.progress import atomic_json
    atomic_json(p['output']/'Allocation Summary.json',{'pool':summary,'groups':{name:{'scheme':value['scheme'],
        'reserved_records':summary['group_counts'][name],'tehsil':value['tehsil'],
        'targets':app.targets(value,current_catalog),'output':str(p['output']/value['tehsil'])}
        for name,(_,value) in generated.items()},'applicant_posts':0})
    return result
