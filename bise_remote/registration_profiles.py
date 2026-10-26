"""Separate profile-owned receipts while preserving legacy pilot evidence."""
import json
import os
from pathlib import Path


def isolate_outputs(root):
    root=Path(root)
    changes=[]
    for filename,label in (('register.json','Full'),('register-pilot.json','Pilot')):
        path=root/'config'/filename
        config=json.loads(path.read_text())
        legacy=root/'registration_output'
        target=legacy/label
        if Path(config['state']['output']) not in (legacy,target):
            raise ValueError('Unexpected registration output path; refusing migration')
        if Path(config['state']['output'])==target:
            continue
        config['state']['output']=str(target)
        temp=path.with_suffix('.output-migration.tmp')
        with temp.open('w') as stream:
            os.chmod(temp,0o600)
            json.dump(config,stream,indent=2);stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        temp.replace(path)
        changes.append(filename)
    return changes
