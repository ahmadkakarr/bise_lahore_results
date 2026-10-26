"""Profile-owned SSC server deployment. Existing Inter services are never changed."""
import base64
import fcntl
import gzip
import hashlib
import inspect
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import sys

from . import pilot,control
from .common import atomic,digest,now


def state(profile):return pilot.ROOT/'artifacts'/profile['job']
def deployment(profile):return json.loads((state(profile)/'deployment.json').read_text())


def identity(config,current):
    if (str(current.get('description','')).strip()!=config['marker'] or current.get('hostname')!=config['name'] or not current.get('unprivileged')):
        raise ValueError('Reserved guest ownership differs')
    net=dict(p.split('=',1) for p in current.get('net0','').split(',') if '=' in p)
    if net.get('ip')!=config['address'] or not current.get('rootfs','').startswith(config['storage']+':'):
        raise ValueError('Reserved guest placement differs')


def configure(profile):
    root=Path(profile['infrastructure_root']);folder=state(profile);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    sys.path.insert(0,str(root/'platform'))
    from android_builder.allocation import choose_vmid,choose_ipv4,reserve_assignment
    from android_builder.autodiscovery import extract_ipv4_claims
    with (root/'.state/android-builder/runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=folder/'deployment.json'
        if path.exists():
            config=json.loads(path.read_text())
            config.update(cores=profile['cores'],memory_mib=profile['memory_mib'])
            atomic(path,config);return config
        inventory=pilot.inventory(profile['ssh_host']);owner=str(path.resolve());environment=profile['placement_environment']
        vmid=choose_vmid(root,inventory['resources'],environment,1001,owner=owner)
        claims=[ip for c in inventory['configs'] for ip in extract_ipv4_claims(c)]
        address=choose_ipv4(root,claims,environment,owner=owner)
        reserve_assignment(root,path,vmid,address)
        config={'vmid':vmid,'address':address,'ssh_host':profile['ssh_host'],'job':profile['job'],
          'name':'bise-'+profile['job'],'marker':'bise-results:'+profile['job'],'storage':'local-lvm',
          'template':'local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst',
          'root':'/var/lib/bise-results/workflows/'+profile['job'],'unit':'bise-'+profile['job'],
          'phase':'reserved','cores':profile['cores'],'memory_mib':profile['memory_mib'],'disk_gib':profile['disk_gib'],
          'photo_storage':'files','download_folder':'remote_downloads/Matric 2026 - Part-I Annual'}
        atomic(path,config);return config


def provision(config):
    script=inspect.getsource(identity)+'''
import fcntl,json,subprocess,shutil
c=json.loads(CONFIG)
def run(*args):return subprocess.check_output(list(map(str,args)),stderr=subprocess.STDOUT).decode()
with open('/run/lock/'+c['unit']+'.lock','a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 rows=json.loads(run('pvesh','get','/cluster/resources','--type','vm','--output-format','json'))
 existing=next((r for r in rows if r['vmid']==c['vmid']),None)
 if existing:
  if existing['type']!='lxc':raise ValueError('Reserved ID belongs to another guest')
  identity(c,json.loads(run('pvesh','get',f"/nodes/{existing['node']}/lxc/{c['vmid']}/config",'--output-format','json')))
  run('pct','set',c['vmid'],'--cores',c['cores'],'--memory',c['memory_mib'])
 else:
  pools=json.loads(run('pvesh','get','/nodes/pve/storage','--output-format','json'))
  if next(v for v in pools if v['storage']==c['storage'])['avail']<(c['disk_gib']+64)*1024**3 or shutil.disk_usage('/').free<8*1024**3:raise ValueError('Insufficient host reserve')
  if subprocess.run(['ping','-c','2','-W','1',c['address'].split('/')[0]],stdout=subprocess.DEVNULL).returncode==0:raise ValueError('Allocated LAN address responds')
  run('pct','create',c['vmid'],c['template'],'--hostname',c['name'],'--description',c['marker'],'--unprivileged','1',
      '--cores',c['cores'],'--memory',c['memory_mib'],'--swap','512','--rootfs',c['storage']+':'+str(c['disk_gib']),
      '--net0',f"name=eth0,bridge=vmbr0,ip={c['address']},gw=192.168.10.1,firewall=1",'--nameserver','1.1.1.1','--features','nesting=1','--onboot','1','--tags','bise-results;ssc-collector')
 if 'running' not in run('pct','status',c['vmid']):run('pct','start',c['vmid'])
print('Dedicated SSC guest ready')
'''
    script=script.replace('CONFIG',repr(json.dumps(config)))
    print(pilot.ssh(config['ssh_host'],['python3','-'],payload=script.encode(),timeout=240).decode(),flush=True)


def release(config):
    archive=pilot.package();sha=hashlib.sha256(archive).hexdigest();path='/opt/bise-results/releases/'+sha[:20]
    if config.get('source_sha256')==sha:return config
    for unit in (config['unit'],config['unit']+'-photos',config['unit']+'-normalize'):
        active=pilot.guest(config,['systemctl','show',unit,'--property=ActiveState','--value']).decode().strip()
        if active in ('active','activating','deactivating'):raise ValueError('Stop this SSC workflow before updating its code; Inter is unaffected')
    install='''set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv tesseract-ocr chromium ca-certificates
id bise >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/bise-results --shell /usr/sbin/nologin bise
install -d -m 755 /opt/bise-results/releases
'''
    folder=pilot.ROOT/'artifacts'/config['job']
    (folder/'install.log').write_bytes(pilot.guest(config,['bash','-s'],payload=install.encode(),timeout=1200))
    pilot.guest(config,['mkdir','-p',path]);pilot.guest(config,['tar','xz','-C',path],payload=archive)
    pilot.guest(config,['python3','-m','venv',path+'/.venv'])
    (folder/'pip.log').write_bytes(pilot.guest(config,[path+'/.venv/bin/pip','install','--disable-pip-version-check','-r',path+'/requirements.lock.txt'],timeout=600))
    config.update(release=path,source_sha256=sha,phase='installed');atomic(folder/'deployment.json',config)
    return config


def seed(profile):
    """Coherent scoped copy; original DB and retention receipts remain untouched."""
    folder=state(profile);path=folder/'seed.sqlite3';metadata=folder/'seed.json'
    if metadata.exists() and path.exists() and digest(path)==json.loads(metadata.read_text())['sha256']:return json.loads(metadata.read_text())
    source=Path(profile['seed_database']).resolve();temporary=path.with_suffix('.tmp');temporary.unlink(missing_ok=True)
    live=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True);db=sqlite3.connect(temporary)
    try:
        live.backup(db,pages=1024,sleep=.02)
        db.execute('PRAGMA foreign_keys=OFF')
        for table in ('members','attempts','database_retention','student_photos','photo_assets','photo_recovery'):
            if db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(table,)).fetchone():
                db.execute(f"DELETE FROM {table} WHERE job_id IN(SELECT id FROM jobs WHERE NOT(year=2026 AND course='SSC' AND exam_type='1'))")
        db.execute("DELETE FROM jobs WHERE NOT(year=2026 AND course='SSC' AND exam_type='1')")
        db.execute('DELETE FROM batches WHERE id NOT IN(SELECT batch_id FROM members)')
        db.execute("UPDATE jobs SET state='pending' WHERE state='running'")
        db.commit();assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchone()
        if (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='student_photos'").fetchone()
            and db.execute('SELECT count(*) FROM student_photos').fetchone()[0]):raise ValueError('Seed has image BLOBs; explicit file migration required')
        rows=db.execute('SELECT state,count(*) FROM jobs GROUP BY state').fetchall()
        csv=folder/'rolls.csv'
        with csv.open('w') as stream:
            stream.write('Roll Number\n')
            for row in db.execute('SELECT roll FROM jobs ORDER BY CAST(roll AS INTEGER),roll'):stream.write(row[0]+'\n')
        db.execute('VACUUM');db.execute('PRAGMA journal_mode=DELETE')
    finally:db.close();live.close()
    os.replace(temporary,path)
    info={'sha256':digest(path),'bytes':path.stat().st_size,'states':dict(rows),'input_sha256':digest(csv),'at':now()}
    atomic(metadata,info);return info


def upload(config,path,target):
    """Resume verified chunks; never expose an incomplete authoritative file."""
    path=Path(path);sha=digest(path)
    code='''import sys,pathlib,hashlib,os
p=pathlib.Path(sys.argv[1]);expected=sys.argv[2]
if p.exists():
 if hashlib.sha256(p.read_bytes()).hexdigest()!=expected:raise ValueError('Existing pinned file differs')
 print('complete')
else:
 q=p.with_suffix(p.suffix+'.part');print(q.stat().st_size if q.exists() else 0)
'''
    offset=pilot.guest(config,['python3','-c',code,target,sha]).decode().strip()
    if offset=='complete':return
    offset=int(offset)
    if offset>path.stat().st_size:raise ValueError('Remote partial larger than source')
    chunk_code='''import sys,pathlib,os
p=pathlib.Path(sys.argv[1]);offset=int(sys.argv[2]);data=sys.stdin.buffer.read()
with p.open('ab') as f:
 if f.tell()!=offset:raise ValueError('Transfer offset differs')
 f.write(data);f.flush();os.fsync(f.fileno())
'''
    with path.open('rb') as stream:
        stream.seek(offset)
        while block:=stream.read(8*1024**2):
            pilot.guest(config,['python3','-c',chunk_code,target+'.part',str(offset)],payload=block,timeout=120)
            offset+=len(block);print(f'Seed transfer {offset//1024**2}/{path.stat().st_size//1024**2} MiB',flush=True)
    finish='''import sys,pathlib,hashlib,os
p=pathlib.Path(sys.argv[1]);q=p.with_suffix(p.suffix+'.part')
if hashlib.sha256(q.read_bytes()).hexdigest()!=sys.argv[2]:raise ValueError('Transferred checksum differs')
os.replace(q,p)
'''
    pilot.guest(config,['python3','-c',finish,target,sha])


def install(profile,config):
    folder=state(profile);info=seed(profile);root=config['root']
    compressed=folder/'seed.sqlite3.gz'
    if not compressed.exists() or not (folder/'seed-gzip.json').exists() or json.loads((folder/'seed-gzip.json').read_text())['source']!=info['sha256']:
        with (folder/'seed.sqlite3').open('rb') as incoming,gzip.open(compressed,'wb',compresslevel=1) as outgoing:shutil.copyfileobj(incoming,outgoing,1024**2)
        atomic(folder/'seed-gzip.json',{'source':info['sha256'],'sha256':digest(compressed)})
    pilot.guest(config,['install','-d','-o','bise','-g','bise','-m','700',root,*[root+'/'+s for s in ('input','runtime','evidence','photo-evidence','photos','checkpoints','snapshots')]])
    upload(config,compressed,root+'/seed.sqlite3.gz')
    runtime={**profile,'input_sha256':info['input_sha256'],'source_sha256':config['source_sha256'],
       'expected_records':sum(info['states'].values()),'browser_executable':'/usr/bin/chromium'}
    for name in ('seed_database','normalization_profile','profile_path','infrastructure_root','ssh_host','input'):runtime.pop(name,None)
    files={'input/rolls.csv':(folder/'rolls.csv').read_bytes(),'config.json':json.dumps(runtime).encode(),
       'normalization.json':Path(profile['normalization_profile']).read_bytes(),
       'catalog.json':(pilot.ROOT/'data/youthgames_catalog.json').read_bytes()}
    write='''import sys,json,pathlib,base64,os
root=pathlib.Path(sys.argv[1])
for name,data in json.load(sys.stdin).items():
 p=root/name;temp=p.with_suffix('.tmp');temp.write_bytes(base64.b64decode(data));os.chmod(temp,0o600);os.replace(temp,p)
'''
    pilot.guest(config,['runuser','-u','bise','--','python3','-c',write,root],payload=json.dumps({k:base64.b64encode(v).decode() for k,v in files.items()}).encode())
    initialize='''import pathlib,gzip,shutil,os,sqlite3,json
from bise_results.store import locked_store
from bise_remote.common import atomic,digest
root=pathlib.Path(ROOT);target=root/'results.sqlite3'
if not target.exists():
 temp=root/'restoring.sqlite3'
 with gzip.open(root/'seed.sqlite3.gz','rb') as incoming,temp.open('wb') as outgoing:shutil.copyfileobj(incoming,outgoing)
 if digest(temp)!=EXPECTED:raise ValueError('Restored database differs')
 os.replace(temp,target)
with locked_store(target) as db:
 db.execute('CREATE TABLE IF NOT EXISTS workflow_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
 if not db.execute("SELECT 1 FROM workflow_meta WHERE key='bootstrap-events'").fetchone():
  with db:
   db.execute("INSERT INTO result_events(job_id,kind,at) SELECT id,'bootstrap',updated_at FROM jobs WHERE state='verified'")
   db.execute("INSERT INTO workflow_meta VALUES('bootstrap-events','1')")
 assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
 assert not db.execute('PRAGMA foreign_key_check').fetchone()
atomic(root/'runtime/request.json',{'id':'full','limit':0})
'''.replace('ROOT',repr(root)).replace('EXPECTED',repr(info['sha256']))
    # The completed compressed transfer is root-owned; only this private guest's
    # collector account needs read access to restore it.
    pilot.guest(config,['chown','bise:bise',root+'/seed.sqlite3.gz'])
    execute(config,initialize)
    units={config['unit']:'bise_remote.worker',config['unit']+'-photos':'bise_remote.photo_recovery',config['unit']+'-normalize':'bise_remote.pipeline'}
    for unit,module in units.items():
        text=f'''[Unit]
Description={unit}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0
[Service]
User=bise
Group=bise
UMask=0077
WorkingDirectory={config['release']}
ExecStart={config['release']}/.venv/bin/python -u -m {module} --root {root}
Restart=on-failure
RestartSec=30
KillMode=mixed
TimeoutStopSec=90
MemoryMax={str(max(4096,profile['memory_mib']-3000))+'M' if module.endswith('worker') else '1300M' if module.endswith('photo_recovery') else '512M'}
CPUQuota={str(max(100,(profile['cores']-1)*100))+'%' if module.endswith('worker') else '75%' if module.endswith('photo_recovery') else '25%'}
TasksMax=768
StandardOutput=journal
[Install]
WantedBy=multi-user.target
'''
        pilot.guest(config,['tee',f'/etc/systemd/system/{unit}.service'],payload=text.encode())
    pilot.guest(config,['mkdir','-p','/etc/systemd/journald.conf.d'])
    pilot.guest(config,['tee','/etc/systemd/journald.conf.d/bise-storage.conf'],payload=b'[Journal]\nSystemMaxUse=128M\nRuntimeMaxUse=64M\n')
    pilot.guest(config,['systemctl','daemon-reload'])
    guardian=config['unit']+'-storage';script='/opt/'+guardian+'.py'
    service=f'''[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {script} --vmid {config['vmid']} --root {root} --marker {config['marker']} --hostname {config['name']}
TimeoutStartSec=60
MemoryMax=512M
'''
    timer=f'[Timer]\nOnBootSec=20\nOnUnitActiveSec=60\nAccuracySec=5\nUnit={guardian}.service\n[Install]\nWantedBy=timers.target\n'
    hostfiles={script:Path(__file__).with_name('guardian.py').read_bytes(),f'/etc/systemd/system/{guardian}.service':service.encode(),f'/etc/systemd/system/{guardian}.timer':timer.encode()}
    hostwrite='''import sys,json,pathlib,base64,os
for name,data in json.load(sys.stdin).items():
 p=pathlib.Path(name);temp=p.with_suffix('.tmp');temp.write_bytes(base64.b64decode(data));os.chmod(temp,0o644);os.replace(temp,p)
'''
    pilot.ssh(config['ssh_host'],['python3','-c',hostwrite],payload=json.dumps({k:base64.b64encode(v).decode() for k,v in hostfiles.items()}).encode())
    pilot.ssh(config['ssh_host'],['systemctl','daemon-reload']);pilot.ssh(config['ssh_host'],['systemctl','enable','--now',guardian+'.timer'])
    pilot.ssh(config['ssh_host'],['systemctl','start',guardian+'.service'])
    config.update(expected_records=runtime['expected_records'],workers=profile['workers'],phase='prepared',seed=info)
    atomic(folder/'deployment.json',config);return config


def execute(config,code,timeout=180):
    cmd='cd '+shlex.quote(config['release'])+' && '+shlex.join([config['release']+'/.venv/bin/python','-c',code])
    return pilot.guest(config,['runuser','-u','bise','--','sh','-c',cmd],timeout=timeout)


def prepare(profile):
    folder=state(profile);folder.mkdir(parents=True,exist_ok=True)
    with (folder/'operator.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        config=configure(profile);provision(config)
        for unit in (config['unit'],config['unit']+'-photos',config['unit']+'-normalize'):
            if pilot.guest(config,['systemctl','show',unit,'--property=ActiveState','--value']).decode().strip() in ('active','activating','deactivating'):
                raise ValueError('Stop this SSC workflow before prepare; its saved state is preserved')
        config=release(config);config=install(profile,config);install_relay(config);return config


def start(profile):
    config=deployment(profile)
    if config.get('source_sha256')!=hashlib.sha256(pilot.package()).hexdigest():raise ValueError('Code changed; stop and prepare this workflow first')
    passed=json.loads(execute(config,"import json,pathlib;print((pathlib.Path("+repr(config['root'])+")/'runtime/acceptance.json').read_text())"))
    if passed.get('status')!='passed':raise ValueError('Bounded server acceptance is required before full start')
    for unit in (config['unit'],config['unit']+'-photos',config['unit']+'-normalize'):
        pilot.guest(config,['systemctl','enable','--now',unit])
    pilot.ssh(config['ssh_host'],['systemctl','enable','--now',config['unit']+'-relay.timer'])
    print(json.dumps({'status':'started','vmid':config['vmid'],'workers':config['workers'],'headless':True,'photos':'separate_files','registration_enabled':False,'root':config['root']}))


def acceptance(config):
    pilot.guest(config,['systemd-run','--unit='+config['unit']+'-acceptance','--collect','--property=User=bise','--property=Group=bise',
      '--property=UMask=0077','--property=MemoryMax=2G','--property=CPUQuota=150%','--property=TimeoutStopSec=90',
      '--working-directory='+config['release'],config['release']+'/.venv/bin/python','-u','-m','bise_remote.acceptance','--root',config['root']])
    print('Bounded server pilot started: three new results and three prior-record photo lookups; no games registrations.')


def status(config):
    code='''import pathlib,json,subprocess,shutil
from bise_remote.common import counts
root=pathlib.Path(ROOT);p=root/'runtime/status.json'
value=json.loads(p.read_text()) if p.exists() else {'status':'prepared'}
value.update(title='Matric 9th 2026 • Server pipeline',counts=counts(root/'results.sqlite3'),workers=json.loads((root/'config.json').read_text())['workers'],disk_free_bytes=shutil.disk_usage(root).free)
value['service']={'ActiveState':subprocess.check_output(['systemctl','show',UNIT,'--property=ActiveState','--value']).decode().strip()}
for label,name in (('pipeline','pipeline.json'),('photo_recovery','photos.json')):
 p=root/'runtime'/name
 value[label]=json.loads(p.read_text()) if p.exists() else {}
import sqlite3
db=sqlite3.connect(root/'results.sqlite3');value['photos_saved']=db.execute('SELECT count(*) FROM photo_assets').fetchone()[0]
value['photo_states']=dict(db.execute('SELECT state,count(*) FROM photo_recovery GROUP BY state'));db.close()
p=root/'pipeline.sqlite3'
if p.exists():
 db=sqlite3.connect(p);value['delivery_states']=dict(db.execute('SELECT state,count(*) FROM ready_outbox GROUP BY state'))
 if db.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_receipts'").fetchone():value['central_receipts']=dict(db.execute('SELECT state,count(*) FROM delivery_receipts GROUP BY state'))
 db.close()
print(json.dumps(value))
'''.replace('ROOT',repr(config['root'])).replace('UNIT',repr(config['unit']))
    return json.loads(execute(config,code,timeout=30))


def stop(config):
    pilot.ssh(config['ssh_host'],['systemctl','disable','--now',config['unit']+'-relay.timer'])
    pilot.guest(config,['systemctl','disable','--now',config['unit'],config['unit']+'-photos',config['unit']+'-normalize'],timeout=120)
    print('SSC workflow stopped; saved results, files and checkpoints preserved. Inter registration is unchanged.')


def logs(config):control.logs(config)
def download(config):control.download({**config,'photo_storage':'files','download_folder':'remote_downloads/Matric 2026 - Part-I Annual'})


def install_relay(config):
    """Stage ready records beside the existing authority, without changing its worker."""
    from . import registration
    central=registration.deployment()
    # Verify the central guest via the host API rather than trusting a reused VMID.
    text=pilot.ssh(config['ssh_host'],['cat',f"/etc/pve/lxc/{central['vmid']}.conf"]).decode()
    from .guardian import owns_guest
    if not owns_guest(text):raise ValueError('Existing central registration guest ownership differs')
    archive=pilot.package();sha=hashlib.sha256(archive).hexdigest();release='/opt/bise-results/intake/'+sha[:20]
    pilot.guest(central,['mkdir','-p',release]);pilot.guest(central,['tar','xz','-C',release],payload=archive)
    photos=central['root']+'/source-media/ssc-2026-part1'
    pilot.guest(central,['install','-d','-o','bise','-g','bise','-m','700',photos])
    database=central['root']+'/state/registration.sqlite3'
    c={'source':{'vmid':config['vmid'],'marker':config['marker'],'root':config['root'],'release':config['release'],'python':config['release']+'/.venv/bin/python'},
       'central':{'vmid':central['vmid'],'marker':'bise-results:inter-2024-pilot','release':release,'python':central['release']+'/.venv/bin/python','photos':photos,'database':database}}
    unit=config['unit']+'-relay';base='/opt/'+unit
    service=f'[Service]\nType=oneshot\nExecStart=/usr/bin/python3 {base}.py --config {base}.json\nTimeoutStartSec=300\nMemoryMax=512M\nCPUQuota=50%\n'
    timer=f'[Timer]\nOnBootSec=40\nOnUnitActiveSec=20\nAccuracySec=2\nUnit={unit}.service\n[Install]\nWantedBy=timers.target\n'
    files={base+'.py':Path(__file__).with_name('relay.py').read_bytes(),base+'.json':json.dumps(c).encode(),
      f'/etc/systemd/system/{unit}.service':service.encode(),f'/etc/systemd/system/{unit}.timer':timer.encode()}
    code='''import sys,json,base64,pathlib,os
for name,data in json.load(sys.stdin).items():
 p=pathlib.Path(name);temp=p.with_suffix('.tmp');temp.write_bytes(base64.b64decode(data));os.chmod(temp,0o600 if name.endswith('.json') else 0o644);os.replace(temp,p)
'''
    pilot.ssh(config['ssh_host'],['python3','-c',code],payload=json.dumps({k:base64.b64encode(v).decode() for k,v in files.items()}).encode())
    pilot.ssh(config['ssh_host'],['systemctl','daemon-reload'])
    atomic(state({'job':config['job']})/'relay.json',{'central_vmid':central['vmid'],'database':database,'photos':photos,'unit':unit,'registration_started':False})
