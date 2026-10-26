"""Small host-side storage guardian; no portal requests or database writes."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time
from urllib.parse import unquote


def storage_safe(pool_free, root_free, data_percent, metadata_percent):
    return (pool_free >= 64*1024**3 and root_free >= 8*1024**3
            and 0 <= data_percent < 85 and 0 <= metadata_percent < 75)


def command(argv):
    return subprocess.check_output(argv,timeout=12,text=True)


def owns_guest(text):
    config=dict(line.split(':',1) for line in text.splitlines() if ':' in line and not line.startswith('#'))
    # pmxcfs stores the description as URI-encoded leading comments, whereas
    # the Proxmox API exposes a decoded "description" property.
    description=config.get('description') or '\n'.join(line[1:] for line in text.splitlines() if line.startswith('#'))
    return (unquote(description).strip()=='bise-results:inter-2024-pilot'
            and config.get('hostname','').strip()=='bise-inter-2024-pilot'
            and config.get('unprivileged','').strip()=='1')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--vmid',type=int,required=True)
    parser.add_argument('--root',required=True);args=parser.parse_args()
    root=Path(args.root)
    if not root.is_absolute() or '..' in root.parts or not root.is_relative_to('/var/lib/bise-results/jobs'):
        raise ValueError('Invalid collector job root')
    # Read the local pmxcfs ownership marker; don't spawn the large Perl API
    # client just to inspect a small config file on this same host.
    if not owns_guest(Path(f'/etc/pve/lxc/{args.vmid}.conf').read_text()):
        raise ValueError('Guardian refuses an unrelated guest')
    health={'observed_at':time.time(),'safe':False}
    try:
        thin=json.loads(command(['lvs','--reportformat','json','--units','b','--nosuffix',
                                 '-o','lv_size,data_percent,metadata_percent','pve/data']))['report'][0]['lv'][0]
        free=shutil.disk_usage('/').free
        data,metadata=float(thin['data_percent']),float(thin['metadata_percent'])
        pool_free=int(float(thin['lv_size'])*(1-data/100))
        health.update(pool_free_bytes=pool_free,host_root_free_bytes=free,
                      thin_data_percent=data,thin_metadata_percent=metadata,
                      safe=storage_safe(pool_free,free,data,metadata))
    except (OSError,ValueError,KeyError,IndexError,subprocess.SubprocessError) as exc:
        health['reason']=type(exc).__name__
    script='''import json,os,tempfile,sys,pathlib
root=pathlib.Path(sys.argv[1]);value=json.load(sys.stdin)
fd,name=tempfile.mkstemp(prefix='.host-storage-',dir=root/'runtime')
with os.fdopen(fd,'w') as stream:
 json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
os.replace(name,root/'runtime/host-storage.json')
'''
    subprocess.run(['pct','exec',str(args.vmid),'--','runuser','-u','bise','--','python3','-c',script,str(root)],
                   input=json.dumps(health),text=True,check=True,timeout=30)
    print(json.dumps(health),flush=True)


if __name__=='__main__':main()
