"""Online SQLite snapshots and resumable, hash-pinned private transfers."""
import fcntl
from contextlib import closing
import gzip
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from .common import atomic, counts, digest, now


def create(root):
    root = Path(root).resolve()
    folder = root / 'snapshots'; folder.mkdir(exist_ok=True)
    with (folder / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        source = root / 'results.sqlite3'
        if not source.exists():
            raise ValueError('No collected database exists yet')
        required = source.stat().st_size * 2 + 1024**3
        if shutil.disk_usage(folder).free < required:
            raise ValueError('Not enough snapshot headroom; collection database preserved')
        ident = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
        raw = folder / (ident + '.sqlite3')
        compressed = folder / (ident + '.sqlite3.gz')
        try:
            deadline = time.monotonic() + 180
            def progress(*args):
                if time.monotonic() > deadline:
                    raise TimeoutError('Snapshot timed out; try while the writer is quieter')
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as live, closing(sqlite3.connect(raw)) as backup:
                live.backup(backup, pages=1024, progress=progress, sleep=.02)
                if backup.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('Snapshot integrity check failed')
                backup.execute('PRAGMA journal_mode=DELETE')
            metadata = {'id': ident, 'created_at': now(), 'database_sha256': digest(raw),
                        'database_bytes': raw.stat().st_size, 'counts': counts(raw)}
            with raw.open('rb') as incoming, gzip.open(compressed, 'wb', compresslevel=1) as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024**2)
            os.chmod(compressed, 0o600)
            metadata.update(filename=compressed.name, sha256=digest(compressed), bytes=compressed.stat().st_size,
                            pinned_until=time.time() + 86400)
            atomic(folder / (ident + '.json'), metadata)
            # Pins protect interrupted/active downloads for one day. Keep two
            # further unpinned snapshots; never touch the authoritative DB.
            unpinned = []
            for path in folder.glob('*.json'):
                item = json.loads(path.read_text())
                if item.get('pinned_until', 0) <= time.time():
                    unpinned.append((path, item))
            for path, item in sorted(unpinned, key=lambda x: x[1]['created_at'], reverse=True)[2:]:
                (folder / item['filename']).unlink(missing_ok=True); path.unlink()
            return metadata
        finally:
            raw.unlink(missing_ok=True)
            if not (folder / (ident + '.json')).exists():
                compressed.unlink(missing_ok=True)


def metadata(root, ident):
    import re
    if not re.fullmatch(r'[0-9TZ]+-[a-f0-9]{8}', ident):
        raise ValueError('Invalid snapshot identifier')
    value = json.loads((Path(root) / 'snapshots' / (ident + '.json')).read_text())
    if value['id'] != ident or Path(value['filename']).name != value['filename']:
        raise ValueError('Invalid snapshot metadata')
    return value


def release(root, ident):
    folder = Path(root) / 'snapshots'
    with (folder / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        item = metadata(root, ident); item['pinned_until'] = 0
        atomic(folder / (ident + '.json'), item)


def stream(root, ident, offset, output):
    if offset < 0:
        raise ValueError('Negative transfer offset')
    folder = Path(root) / 'snapshots'
    with (folder / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        item = metadata(root, ident)
        item['pinned_until'] = time.time() + 86400
        atomic(folder / (ident + '.json'), item)
        incoming = (folder / item['filename']).open('rb')
    with incoming:
        if offset > item['bytes']:
            raise ValueError('Offset exceeds pinned snapshot size')
        incoming.seek(offset)
        shutil.copyfileobj(incoming, output, 1024**2)


def restore(compressed, destination, item):
    compressed, destination = Path(compressed), Path(destination)
    if compressed.stat().st_size != item['bytes'] or digest(compressed) != item['sha256']:
        raise ValueError('Downloaded snapshot checksum mismatch')
    temporary = destination.with_suffix('.restore.tmp')
    try:
        with gzip.open(compressed, 'rb') as incoming, temporary.open('wb') as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024**2)
        if digest(temporary) != item['database_sha256']:
            raise ValueError('Database checksum mismatch')
        with closing(sqlite3.connect(temporary.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('Downloaded database is corrupt')
        if counts(temporary) != item['counts']:
            raise ValueError('Downloaded database row counts differ')
        os.chmod(temporary, 0o600); temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
