"""Deploy university profiles and reuse existing remote transport/monitoring."""
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import shlex

from . import pilot,control
from .common import atomic,load

STATE=pilot.ROOT/'artifacts/inter-2024-university-registration'
ROOT='/var/lib/bise-results/workflows/inter-2024-universities-open-20261025'
UNIT='youthgames-inter2024'


def export_ledger():
    import fcntl
    source=pilot.ROOT/'data/youthgames.sqlite3'
    with source.with_suffix('.lock').open('rb') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;db.execute('BEGIN')
            entries=[dict(r) for r in db.execute('SELECT portal,campaign,applicant_key,status,tracking,confirmed_at,payload_hash FROM registered_applicants')]
    return {'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'entries':entries,'exported_at':datetime.now(timezone.utc).isoformat()}


def deployment():return json.loads((STATE/'deployment.json').read_text())


def prepare():
    from bise_normalize.university import SPORT_WEIGHTS
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    old=control.deployment()
    if pilot.guest(old,['systemctl','show',UNIT,'--property=ActiveState','--value']).decode().strip()=='active':
        raise ValueError('Stop registration before changing its pinned input')
    config=pilot.setup(pilot.configure(load(pilot.ROOT/'remote_profiles/inter-2024-part2.json')))
    proposed=json.loads((pilot.ROOT/'docs/INTER_2024_UNIVERSITIES_OPEN_PROFILE.proposed.json').read_text())
    receipt=json.loads((pilot.ROOT/'artifacts/inter-2024-registration-plan/Records Snapshot Receipt.json').read_text())
    proposed.update(seed='inter2024-universities-open-v1',as_of_date='2026-10-25',sport_weights=SPORT_WEIGHTS,
                    catalog=ROOT+'/config/catalog.json',provisional_dob={'birth_year':2006,
                    'seed':'operator-approved-inter2024-random-dob-v1','operator_confirmed_admin_exception':True})
    proposed['photos']['source_sha256']=receipt['source_sha256']
    import httpx
    from youthgames_register.http_pilot import form_catalog
    from youthgames_shared.catalog import signature
    response=httpx.get('https://youthgames.punjab.gov.pk/',timeout=30,trust_env=False);response.raise_for_status()
    _,_,data=form_catalog(response.text)
    catalog={'schema_version':1,'source_url':'https://youthgames.punjab.gov.pk/#registration',
             'fetched_at':datetime.now(timezone.utc).isoformat(),'data':data,'content_sha256':signature(data)}
    files={'batch.json':proposed,'catalog.json':catalog,'previous-registrations.json':export_ledger()}
    script='''import pathlib,json,sys,os,pwd
root=pathlib.Path(ROOT);user=pwd.getpwnam('bise')
for name in ('config','state','input','reports','runtime'):
 path=root/name;path.mkdir(exist_ok=True);os.chown(path,user.pw_uid,user.pw_gid)
for name,value in json.load(sys.stdin).items():
 path=root/'config'/name
 if name=='batch.json' and path.exists():
  previous=json.loads(path.read_text())
  if previous!=value:raise ValueError('Frozen cohort config differs; create a new workflow version')
 path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\\n');os.chown(path,user.pw_uid,user.pw_gid);os.chmod(path,0o600)
'''.replace('ROOT',repr(ROOT))
    pilot.guest(config,['python3','-c',script],payload=json.dumps(files).encode())
    invocation=[config['release']+'/.venv/bin/python','-m','bise_normalize.university','--config',ROOT+'/config/batch.json']
    command=['pct','exec',str(config['vmid']),'--','runuser','-u','bise','--','sh','-c',
             'cd '+shlex.quote(config['release'])+' && '+shlex.join(invocation)]
    result=pilot.ssh(config['ssh_host'],command,timeout=600)
    (STATE/'Preparation.json').write_bytes(result)
    value={**config,'root':ROOT,'unit':UNIT,'workers':4,'kind':'registration'}
    atomic(STATE/'deployment.json',value)
    print(result.decode(),flush=True)


def start(pilot_mode=False):
    c=deployment()
    active=pilot.guest(c,['systemctl','show',UNIT,'--property=ActiveState','--value']).decode().strip()
    if active in ('active','activating','deactivating'):
        print('Registration is already active; no second runner started.');return
    # Repair older generated profiles without moving/deleting their receipts.
    # Only the inactive runner's configured output directory changes.
    import inspect
    from .registration_profiles import isolate_outputs
    script='import json,os\nfrom pathlib import Path\n'+inspect.getsource(isolate_outputs)+'\nprint(isolate_outputs('+repr(ROOT)+'))'
    pilot.guest(c,['runuser','-u','bise','--','python3','-c',script])
    full='' if pilot_mode else ' --full'
    unit=f'''[Unit]
Description=Inter 2024 Universities Open registration {'pilot' if pilot_mode else 'full'}
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=bise
Group=bise
UMask=0077
WorkingDirectory={c['release']}
ExecStart={c['release']}/.venv/bin/python -u -m bise_remote.registration_worker --root {ROOT}{full}
TimeoutStopSec=90
KillMode=mixed
MemoryMax=2G
CPUQuota=100%
TasksMax=128
Restart={'no' if pilot_mode else 'on-failure'}
RestartSec=30
RestartPreventExitStatus=2
[Install]
WantedBy=multi-user.target
'''
    pilot.guest(c,['tee','/etc/systemd/system/'+UNIT+'.service'],payload=unit.encode())
    pilot.guest(c,['systemctl','daemon-reload'])
    pilot.guest(c,['systemctl','start',UNIT] if pilot_mode else ['systemctl','enable','--now',UNIT])
    atomic(pilot.ROOT/'data/youthgames.remote-owner.json',{'writer':'server','host':c['ssh_host'],
           'vmid':c['vmid'],'database':ROOT+'/state/registration.sqlite3','local_database_retained':True})
    atomic(control.STATE/'active-task.json',{'job':'registration'})
    print('Registration pilot started: exactly one pinned Raiwind and one Model Town applicant.' if pilot_mode else 'Full registration started.',flush=True)


def status(c):
    code='''import pathlib,json,sys,subprocess,datetime
root=pathlib.Path(sys.argv[1]);path=root/'runtime/status.json'
value=json.loads(path.read_text()) if path.exists() else {'kind':'registration','status':'starting','workers':4}
value['service']=dict(line.split('=',1) for line in subprocess.check_output(['systemctl','show','youthgames-inter2024','--property=ActiveState,SubState,ExecMainStatus']).decode().splitlines() if '=' in line)
value['observed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();print(json.dumps(value))
'''
    return json.loads(pilot.guest(c,['runuser','-u','bise','--','python3','-c',code,ROOT],timeout=30))


def stop(c):
    pilot.guest(c,['systemctl','stop',UNIT],timeout=120);print('Registration stopped; committed receipts preserved.')


def logs(c):
    control.logs(c)
