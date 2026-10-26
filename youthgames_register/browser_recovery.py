"""Local, evidence-bound recovery of browser refusals and blocked dispatches."""

import json
from pathlib import Path

from .config import sha, signature
from .http_recovery import recover_rate_limited, cooldown_remaining
from .store import check_receipt, jobs, now


STOP_DETAIL = 'SubmissionStopped: Run stopped by another worker; no new submission allowed'


def recover_stopped_dispatches(db, config, job_ids=None):
    recovered = []
    for job in jobs(db, config['profile'], ('blocked_input',)):
        if job_ids is not None and job['id'] not in job_ids:
            continue
        if job['tracking'] or job['detail'] != STOP_DETAIL or not check_receipt(job['receipt']):
            continue
        original = Path(job['receipt'])
        saved = json.loads(original.read_text())
        attempt = db.execute('SELECT * FROM attempts WHERE job=? ORDER BY id DESC LIMIT 1', (job['id'],)).fetchone()
        if (not attempt or attempt['dispatched_at'] is not None or not attempt['intent_at']
                or attempt['outcome'] != 'blocked_input' or attempt['evidence'] != str(original)
                or saved.get('application') != 'youthgames_register'
                or saved.get('job') != job['id'] or saved.get('attempt') != attempt['id']
                or saved.get('payload_hash') != job['payload_hash']
                or saved.get('post_dispatched') is not False
                or saved.get('outcome', {}).get('state') != 'blocked_input'
                or saved.get('outcome', {}).get('detail') != STOP_DETAIL):
            continue
        # This exact detail is recorded when the shared stop guard raises inside
        # PostGate before dispatched() and route.continue_(). A request_failed
        # event alone would NOT prove non-delivery and is never sufficient here.
        derived = {**saved, 'reconciled_at': now(),
            'recovery': 'Shared stop guard blocked dispatch; no applicant POST left the gate',
            'files': {**saved['files'], 'original-receipt.json': {
                'path': str(original.resolve()), 'sha256': sha(original.read_bytes())}}}
        target = original.with_name('stopped-before-dispatch-' + signature(derived['recovery'])[:16] + '.json')
        if not target.exists():
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(derived, indent=2) + '\n')
            temporary.replace(target)
        audited = json.loads(target.read_text())
        if (not check_receipt(target) or audited.get('job') != job['id']
                or audited.get('attempt') != attempt['id']
                or audited.get('payload_hash') != job['payload_hash']
                or audited.get('outcome') != saved['outcome']
                or audited.get('post_dispatched') is not False):
            raise ValueError('Stopped dispatch recovery receipt failed integrity checks')
        with db:
            changed = db.execute('INSERT OR IGNORE INTO submission_recoveries VALUES (?,?,?,?,?,?)',
                (attempt['id'], job['id'], 'browser_guard_stopped_before_dispatch', now(), str(target), now())).rowcount
            if changed:
                db.execute("UPDATE jobs SET state='ready',detail=?,receipt=?,updated_at=? WHERE id=? AND state='blocked_input'",
                    ('Shared stop prevented dispatch; safe to resume with a fresh attempt', str(target), now(), job['id']))
        if changed:
            recovered.append({'job': job['id'], 'reason': 'browser_guard_stopped_before_dispatch'})
    return recovered


def recover_browser_jobs(db, config):
    """Idempotent local repair; no portal access and no applicant POSTs."""
    throttled = recover_rate_limited(db, config, transports=('http', 'playwright'))
    stopped = recover_stopped_dispatches(db, config)
    from .name_recovery import recover_name_formatting
    names = recover_name_formatting(db, config)
    return {'recovered_throttles': throttled, 'recovered_stopped_dispatches': stopped,
        'recovered_name_formats':names,
        'cooldown_seconds': cooldown_remaining(db, config['portal'].split('#')[0].rstrip('/'), config['campaign']),
        'applicant_posts': 0}
