import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            h.update(block)
    return h.hexdigest()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(handle, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9-]{1,63}', value):
        raise ValueError('Invalid job identifier')
    return value


def load(path):
    path = Path(path).resolve()
    c = json.loads(path.read_text())
    safe_id(c['job'])
    if c.get('schema_version') != 1 or c.get('class') != 12 or c.get('year') != 2024:
        raise ValueError('This remote profile requires HSSC Part-II Annual 2024')
    if not 1 <= c['workers'] <= 18 or not 1 <= c['ocr_workers'] <= 8:
        raise ValueError('Invalid worker limits')
    if c['interval_seconds'] < 3 or c['disk_pause_gib'] < 4 or c['disk_resume_gib'] <= c['disk_pause_gib']:
        raise ValueError('Invalid pacing or storage reserve')
    if c.get('missing_dob_policy','require') not in ('require','estimate'):
        raise ValueError('Invalid DOB collection policy')
    if c.get('missing_dob_policy') == 'estimate':
        if (type(c.get('estimated_age_at_exam')) is not int or not 1 <= c['estimated_age_at_exam'] <= 100
                or c.get('sports_registration_requires_verified_dob') is not True):
            raise ValueError('Estimated age requires an explicit assumption and verified-DOB registration gate')
    if not c['ssh_host'] or c['ssh_host'].startswith('-') or any(x.isspace() for x in c['ssh_host']):
        raise ValueError('Invalid SSH host alias')
    for key in ('input', 'infrastructure_root'):
        c[key] = str((path.parent / c[key]).resolve())
    c['profile_path'] = str(path)
    return c


def counts(database):
    database = Path(database)
    if not database.exists():
        return {'total': 0}
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.execute('PRAGMA query_only=ON')
        result = dict(db.execute('SELECT state,count(*) FROM jobs GROUP BY state'))
        result['total'] = sum(result.values())
        result['retained'] = db.execute('SELECT count(*) FROM database_retention').fetchone()[0]
        return result
