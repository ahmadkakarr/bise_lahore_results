"""Start/resume the full server queue, independently of the laptop monitor."""
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from . import pilot
from .common import atomic, digest, now

STATE=pilot.ROOT/'artifacts/inter-2024-server'
JOB_ROOT='/var/lib/bise-results/jobs/inter-2024-part2-annual'
UNIT='bise-inter-2024'
GUARDIAN_UNIT='bise-inter-2024-storage'


def deployment():
    return json.loads((STATE/'deployment.json').read_text())


def start(profile):
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (STATE/'operator.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        config=json.loads((pilot.STATE/'deployment.json').read_text())
        active=pilot.guest(config,['systemctl','show',UNIT,'--property=ActiveState','--value']).decode().strip()
        if active in ('active','activating','deactivating'):
            print('Full server job is already running; no second collector started.',flush=True)
            return
        from bise_results.importer import audit
        audit_result=audit(Path(profile['input']))
        if (audit_result['invalid_rows'] or audit_result['duplicate_records']
                or not audit_result['unique_rolls'] or audit_result['sha256']!=config['input_sha256']):
            raise ValueError('Full roll list differs from the verified input; existing job preserved')
        # New code is deployed only with the collector stopped. Monitor commands
        # never invoke setup or change a running release.
        if config['source_sha256']!=hashlib.sha256(pilot.package()).hexdigest():
            config=pilot.setup(pilot.configure(profile))
        runtime={**profile,'input_sha256':audit_result['sha256'],'source_sha256':config['source_sha256'],
                 'expected_records':audit_result['unique_rolls'],'browser_executable':'/usr/bin/chromium'}
        for key in ('input','infrastructure_root','profile_path','ssh_host'):
            runtime.pop(key,None)
        install(config, runtime, Path(profile['input']).read_bytes())
        atomic(STATE/'deployment.json',{**config,'root':JOB_ROOT,'unit':UNIT,
                                      'workers':runtime['workers'],'expected_records':runtime['expected_records']})
        print(pilot.guest(config,['systemctl','enable','--now',UNIT]).decode(),flush=True)
        print(json.dumps({'status':'started','workers':runtime['workers'],'headless':True,
                          'rolls':runtime['expected_records'],'root':JOB_ROOT,'vmid':config['vmid']}),flush=True)


def install(config,runtime,csv):
    """Upload an immutable input, retain prior DB, install isolated systemd units."""
    if hashlib.sha256(csv).hexdigest()!=runtime['input_sha256']:
        raise ValueError('Input changed during transfer preparation')
    script='''import pathlib,os,json,sys,hashlib,fcntl,subprocess
root=pathlib.Path(ROOT);root.mkdir(parents=True,exist_ok=True)
with (root/'operator.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 if subprocess.run(['systemctl','is-active','--quiet','bise-inter-2024']).returncode==0:
  raise RuntimeError('Full collector became active; refusing to change its input')
 for name in ('input','runtime','evidence','checkpoints','snapshots'):(root/name).mkdir(exist_ok=True)
 path=root/'input/rolls.csv';payload=sys.stdin.buffer.read()
 if hashlib.sha256(payload).hexdigest()!=EXPECTED:raise ValueError('Transferred CSV checksum differs')
 if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=EXPECTED:raise ValueError('Existing remote input differs')
 if not path.exists():
  temporary=path.with_suffix('.tmp');temporary.write_bytes(payload);os.chmod(temporary,0o444);os.replace(temporary,path)
 info=json.loads(CONFIG)
 old=root/'config.json'
 if old.exists() and json.loads(old.read_text())['input_sha256']!=info['input_sha256']:raise ValueError('Pinned job hash differs')
 for path,value in ((old,info),(root/'runtime/request.json',{'id':info['input_sha256'][:16],'limit':0})):
  temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value));os.chmod(temp,0o444);os.replace(temp,path)
 import pwd
 user=pwd.getpwnam('bise')
 for path in [root,root/'operator.lock',*[root/name for name in ('runtime','evidence','checkpoints','snapshots')]]:os.chown(path,user.pw_uid,user.pw_gid)
 os.chmod(root,0o700)
'''.replace('ROOT',repr(JOB_ROOT)).replace('EXPECTED',repr(runtime['input_sha256'])).replace('CONFIG',repr(json.dumps(runtime)))
    pilot.guest(config,['python3','-c',script],payload=csv)
    # Seed the pilot's complete identities/photos without copying a live WAL DB.
    seed='''import sqlite3,pathlib,os
root=pathlib.Path(ROOT);target=root/'results.sqlite3'
if not target.exists():
 source=sqlite3.connect('file:'+PILOT+'/results.sqlite3?mode=ro',uri=True)
 assert source.execute('pragma quick_check').fetchone()[0]=='ok'
 assert not source.execute("select 1 from jobs where year!=2024 or course!='HSSC' or exam_type!='2' limit 1").fetchone()
 backup=sqlite3.connect(root/'seed.sqlite3.tmp');source.backup(backup)
 assert backup.execute('pragma quick_check').fetchone()[0]=='ok'
 backup.execute('pragma journal_mode=DELETE');backup.close();source.close()
 os.replace(root/'seed.sqlite3.tmp',target)
'''.replace('ROOT',repr(JOB_ROOT)).replace('PILOT',repr(pilot.GUEST_ROOT))
    pilot.guest(config,['runuser','-u','bise','--','python3','-c',seed])
    unit=f'''[Unit]
Description=BISE Inter Part-II 2024 full collector
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=bise
Group=bise
UMask=0077
WorkingDirectory={config['release']}
ExecStart={config['release']}/.venv/bin/python -u -m bise_remote.worker --root {JOB_ROOT}
Restart=on-failure
RestartSec=30
RestartPreventExitStatus=2
KillMode=mixed
TimeoutStopSec=90
MemoryMax=5G
CPUQuota=300%
TasksMax=768

[Install]
WantedBy=multi-user.target
'''
    pilot.guest(config,['tee',f'/etc/systemd/system/{UNIT}.service'],payload=unit.encode())
    pilot.guest(config,['systemctl','daemon-reload'])
    # The host timer keeps storage checks independent of this laptop connection.
    guardian_source=Path(__file__).with_name('guardian.py').read_bytes()
    guardian_path='/opt/bise-results-guardian.py'
    guardian_service=f'''[Unit]
Description=Storage guard for BISE collector LXC {config['vmid']}
ConditionPathExists=/etc/pve/lxc/{config['vmid']}.conf
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {guardian_path} --vmid {config['vmid']} --root {JOB_ROOT}
TimeoutStartSec=60
MemoryMax=512M
CPUQuota=50%
'''
    guardian_timer=f'''[Unit]
Description=Refresh BISE host storage admission every minute
[Timer]
OnBootSec=20
OnUnitActiveSec=60
AccuracySec=5
Unit={GUARDIAN_UNIT}.service
[Install]
WantedBy=timers.target
'''
    write='''import base64,json,pathlib,os,sys
for name,payload in json.load(sys.stdin).items():
 path=pathlib.Path(name);temp=path.with_suffix(path.suffix+'.tmp');temp.write_bytes(base64.b64decode(payload));os.chmod(temp,0o644);os.replace(temp,path)
'''
    import base64
    files={guardian_path:guardian_source,f'/etc/systemd/system/{GUARDIAN_UNIT}.service':guardian_service.encode(),
           f'/etc/systemd/system/{GUARDIAN_UNIT}.timer':guardian_timer.encode()}
    pilot.ssh(config['ssh_host'],['python3','-c',write],payload=json.dumps({k:base64.b64encode(v).decode() for k,v in files.items()}).encode())
    pilot.ssh(config['ssh_host'],['systemctl','daemon-reload'])
    pilot.ssh(config['ssh_host'],['systemctl','enable','--now',GUARDIAN_UNIT+'.timer'])
    pilot.ssh(config['ssh_host'],['systemctl','start',GUARDIAN_UNIT+'.service'])


def status(config):
    script='''import json,pathlib,subprocess,sys,datetime,shutil
root=pathlib.Path(sys.argv[1]);path=root/'runtime/status.json'
value=json.loads(path.read_text()) if path.exists() else {'status':'starting'}
value['service']=dict(line.split('=',1) for line in subprocess.check_output(['systemctl','show','bise-inter-2024','--property=ActiveState,SubState,ExecMainStatus']).decode().splitlines() if '=' in line)
value['disk_free_bytes']=shutil.disk_usage(root).free
value['observed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
health=root/'runtime/host-storage.json'
value['host_storage']=json.loads(health.read_text()) if health.exists() else {'safe':False}
print(json.dumps(value))
'''
    return json.loads(pilot.guest(config,['runuser','-u','bise','--','python3','-c',script,JOB_ROOT],timeout=30))


def panel(value, rate=None):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.progress import Progress,BarColumn,TextColumn
    counts=value.get('counts',{});total=value.get('expected_records') or counts.get('total',0)
    saved=counts.get('verified',0);done=saved+counts.get('failed',0)+counts.get('needs_review',0)
    service=value.get('service',{}).get('ActiveState','unknown')
    age='unknown'
    if value.get('heartbeat_at'):
        age=str(max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(value['heartbeat_at'])).total_seconds())))+'s'
    progress=Progress(TextColumn('Processed'),BarColumn(),TextColumn('{task.completed:,.0f}/{task.total:,.0f}'))
    progress.add_task('queue',total=max(1,total),completed=done)
    registration=value.get('kind')=='registration'
    transport='HTTP workers' if registration else 'headless workers'
    status_text=(f"{value.get('status','unknown')} | service {service} | {value.get('workers',8)} {transport} | heartbeat {age}\n"
                 f"Saved {saved:,}  •  pending {counts.get('pending',max(0,total-done)):,}  •  review {counts.get('needs_review',0):,}  •  failed {counts.get('failed',0):,}\n"
                 f"Free disk {value.get('disk_free_bytes',0)/1024**3:.1f} GiB" + (f'  •  {rate:.1f} saved/min' if rate is not None else ''))
    workers=Table('Worker','Job','Tehsil','Activity',expand=True)
    for number,event in sorted(value.get('worker_activity',{}).items(),key=lambda x:int(x[0])):
        workers.add_row(number,str(event.get('job','')),event.get('group',''),event.get('phase',event.get('state','waiting')))
    parts=[Panel(Group(Text(status_text),progress),title='Inter 2024 • '+('Universities registration '+value.get('mode','') if registration else 'Server collector'),border_style='cyan'),workers]
    if registration:
        groups=Table('Tehsil','Registered','External duplicate','Pending','Needs attention',expand=True)
        for group,states in value.get('groups',{}).items():
            groups.add_row(group,str(states.get('registered',0)),str(states.get('already_registered_external',0)),
               str(sum(states.get(k,0) for k in ('ready','filling','filled','submitting'))),
               str(sum(states.get(k,0) for k in ('rejected','blocked_input','uncertain'))))
        parts.append(groups)
        for row in value.get('recent_tracking',[]):parts.append(Text(f"{row['group']} • {row['roll']} • {row['tracking']}"))
    parts.append(Text('Ctrl+C closes this monitor only; the server job keeps running.'))
    return Group(*parts)


def watch(config,interval=5,status_provider=None):
    from collections import deque
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    samples=deque();last=None
    try:
        with Live(Panel('Connecting to server…'),refresh_per_second=1) as live:
            while True:
                try:
                    value=(status_provider or status)(config);moment=time.monotonic();saved=value.get('counts',{}).get('verified',0)
                    samples.append((moment,saved))
                    while len(samples)>2 and samples[0][0]<moment-60:samples.popleft()
                    rate=(saved-samples[0][1])*60/max(1,moment-samples[0][0])
                    last=value;live.update(panel(value,rate))
                except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
                    live.update(Panel(Text('Monitor connection unavailable; retrying. Server job is independent.\n'
                        + (f"Last observed saved: {last.get('counts',{}).get('verified',0):,}" if last else 'No status received yet.')),border_style='yellow'))
                time.sleep(interval)
    except KeyboardInterrupt:
        print('Monitor closed. Server collector was not stopped.',flush=True)


def logs(config):
    command=['pct','exec',str(config['vmid']),'--','runuser','-u','bise','--','tail','-n','40','-F',config.get('root',JOB_ROOT)+'/runtime/events.jsonl']
    try:subprocess.run(['ssh',*pilot.HOST_OPTIONS,config['ssh_host'],shlex.join(command)],check=False)
    except KeyboardInterrupt:print('Log monitor closed; server collector continues.')


def stop(config):
    pilot.guest(config,['systemctl','disable','--now',UNIT],timeout=120)
    print('Server collector stopped. Committed rows remain; start resumes pending work.')


def download(config):
    """Bounded-memory, resumable download of one pinned online snapshot."""
    from . import snapshots
    output=pilot.ROOT/'remote_downloads/Inter 2024 - Part-II Annual'
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    pending=output/'pending-snapshot.json'
    prefix=[config['release']+'/.venv/bin/python','-c']
    def guest_python(code):
        return ['pct','exec',str(config['vmid']),'--','runuser','-u','bise','--','sh','-c',
                'cd '+shlex.quote(config['release'])+' && '+shlex.join([*prefix,code])]
    if pending.exists():item=json.loads(pending.read_text())
    else:
        code='from bise_remote.snapshots import create;import json;print(json.dumps(create('+repr(JOB_ROOT)+')))'
        item=json.loads(pilot.ssh(config['ssh_host'],guest_python(code),timeout=300))
        atomic(pending,item)
    compressed=output/item['filename'];partial=compressed.with_suffix(compressed.suffix+'.part')
    offset=partial.stat().st_size if partial.exists() else 0
    code='from bise_remote.snapshots import stream;import sys;stream('+repr(JOB_ROOT)+','+repr(item['id'])+','+str(offset)+',sys.stdout.buffer)'
    cmd=guest_python(code)
    with partial.open('ab') as stream:
        result=subprocess.run(['ssh',*pilot.HOST_OPTIONS,config['ssh_host'],shlex.join(cmd)],stdout=stream)
    if result.returncode:raise RuntimeError('Download interrupted; rerun download to resume this snapshot')
    partial.replace(compressed)
    snapshots.restore(compressed,output/'results.sqlite3',item)
    atomic(output/'Snapshot.json',item);pending.unlink()
    code='from bise_remote.snapshots import release;release('+repr(JOB_ROOT)+','+repr(item['id'])+')'
    pilot.ssh(config['ssh_host'],guest_python(code))
    print('Downloaded and verified: '+str(output/'results.sqlite3'))
