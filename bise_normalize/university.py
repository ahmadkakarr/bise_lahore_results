"""Pinned Inter cohort preparation feeding the existing normalization engine."""
from collections import Counter,defaultdict
from contextlib import closing
from datetime import date,timedelta,datetime,timezone
import csv
import hashlib
import json
from pathlib import Path
import sqlite3

from .app import execute
from .config import template
from .engine import identifier
from youthgames_shared.catalog import signature
from bise_allocate.app import targets

SPORT_WEIGHTS={'Athletics':28,'Badminton':20,'Basketball':4,'Chess':2,'Cricket (Hard Ball)':12,
 'Cycling':1,'E-Sports':3,'Football':5,'Fencing':1,'Hockey':2,'Kabaddi (Boys Only)':1,
 'Karate':1,'Padel Tennis':1,'Table Tennis':3,'Taekwondo':1,'Tug of War':8,'Volleyball':3,
 'Wrestling (Boys Only)':1,'Handball':1,'Squash':1,'Laser Run':1}


def generated_dob(key,policy):
    if (policy.get('operator_confirmed_admin_exception') is not True or policy.get('birth_year')!=2006
            or not policy.get('seed')):raise ValueError('Explicit provisional-DOB admin policy required')
    first=date(policy['birth_year'],1,1);days=(date(policy['birth_year']+1,1,1)-first).days
    offset=int(hashlib.sha256((policy['seed']+':dob:'+key).encode()).hexdigest(),16)%days
    return (first+timedelta(days=offset)).isoformat()


def apportion(number,weights):
    total=sum(weights.values())
    counts={k:number*v//total for k,v in weights.items()}
    ranking=sorted(weights,key=lambda k:(-(number*weights[k]%total),k))
    for key in ranking[:number-sum(counts.values())]:counts[key]+=1
    return counts


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temp.replace(path)


def prepare(config_path):
    config_path=Path(config_path).resolve();c=json.loads(config_path.read_text());root=config_path.parent.parent
    if c['participation']!={'Category':'Universities','SubCategory':'Open Category'}:raise ValueError('Unexpected cohort participation')
    snapshot=Path(c['source']['database']);catalog=json.loads(Path(c['catalog']).read_text())
    if hashlib.sha256(snapshot.read_bytes()).hexdigest()!=c['source']['sha256']:raise ValueError('Pinned records snapshot changed')
    from youthgames_register.store import opened
    with opened(root/'state/registration.sqlite3') as ledger:
        migration=json.loads((root/'config/previous-registrations.json').read_text())
        with ledger:
            for row in migration['entries']:
                ledger.execute('INSERT OR IGNORE INTO imported_registrations VALUES(?,?,?,?,?,?,?,?)',
                    (row['portal'],row['campaign'],row['applicant_key'],row['status'],row['tracking'],row['confirmed_at'],migration['source_sha256'],row['payload_hash']))
        prior={r[0] for r in ledger.execute('SELECT applicant_key FROM imported_registrations WHERE campaign=?',(c['campaign'],))}
        prior.update(r[0] for r in ledger.execute('SELECT applicant_key FROM registered_applicants WHERE campaign=?',(c['campaign'],)))
    with closing(sqlite3.connect(snapshot.as_uri()+'?mode=ro',uri=True)) as src:
        src.row_factory=sqlite3.Row
        records=[];key_counts=Counter()
        for row in src.execute("SELECT id,roll,result_json FROM jobs WHERE state='verified' AND year=2024 AND course='HSSC' AND exam_type='2' ORDER BY roll"):
            record=json.loads(row['result_json']);key=identifier(record.get('student_cnic'))
            records.append((row['id'],row['roll'],key,record));key_counts[key]+=1
        refs={r['job_id']:dict(r) for r in src.execute('SELECT * FROM photo_references')}
    state=root/'state';state.mkdir(parents=True,exist_ok=True)
    allocation=sqlite3.connect(state/'allocations.sqlite3');allocation.row_factory=sqlite3.Row
    allocation.executescript('''CREATE TABLE IF NOT EXISTS cohort_meta(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE IF NOT EXISTS university_allocations(applicant_key TEXT PRIMARY KEY,roll TEXT UNIQUE,
      tehsil TEXT,uc_number INTEGER,uc_label TEXT,sport TEXT,dob TEXT,photo TEXT,source_record TEXT,provisional TEXT);
      CREATE TABLE IF NOT EXISTS cohort_exclusions(roll TEXT PRIMARY KEY,reason TEXT);''')
    definition=signature(c);saved=allocation.execute("SELECT value FROM cohort_meta WHERE key='definition'").fetchone()
    if saved and saved[0]!=definition:allocation.close();raise ValueError('Frozen cohort policy changed; use a new version')
    groups=[v['Tehsil'] for v in c['locations']['targets']]
    official={group:targets({'division':'Lahore','district':'Lahore','tehsil':group,
                'uc_numbers':next(v['uc_numbers'] for v in c['locations']['targets'] if v['Tehsil']==group)},catalog) for group in groups}
    summary=Counter(source_saved=len(records));policy=c['provisional_dob']
    if not saved:
        candidates=[]
        with allocation:
            for job,roll,key,record in records:
                reason='invalid_bform' if not key else 'duplicate_identity_group' if key_counts[key]>1 else 'already_registered' if key in prior else 'missing_photo' if job not in refs else None
                if reason:
                    allocation.execute('INSERT INTO cohort_exclusions VALUES(?,?)',(roll,reason));continue
                candidates.append((job,roll,key,record))
            candidates.sort(key=lambda r:hashlib.sha256((c['seed']+':target:'+r[2]).encode()).hexdigest())
            sport_groups=defaultdict(list)
            for index,item in enumerate(candidates):
                gender='Male' if int(item[2][-1])%2 else 'Female'
                sport_groups[(groups[index%2],gender)].append(item)
            for (group,gender),items in sport_groups.items():
                weights=dict(c['sport_weights'])
                if gender=='Female':
                    weights={k:v for k,v in weights.items() if 'boys only' not in k.casefold()}
                allowed=catalog['data']['participation']['sportsByCategoryName']['Universities']
                if set(weights)-set(allowed):raise ValueError('Sport policy contains a game not offered by Universities')
                counts=apportion(len(items),weights)
                games=[game for game in sorted(counts) for _ in range(counts[game])]
                items.sort(key=lambda r:hashlib.sha256((c['seed']+':sport:'+r[2]).encode()).hexdigest())
                for index,((job,roll,key,record),sport) in enumerate(zip(items,games)):
                    uc=official[group][index%len(official[group])]
                    from .engine import born_on
                    dob=born_on(record['date_of_birth'],'%d/%m/%Y').isoformat() if record.get('date_of_birth') else generated_dob(key,policy)
                    provisional={'value':dob,'original_value':record.get('date_of_birth',''),
                      'is_estimate':not bool(record.get('date_of_birth')),'source':'operator_provisional_randomized_birth_year',
                      'birth_year':2006,'seed':policy['seed'],'correction_required':not bool(record.get('date_of_birth')),
                      'operator_confirmed_admin_exception':True,'department_policy_verified_by_site':False}
                    ref=refs[job]
                    photo={'source_database':ref['source_database'],'source_sha256':c['photos']['source_sha256'],
                      'job_id':job,'roll':roll,'applicant_key':key,'sha256':ref['sha256'],
                      'mime_type':ref['mime_type'],'bytes':ref['payload_bytes']}
                    allocation.execute('INSERT INTO university_allocations VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (key,roll,group,uc['number'],uc['UC'],sport,dob,json.dumps(photo),json.dumps(record),json.dumps(provisional)))
            allocation.execute('INSERT INTO cohort_meta VALUES(?,?)',('definition',definition))
    summary.update({r[0]:r[1] for r in allocation.execute('SELECT reason,count(*) FROM cohort_exclusions GROUP BY reason')})
    fields=['Roll No','Collection Status','Student Name','Father Name','Student CNIC / B-Form','Date of Birth',
            'Institution / District','Registration DOB','Division','District','Tehsil','UC','Sport',
            'Administrative Allocation','Sport Allocation','Photo Reference','Provisional DOB Metadata']
    outputs=[]
    for group in groups:
        slug='raiwind' if group=='Raiwind' else 'model-town';path=root/'input'/(slug+'.csv');path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
            for row in allocation.execute('SELECT * FROM university_allocations WHERE tehsil=? ORDER BY roll',(group,)):
                record=json.loads(row['source_record']);metadata={'scheme':c['batch'],'tehsil':group,'uc_number':row['uc_number'],
                  'uc_label':row['uc_label'],'residence_inferred':False,'administrative_allocation':True}
                writer.writerow({'Roll No':row['roll'],'Collection Status':'Saved','Student Name':record['name'],
                   'Father Name':record['father_name'],'Student CNIC / B-Form':record['student_cnic'],
                   'Date of Birth':record.get('date_of_birth',''),'Registration DOB':row['dob'],
                   'Institution / District':record.get('institution',''),'Division':'Lahore','District':'Lahore','Tehsil':group,
                   'UC':'','Sport':row['sport'],'Administrative Allocation':json.dumps(metadata),
                   'Sport Allocation':json.dumps({'batch':definition,'applicant_key':row['applicant_key'],
                        'sport':row['sport'],'gender':'Male' if int(row['applicant_key'][-1])%2 else 'Female'}),
                   'Photo Reference':row['photo'],'Provisional DOB Metadata':row['provisional']})
        cfg=template();cfg['profile']=c['batch']+' '+group
        cfg['source'].update(path=str(path),format='csv',exam={'board':'BISE Lahore','year':2024,'course':'HSSC','part':'II','session':'Annual'})
        cfg['target']['template']=None;cfg['mapping']['DOB']='Registration DOB';cfg['mapping']['Institute']='Institution / District'
        cfg['defaults'].update(Category='Universities',SubCategory='Open Category',Division='Lahore',District='Lahore',Tehsil=group,Phone='03211234567')
        cfg['size_estimation']['as_of_date']=c['as_of_date'];cfg['validation']['completeness_required'].remove('UC')
        cfg['validation'].update(catalog=c['catalog'],eligibility={'mode':'catalog'})
        cfg['portal'].update(enabled=True,campaign=c['campaign'],normalization_database=str(state/'normalization.sqlite3'),registration_database=str(state/'registration.sqlite3'))
        cfg['output'].update(folder=str(root/'reports'/group),write_excel=False)
        profile=root/'config'/('normalize-'+slug+'.json');write_json(profile,cfg)
        result=execute(profile);outputs.append((group,Path(result['output'])))
        summary[group]=result['summary'].get('ready_to_register',0)
    allocation.close()
    combined=root/'reports/Combined';(combined/'Data').mkdir(parents=True,exist_ok=True)
    from . import COLUMNS
    provenance=[]
    with (combined/'Ready to Register.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=COLUMNS);writer.writeheader()
        for group,folder in outputs:
            with (folder/'Ready to Register.csv').open(encoding='utf-8-sig',newline='') as source:
                writer.writerows(csv.DictReader(source))
            provenance.extend(json.loads(line) for line in (folder/'Data/ready-to-register-provenance.jsonl').read_text().splitlines() if line.strip())
    prov=combined/'Data/ready-to-register-provenance.jsonl';prov.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in provenance))
    files={str(p.relative_to(combined)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [combined/'Ready to Register.csv',prov]}
    write_json(combined/'Data/manifest.json',{'application':'bise_normalize','profile':c['batch'],'files':files,'source_sha256':c['source']['sha256']})
    from youthgames_register.config import signature as photo_signature
    reg=json.loads(Path(__file__).parents[1].joinpath('youthgames_register/profile.template.json').read_text())
    reg.update(profile=c['batch'],campaign=c['campaign'])
    reg['input']={'csv':str(combined/'Ready to Register.csv'),'manifest':str(combined/'Data/manifest.json'),
      'provenance':str(prov),'expected_csv_sha256':files['Ready to Register.csv']}
    reg['selection'].update(category='Universities',subcategory='Open Category',sport=list(c['sport_weights']),rolls=[],
         dispatch_pattern=groups,dispatch_key=c['seed'])
    reg['photos'].update(mode='original_sqlite_reference',shared_file=None,source_database=c['photos']['original_database'],
        source_sha256=c['photos']['source_sha256'],expected_sha256=photo_signature(c['photos']))
    reg['browser'].update(headless=True,workers=4)
    reg['confirmation'].update(input_sha256=files['Ready to Register.csv'])
    reg['confirmation']['fields']=list(dict.fromkeys(reg['confirmation']['fields']+
        ['DOB','Category','SubCategory','Sport','Division','District','Tehsil','UC','OriginalPhoto']))
    rule=catalog['data']['participation']['dobEligibilityRulesByCategoryName']['Universities']
    reg['inspection'].update(observed_secondary_dob_min=rule['min_dob'],observed_secondary_dob_max=rule['max_dob'],client_date=c['as_of_date'])
    reg['pacing']['submission_interval_seconds']=6
    reg['state'].update(database=str(state/'registration.sqlite3'),output=str(root/'registration_output/Full'),write_reports=False)
    write_json(root/'config/register.json',reg)
    pilot_path=root/'config/register-pilot.json'
    if pilot_path.exists():pilots=json.loads(pilot_path.read_text())['selection']['rolls']
    else:
        pilots=[next(r['roll'] for r in provenance if r['data']['Tehsil']==g) for g in groups]
    reg['profile']=c['batch']+' Pilot';reg['selection']['rolls']=pilots
    reg['state']['output']=str(root/'registration_output/Pilot');write_json(pilot_path,reg)
    summary=dict(summary)
    summary.update(total_ready=len(provenance),pilot_rolls=pilots,estimated_dob_year=2006,
        games=len(c['sport_weights']),normalization_completed=True,applicant_posts=0)
    write_json(root/'reports/Summary.json',dict(summary))
    return dict(summary)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True)
    print(json.dumps(prepare(parser.parse_args().config),indent=2))
