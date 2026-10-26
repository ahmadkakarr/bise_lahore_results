"""Browser-free, pinned one-per-UC HTTP verification using the shared ledger."""

import csv
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from bise_normalize import COLUMNS
from bise_normalize.publish import locked
from youthgames_register import http_pilot
from youthgames_register.config import input_snapshot, load as register_config, resolved, sha
from youthgames_register.store import check_receipt
from . import app
from .store import opened


def pilot_profile(profile, pilots):
    """Seal exactly these 20 confirmed rows independently of the changing master."""
    c=app.load(profile);p=app.paths(c,profile)
    path=p['registration_profile'].with_name(p['registration_profile'].stem+'_http_pilot.json')
    if path.exists():
        config=register_config(path)
        if set(config['selection']['rolls']) != {r['roll'] for r in pilots}:
            raise ValueError('Existing HTTP pilot profile has different pinned identities')
        return path
    base=register_config(p['registration_profile'])
    selected={row['roll'] for row in pilots}
    destination=p['allocation_database'].parent/'registration_inputs'/(c['scheme']+' HTTP Pilot')
    with locked(destination), input_snapshot(base,p['registration_profile']) as (rows,origins,_,hashes):
        pairs=[(row,origin) for row,origin in zip(rows,origins) if origin['roll'] in selected]
        if len(pairs)!=len(selected) or len({row['BForm'] for row,_ in pairs})!=len(pairs):
            raise ValueError('HTTP pilot source is incomplete or has duplicate identities')
        if any(not origin['portal']['ready'] or origin.get('issues') or origin['portal']['age']['state']!='eligible' for _,origin in pairs):
            raise ValueError('HTTP pilot contains a blocked source row')
        destination.parent.mkdir(parents=True,exist_ok=True)
        work=Path(tempfile.mkdtemp(prefix='.http-pilot-',dir=destination.parent))
        try:
            (work/'Data').mkdir()
            with (work/'Ready to Register.csv').open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=COLUMNS);writer.writeheader();writer.writerows(row for row,_ in pairs)
            (work/'Data/ready-to-register-provenance.jsonl').write_text(''.join(json.dumps(origin,ensure_ascii=False)+'\n' for _,origin in pairs))
            files={name:sha((work/name).read_bytes()) for name in ('Ready to Register.csv','Data/ready-to-register-provenance.jsonl')}
            (work/'Data/manifest.json').write_text(json.dumps({'application':'bise_allocate','scope':'pinned one-per-UC HTTP pilot',
                'source_hashes':hashes,'rolls':sorted(selected),'files':files},indent=2)+'\n')
            if destination.exists():
                old=json.loads((destination/'Data/manifest.json').read_text())
                if old!=json.loads((work/'Data/manifest.json').read_text()) or any(sha((destination/name).read_bytes())!=digest for name,digest in files.items()):
                    raise ValueError('Existing HTTP pilot input differs; it was preserved')
            else:work.rename(destination)
        finally:
            if work.exists():shutil.rmtree(work)
    config=deepcopy(base)
    config['profile']=c['scheme']+' HTTP Pilot'
    config['selection']['rolls']=sorted(selected)
    config['input']={'csv':os.path.relpath(destination/'Ready to Register.csv',path.parent),
        'provenance':os.path.relpath(destination/'Data/ready-to-register-provenance.jsonl',path.parent),
        'manifest':os.path.relpath(destination/'Data/manifest.json',path.parent),
        'expected_csv_sha256':files['Ready to Register.csv']}
    config['confirmation']['input_sha256']=files['Ready to Register.csv']
    for section,key in [('photos','shared_file'),('state','database')]:
        config[section][key]=os.path.relpath(resolved(p['registration_profile'],base[section][key]),path.parent)
    config['state'].update(output=os.path.relpath(p['output'].parent.parent/'registration_output'/config['profile'],path.parent),write_reports=False)
    config['browser']['workers']=1  # This bounded HTTP test is sequential.
    app.write_profile(path,config)
    register_config(path)
    return path


def verification(profile,pilots):
    from youthgames_register.domain import classify
    checks=[]
    for selected in pilots:
        row=app.progress(profile,selected['roll'])['records'][0]
        valid=bool(row['receipt']) and check_receipt(row['receipt'])
        receipt=json.loads(Path(row['receipt']).read_text()) if valid else {}
        response=next((v['path'] for k,v in receipt.get('files',{}).items() if k=='response.html'),None)
        classified=classify(Path(response).read_text(),receipt['http_status']) if response else {}
        checks.append({'uc_number':selected['uc_number'],'roll':selected['roll'],'registration_job':row['registration_job'],
            'state':row['registration_state'],'tracking_saved':bool(row['tracking']),
            'tracking_matches_response':bool(row['tracking']) and classified.get('tracking')==row['tracking'],
            'transport':receipt.get('transport'),'receipt_valid':valid,
            'assigned_uc_matches_submitted':row['uc']==row['submitted_uc'] and row['tehsil']==row['submitted_tehsil']})
    return checks


def run(profile,client_factory=None,sleep=time.sleep,emit=None):
    """Exactly one new pinned identity per UC; no browser launch or POST retry."""
    c=app.load(profile);p=app.paths(c,profile)
    client_factory=client_factory or http_pilot.Client
    # Offline sync: normalization and pinned hashes only; no browser preparation.
    app.sync(profile)
    pilots=app.select_pilots(profile,transport='http')
    path=pilot_profile(profile,pilots)
    config=register_config(path)
    checks=verification(profile,pilots)
    if emit:emit('queue',total=sum(row['state']!='registered' for row in checks))
    if not all(row['state']=='registered' for row in checks):
        http_pilot.prepare(path,client_factory())
    outcomes=[];previous_finish=None
    for selected in pilots:
        current=app.progress(profile,selected['roll'])['records'][0]
        if current['registration_state']=='registered':continue
        if previous_finish is not None:
            sleep(max(0,config['pacing']['submission_interval_seconds']-(time.monotonic()-previous_finish)))
        if emit:emit('submitting',worker=1,uc=selected['uc_number'],roll=selected['roll'])
        else:print(json.dumps({'uc':selected['uc_number'],'transport':'http','phase':'submitting'}),flush=True)
        result=http_pilot.run(path,client_factory(),roll=selected['roll'])
        previous_finish=time.monotonic()
        outcomes.append(result)
        if emit:emit('result',worker=1,uc=selected['uc_number'],roll=selected['roll'],state=result.get('state',result.get('status')))
        else:print(json.dumps({'uc':selected['uc_number'],'transport':'http','state':result.get('state',result.get('status')),
                              'post_attempts':result.get('post_attempts',0)}),flush=True)
        if result.get('state')!='registered':break
    checks=verification(profile,pilots)
    report=app.progress(profile)
    report.update(transport='http',browser_launched=False,pilot_checks=checks,
        attempted_now=sum(result.get('post_attempts',0) for result in outcomes),
        pilot_passed=len(checks)==len(c['uc_numbers']) and all(x['state']=='registered' and x['tracking_saved'] and
            x['tracking_matches_response'] and x['transport']=='http' and x['receipt_valid'] and x['assigned_uc_matches_submitted'] for x in checks))
    destination=resolved(path,config['state']['output']);destination.mkdir(parents=True,exist_ok=True)
    (destination/'HTTP UC Pilot Verification.json').write_text(json.dumps(report,indent=2)+'\n')
    report['report']=str(destination/'HTTP UC Pilot Verification.json')
    return report
