"""Recover only an explicit, receipt-bound rejection with a safe name-format fix."""

import json
from pathlib import Path

from youthgames_shared.names import name_changes,NAME_POLICY
from .config import sha,signature
from .domain import classify
from .store import jobs,check_receipt,now


NAME_ERRORS={
    'The father name may only contain letters, spaces, apostrophes, periods, and hyphens.':'FatherName',
    'The first name may only contain letters, spaces, apostrophes, periods, and hyphens.':'FullName',
    'The full name may only contain letters, spaces, apostrophes, periods, and hyphens.':'FullName',
}


def recover_name_formatting(db,config):
    recovered=[]
    if not db.execute('SELECT 1 FROM profiles WHERE name=?',(config['profile'],)).fetchone():return recovered
    for job in jobs(db,config['profile'],('rejected',)):
        if job['tracking'] or not check_receipt(job['receipt']):continue
        original=Path(job['receipt']);saved=json.loads(original.read_text())
        attempt=db.execute('SELECT * FROM attempts WHERE job=? ORDER BY id DESC LIMIT 1',(job['id'],)).fetchone()
        if (not attempt or not attempt['dispatched_at'] or attempt['outcome']!='rejected'
                or attempt['evidence']!=str(original) or saved.get('job')!=job['id']
                or saved.get('attempt')!=attempt['id'] or saved.get('payload_hash')!=job['payload_hash']
                or saved.get('post_dispatched') is not True
                or saved.get('outcome',{}).get('state')!='rejected'
                or saved.get('application')!='youthgames_register'):continue
        try:
            result=classify(Path(saved['files']['response.html']['path']).read_text(),saved['http_status'])
            proof_entry=saved['files'].get('http-request-proof.json')
            proof=json.loads(Path(proof_entry['path']).read_text()) if proof_entry else {}
        except (KeyError,TypeError,ValueError,OSError):continue
        messages=result['detail'].split('; ')
        if result['state']!='rejected' or not messages or any(message not in NAME_ERRORS for message in messages):continue
        # A separator correction already sent and rejected is a new validation
        # problem, not permission to repeat the same corrected POST on restart.
        if any(NAME_ERRORS[message] in proof.get('name_formatting',{}) for message in messages):continue
        data=json.loads(job['payload'])['data']
        try:changes=name_changes(data)
        except ValueError:continue
        if not changes or any(NAME_ERRORS[message] not in changes for message in messages):continue
        # Keep the original payload/attempt untouched. Normalization or the wire
        # adapter will apply the same letter-preserving formatting policy.
        derived={**saved,'reconciled_at':now(),'recovery':'explicit_name_separator_rejection',
            'name_policy':NAME_POLICY,'proposed_name_formatting':changes,
            'files':{**saved['files'],'original-receipt.json':{
                'path':str(original.resolve()),'sha256':sha(original.read_bytes())}}}
        target=original.with_name('name-format-reconciled-'+signature({'policy':NAME_POLICY,'changes':changes})[:16]+'.json')
        if not target.exists():
            temporary=target.with_suffix('.tmp');temporary.write_text(json.dumps(derived,indent=2)+'\n');temporary.replace(target)
        audited=json.loads(target.read_text())
        if (not check_receipt(target) or audited.get('proposed_name_formatting')!=changes
                or audited.get('payload_hash')!=job['payload_hash'] or audited.get('attempt')!=attempt['id']):
            raise ValueError('Name-format recovery receipt failed integrity checks')
        with db:
            changed=db.execute('INSERT OR IGNORE INTO submission_recoveries VALUES (?,?,?,?,?,?)',
                (attempt['id'],job['id'],'explicit_name_separator_rejection',now(),str(target),now())).rowcount
            if changed:
                updated=db.execute("UPDATE jobs SET state='ready',detail=?,receipt=?,updated_at=? WHERE id=? AND state='rejected' AND payload_hash=? AND receipt=?",
                    ('Verified explicit name-format rejection; safe separator-only correction available',str(target),now(),job['id'],job['payload_hash'],str(original))).rowcount
                if updated!=1:raise ValueError('Name-format recovery lost its original rejected snapshot')
        if changed:recovered.append({'job':job['id'],'fields':sorted(changes),'policy':NAME_POLICY})
    return recovered
