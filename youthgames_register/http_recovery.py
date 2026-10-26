"""Recover hash-verified explicit throttling refusals, never lost POSTs."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .config import sha, signature
from .domain import classify
from .store import check_receipt, jobs, now


def recover_rate_limited(db, config, job_ids=None, transports=('http',)):
    recovered = []
    for job in jobs(db, config['profile'], ('rejected',)):
        if job_ids is not None and job['id'] not in job_ids:
            continue
        if job['tracking'] or not check_receipt(job['receipt']):
            continue
        original = Path(job['receipt'])
        saved = json.loads(original.read_text())
        attempt = db.execute('SELECT * FROM attempts WHERE job=? ORDER BY id DESC LIMIT 1', (job['id'],)).fetchone()
        # Older browser receipts predate the explicit transport field. Their
        # application name distinguishes them from HTTP adapter receipts.
        transport = saved.get('transport')
        if transport is None and saved.get('application') == 'youthgames_register':
            transport = 'playwright'
        if (not attempt or saved.get('attempt') != attempt['id'] or saved.get('job') != job['id']
                or saved.get('payload_hash') != job['payload_hash'] or not saved.get('post_dispatched')
                or transport not in transports or not attempt['dispatched_at']
                or attempt['outcome'] != 'rejected' or attempt['evidence'] != str(original)):
            continue
        result = classify(Path(saved['files']['response.html']['path']).read_text(), saved['http_status'])
        if not result.get('rate_limited'):
            continue
        captured = datetime.fromisoformat(saved['captured_at'])
        if captured.tzinfo is None:
            continue
        not_before = captured + timedelta(seconds=result['retry_after_seconds'] + 2)
        derived = {**saved, 'outcome': result, 'reconciled_at': now(),
            'recovery': 'Explicit server throttle refusal; new attempt allowed after cooldown',
            'not_before': not_before.isoformat(),
            'files': {**saved['files'], 'original-receipt.json': {'path': str(original.resolve()), 'sha256': sha(original.read_bytes())}}}
        target = original.with_name('throttle-reconciled-' + signature(result)[:16] + '.json')
        if not target.exists():
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(derived, indent=2) + '\n')
            temporary.replace(target)
        audited = json.loads(target.read_text())
        if (not check_receipt(target) or audited.get('outcome') != result
                or audited.get('job') != job['id'] or audited.get('attempt') != attempt['id']
                or audited.get('payload_hash') != job['payload_hash']):
            raise ValueError('Derived throttle recovery receipt failed integrity checks')
        with db:
            changed = db.execute('INSERT OR IGNORE INTO submission_recoveries VALUES (?,?,?,?,?,?)',
                (attempt['id'], job['id'], 'explicit_server_rate_limit', not_before.isoformat(), str(target), now())).rowcount
            if changed:
                db.execute("UPDATE jobs SET state='ready',detail=?,receipt=?,updated_at=? WHERE id=? AND state='rejected'",
                    (result['detail'], str(target), now(), job['id']))
        if changed:
            recovered.append({'job': job['id'], 'detail': result['detail'],
                'wait_seconds': max(0, (not_before - datetime.now(timezone.utc)).total_seconds())})
    return recovered


def cooldown_remaining(db, portal, campaign, connection_key=None):
    sql='''SELECT r.not_before FROM submission_recoveries r JOIN jobs j ON j.id=r.job
        LEFT JOIN attempt_connections c ON c.attempt=r.attempt WHERE j.portal=? AND j.campaign=?'''
    args=[portal,campaign]
    if connection_key:
        sql+=' AND (c.connection_key=? OR c.connection_key IS NULL)';args.append(connection_key)
    rows = db.execute(sql,args)
    current = datetime.now(timezone.utc)
    return max([0] + [(datetime.fromisoformat(row[0]) - current).total_seconds() for row in rows])
