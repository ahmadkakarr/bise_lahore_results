"""Bounded, operator-run HSSC pilot in a dedicated unprivileged Proxmox LXC.

No bulk job, games submission, host networking edits or existing guest changes.
"""
import argparse
from contextlib import closing
import fcntl
import gzip
import hashlib
import io
import inspect
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile

from .common import atomic, load, digest

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT/'artifacts/inter-2024-server-pilot'
HOST_OPTIONS = ['-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                '-o','ConnectTimeout=10','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3']
GUEST_ROOT='/var/lib/bise-results/pilot-inter-2024'


def ssh(host, command, *, payload=None, timeout=180):
    result=subprocess.run(['ssh',*HOST_OPTIONS,host,shlex.join(command)],input=payload,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-3000:] or result.stdout.decode(errors='replace')[-3000:])
    return result.stdout


def guest(config, command, **kwargs):
    return ssh(config['ssh_host'],['pct','exec',str(config['vmid']),'--',*command],**kwargs)


def inventory(host):
    script='''import json,subprocess
rows=json.loads(subprocess.check_output(['pvesh','get','/cluster/resources','--type','vm','--output-format','json']))
configs=[]
for r in rows:
 kind='lxc' if r['type']=='lxc' else 'qemu'
 c=json.loads(subprocess.check_output(['pvesh','get',f"/nodes/{r['node']}/{kind}/{r['vmid']}/config",'--output-format','json']))
 configs.append(c)
print(json.dumps({'resources':rows,'configs':configs}))
'''
    return json.loads(ssh(host,['python3','-'],payload=script.encode()))


def configure(profile):
    platform_root=Path(profile['infrastructure_root'])
    runtime_settings={key:profile[key] for key in ('workers','ocr_workers','interval_seconds')}
    sys.path.insert(0,str(platform_root/'platform'))
    from android_builder.allocation import choose_vmid, choose_ipv4, reserve_assignment
    from android_builder.autodiscovery import extract_ipv4_claims
    state_dir=platform_root/'.state/android-builder';state_dir.mkdir(parents=True,exist_ok=True)
    with (state_dir/'runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        config_path=STATE/'deployment.json'
        if config_path.exists():
            config=json.loads(config_path.read_text())
            config.update(runtime_settings)
            atomic(config_path,config)
            return config
        inv=inventory(profile['ssh_host'])
        owner=str(config_path.resolve())
        vmid=choose_vmid(platform_root,inv['resources'],'test',950,owner=owner)
        live=[ip for cfg in inv['configs'] for ip in extract_ipv4_claims(cfg)]
        address=choose_ipv4(platform_root,live,'test',owner=owner)
        reserve_assignment(platform_root,config_path,vmid,address)
        config={'vmid':vmid,'address':address,'ssh_host':profile['ssh_host'],
                'name':'bise-inter-2024-pilot','storage':'local-lvm',
                'template':'local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst',
                'input_sha256':digest(profile['input']),'phase':'reserved',**runtime_settings}
        atomic(config_path,config)
        # This standalone pilot uses pct, not the platform's generic Ansible
        # service controller. Keep it out of that controller's instance catalog.
        atomic(platform_root/'.state/bise-results/pilot.json',{
            'id':'bise-inter-2024-pilot','service':'bise-results','profile':'bounded-inter-pilot',
            'containers':{'collector':{'vm_id':vmid,'hostname':config['name'],'address':address}},
            'collection_only':True,'pilot_limit':4})
        return config


def package():
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w') as archive:
        def add(path):
            if path.is_symlink():raise ValueError('Refusing a symlink in remote source package')
            info=archive.gettarinfo(str(path),arcname=str(path.relative_to(ROOT)))
            info.uid=info.gid=info.mtime=0;info.uname=info.gname='';info.mode=0o644;info.pax_headers={}
            with path.open('rb') as stream:archive.addfile(info,stream)
        for directory in ('bise_results','bise_remote','run_alarm','bise_normalize','bise_allocate','youthgames_register','youthgames_shared'):
            for path in sorted((ROOT/directory).rglob('*.py')):
                add(path)
        for directory in ('bise_normalize','youthgames_register'):
            add(ROOT/directory/'profile.template.json')
        add(ROOT/'requirements.lock.txt')
    return gzip.compress(buffer.getvalue(),mtime=0)


def validate_guest_identity(config, current):
    # Proxmox serializes description text with a trailing newline.
    if (str(current.get('description','')).strip() != 'bise-results:inter-2024-pilot'
            or current.get('hostname') != config['name'] or not current.get('unprivileged')):
        raise ValueError('Guest ownership differs; refusing mutation')
    net=dict(item.split('=',1) for item in current.get('net0','').split(',') if '=' in item)
    if net.get('ip') != config['address'] or not current.get('rootfs','').startswith(config['storage']+':'):
        raise ValueError('Guest placement differs; refusing mutation')


def setup(config):
    # Host lock + ownership marker prevent accidental use of an unrelated VMID.
    payload=json.dumps(config)
    script='''import json,subprocess,fcntl,shutil
c=json.loads(CONFIG)
def run(*args):return subprocess.check_output(list(map(str,args)),stderr=subprocess.STDOUT).decode()
with open('/run/lock/bise-inter-pilot.lock','a') as guard:
 fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
 rows=json.loads(run('pvesh','get','/cluster/resources','--type','vm','--output-format','json'))
 existing=next((r for r in rows if r['vmid']==c['vmid']),None)
 marker='bise-results:inter-2024-pilot'
 if existing:
  if existing['type']!='lxc':raise RuntimeError('Reserved VMID belongs to another guest')
  current=json.loads(run('pvesh','get',f"/nodes/{existing['node']}/lxc/{c['vmid']}/config",'--output-format','json'))
  validate_guest_identity(c,current)
 else:
  storage=json.loads(run('pvesh','get','/nodes/pve/storage','--output-format','json'))
  pool=next(v for v in storage if v['storage']==c['storage'])
  if pool.get('avail',0)<64*1024**3 or shutil.disk_usage('/').free<8*1024**3:raise RuntimeError('Host storage reserve too low')
  ip=c['address'].split('/')[0]
  # Do not claim a LAN address already responding outside the managed catalog.
  probe=subprocess.run(['ping','-c','2','-W','1',ip],stdout=subprocess.DEVNULL)
  if probe.returncode==0:raise RuntimeError('Candidate IP responds; inspect placement')
  run('pct','create',c['vmid'],c['template'],'--hostname',c['name'],'--description',marker,
      '--unprivileged','1','--cores','4','--memory','6144','--swap','512',
      '--rootfs',c['storage']+':32','--net0',f"name=eth0,bridge=vmbr0,ip={c['address']},gw=192.168.10.1,firewall=1",
      '--nameserver','1.1.1.1','--features','nesting=1','--onboot','1','--tags','bise-results;bounded-pilot')
 if 'running' not in run('pct','status',c['vmid']):run('pct','start',c['vmid'])
print(json.dumps({'vmid':c['vmid'],'status':'guest_ready'}))
'''.replace('CONFIG',repr(payload))
    script=inspect.getsource(validate_guest_identity)+'\n'+script
    print(ssh(config['ssh_host'],['python3','-'],payload=script.encode(),timeout=180).decode(),flush=True)
    install='''set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv tesseract-ocr chromium ca-certificates
id bise >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/bise-results --shell /usr/sbin/nologin bise
install -d -o bise -g bise -m 700 /var/lib/bise-results/pilot-inter-2024 /var/lib/bise-results/pilot-inter-2024/input /var/lib/bise-results/pilot-inter-2024/evidence
install -d -m 755 /opt/bise-results/releases
'''
    output=guest(config,['bash','-s'],payload=install.encode(),timeout=1200)
    (STATE/'install.log').write_bytes(output)
    archive=package();sha=hashlib.sha256(archive).hexdigest();release='/opt/bise-results/releases/'+sha[:20]
    guest(config,['mkdir','-p',release])
    guest(config,['tar','xz','-C',release],payload=archive)
    guest(config,['python3','-m','venv',release+'/.venv'])
    output=guest(config,[release+'/.venv/bin/pip','install','--disable-pip-version-check','-r',release+'/requirements.lock.txt'],timeout=600)
    (STATE/'pip.log').write_bytes(output)
    guest(config,['chown','-R','root:root',release])
    config.update(release=release,source_sha256=sha,phase='installed')
    atomic(STATE/'deployment.json',config)
    return config


def pilot(config):
    if not config.get('release'):raise ValueError('Run setup first')
    active=guest(config,['systemctl','show','bise-inter-2024-pilot','--property=ActiveState','--value']).decode().strip()
    if active in ('active','activating','deactivating'):
        print('Pilot is already running; no new collector started.',flush=True);return
    csv=b'Roll Number\n500001\n500002\n500003\n500004\n'
    guest(config,['tee',GUEST_ROOT+'/input/rolls.csv'],payload=csv)
    guest(config,['chown','bise:bise',GUEST_ROOT+'/input/rolls.csv'])
    cmd=['systemd-run','--unit=bise-inter-2024-pilot','--collect',
         '--property=User=bise','--property=Group=bise','--property=UMask=0077',
         '--property=MemoryMax=5G','--property=CPUQuota=300%',
         '--property=TasksMax=512','--property=TimeoutStopSec=60',
         '--working-directory='+config['release'],config['release']+'/.venv/bin/python','-u','-m','bise_results',
         'collect',GUEST_ROOT+'/input/rolls.csv','--class','12','--year','2024','--limit','4',
         '--workers',str(config['workers']),'--ocr-workers',str(config['ocr_workers']),
         '--interval',str(config['interval_seconds']),'--headless','--captcha-mode','auto',
         '--browser-executable','/usr/bin/chromium','--database',GUEST_ROOT+'/results.sqlite3',
         '--artifacts',GUEST_ROOT+'/evidence','--min-available-memory-mb','512','--min-free-disk-gib','4',
         '--missing-dob-policy','estimate','--estimated-age-at-exam','18','--save-student-photos','--no-alarm']
    print(guest(config,cmd).decode(),flush=True)
    print('Pilot started independently of SSH. Only four supplied rolls; reruns skip saved rows.',flush=True)


def status(config):
    print(guest(config,['systemctl','show','bise-inter-2024-pilot','--property=ActiveState,SubState,ExecMainStatus']).decode())
    print(guest(config,['journalctl','-u','bise-inter-2024-pilot','-n','30','--no-pager','-o','cat']).decode())


def download(config):
    # SQLite online backup includes committed WAL, and photos live in the same DB.
    script='''import sqlite3,os,json,hashlib
root=ROOT
source=sqlite3.connect('file:'+root+'/results.sqlite3?mode=ro',uri=True)
target=sqlite3.connect(root+'/snapshot.sqlite3.tmp')
source.backup(target)
assert target.execute('pragma quick_check').fetchone()[0]=='ok'
target.execute('pragma journal_mode=DELETE')
target.close();source.close();os.replace(root+'/snapshot.sqlite3.tmp',root+'/snapshot.sqlite3')
'''.replace('ROOT',repr(GUEST_ROOT))
    guest(config,['runuser','-u','bise','--','python3','-'],payload=script.encode())
    remote_sha=guest(config,['sha256sum',GUEST_ROOT+'/snapshot.sqlite3']).decode().split()[0]
    payload=guest(config,['cat',GUEST_ROOT+'/snapshot.sqlite3'])
    if hashlib.sha256(payload).hexdigest()!=remote_sha:raise ValueError('Downloaded snapshot checksum differs from server')
    target=STATE/'results.sqlite3';temp=target.with_suffix('.download')
    temp.write_bytes(payload)
    import sqlite3
    with closing(sqlite3.connect(temp)) as db:
        if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Downloaded DB integrity failed')
        db.execute('PRAGMA journal_mode=DELETE')
    temp.replace(target)
    atomic(STATE/'download.json',{'database':str(target),'sha256':digest(target),'bytes':len(payload),
                               'photos_included':True,'source_sha256':config['source_sha256']})
    print(json.dumps({'database':str(target),'sha256':digest(target),'bytes':len(payload)}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('setup','start','status','download'))
    parser.add_argument('--profile',type=Path,default=ROOT/'remote_profiles/inter-2024-part2.json')
    args=parser.parse_args();STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (STATE/'operator.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        profile=load(args.profile)
        if args.action=='setup':setup(configure(profile));return
        config=json.loads((STATE/'deployment.json').read_text())
        {'start':pilot,'status':status,'download':download}[args.action](config)


if __name__=='__main__':main()
