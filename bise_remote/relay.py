"""Host-side authenticated local LXC relay. No public ingress or portal calls."""
import argparse
import json
from pathlib import Path
import subprocess
import shlex
import time


def invoke(c,arguments,payload=None):
    command='cd '+shlex.quote(c['release'])+' && '+shlex.join([c['python'],'-m','bise_remote.handoff',*arguments])
    result=subprocess.run(['pct','exec',str(c['vmid']),'--','runuser','-u','bise','--','sh','-c',command],
        input=payload,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
    if result.returncode:raise RuntimeError(result.stderr.decode()[-1000:])
    return json.loads(result.stdout)


def run(config):
    # Native root-owned service configuration pins both approved guest identities.
    from urllib.parse import unquote
    for c in (config['source'],config['central']):
        text=Path(f"/etc/pve/lxc/{c['vmid']}.conf").read_text()
        description=unquote('\n'.join(s[1:] for s in text.splitlines() if s.startswith('#'))).strip()
        if description!=c['marker']:raise ValueError('Relay guest ownership differs')
    batch=invoke(config['source'],['export','--root',config['source']['root'],'--limit','50'])
    if not batch['entries']:return {'at':time.time(),'delivered':0,'status':'caught_up'}
    receipt=invoke(config['central'],['ingest','--root',config['central']['photos'],'--database',config['central']['database']],json.dumps(batch).encode())
    invoke(config['source'],['ack','--root',config['source']['root']],json.dumps(receipt).encode())
    return {'at':time.time(),'delivered':len(receipt['receipts']),'status':'delivered','registration_started':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(run(json.loads(args.config.read_text()))))
