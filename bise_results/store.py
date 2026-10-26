"""Local SQLite queue. One process owns the queue lock; workers share short transactions."""

from contextlib import contextmanager, nullcontext
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3

from .importer import audit
from .exams import exam_details
from .parser import IDENTITY_FIELDS, PARSER_VERSION, ReviewRequired, parse_result, parser_version


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def locked_store(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another collector/import/export owns this database; wait for it to finish") from exc
        db = sqlite3.connect(path,timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
          CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY, school TEXT NOT NULL, input_sha256 TEXT NOT NULL,
            year INTEGER NOT NULL, source_rows INTEGER NOT NULL, created_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, roll TEXT NOT NULL, year INTEGER NOT NULL,
            course TEXT NOT NULL DEFAULT 'SSC', exam_type TEXT NOT NULL DEFAULT '2',
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            result_json TEXT, receipt_json TEXT, error TEXT, updated_at TEXT NOT NULL,
            UNIQUE(roll,year,course,exam_type));
          CREATE TABLE IF NOT EXISTS members (
            batch_id TEXT NOT NULL REFERENCES batches(id), job_id INTEGER NOT NULL REFERENCES jobs(id),
            source_row INTEGER NOT NULL, PRIMARY KEY(batch_id, job_id));
          CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
            started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT, evidence TEXT, detail TEXT);
          CREATE TABLE IF NOT EXISTS database_retention (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id), result_sha256 TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL, retained_at TEXT NOT NULL, reason TEXT NOT NULL,
            manifest TEXT);
          CREATE TABLE IF NOT EXISTS student_photos (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id), sha256 TEXT NOT NULL,
            mime_type TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
            source_url TEXT NOT NULL, payload BLOB NOT NULL);
        """)
        from .media import SCHEMA
        db.executescript(SCHEMA)
        # Preserve existing Part-II batch IDs/jobs while adding exam-scoped batches.
        columns = {row['name'] for row in db.execute('PRAGMA table_info(batches)')}
        for name, default in [('course', 'SSC'), ('exam_type', '2')]:
            if name not in columns:
                db.execute(f"ALTER TABLE batches ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
        db.execute('CREATE INDEX IF NOT EXISTS jobs_exam_state ON jobs(year,course,exam_type,state)')
        db.execute('CREATE INDEX IF NOT EXISTS members_source_order ON members(batch_id,source_row,job_id)')
        db.commit()
        try:
            yield db
        finally:
            db.close()


def import_batch(db, source, school, year=2026, exam_type='2', course='SSC'):
    exam_type = str(exam_type)
    exam_details(exam_type, course)
    school = school.strip()
    if not school:
        raise ValueError("School/academy label cannot be blank")
    report = audit(Path(source))
    if report["invalid_rows"] or not report["unique_rolls"]:
        raise ValueError(f"Input needs correction: {report}")
    identity = json.dumps([school, year, course, exam_type, report["sha256"]])
    batch_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
    existing=db.execute('SELECT source_rows FROM batches WHERE id=?',(batch_id,)).fetchone()
    if existing and existing['source_rows']==report['valid_records']:
        members=db.execute('SELECT count(*) FROM members WHERE batch_id=?',(batch_id,)).fetchone()[0]
        if members==report['unique_rolls']:return batch_id
    with Path(source).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    with db:
        db.execute("INSERT OR IGNORE INTO batches (id,school,input_sha256,year,source_rows,created_at,course,exam_type) VALUES (?,?,?,?,?,?,?,?)", (batch_id, school, report["sha256"], year, report["valid_records"], now(), course, exam_type))
        for index, row in enumerate(rows, start=2):
            roll = row.get(report['roll_column'], '').strip()
            if not roll:
                continue
            db.execute("INSERT OR IGNORE INTO jobs (roll,year,course,exam_type,updated_at) VALUES (?,?,?,?,?)", (roll, year, course, exam_type, now()))
            job = db.execute("SELECT id FROM jobs WHERE roll=? AND year=? AND course=? AND exam_type=?", (roll, year, course, exam_type)).fetchone()
            db.execute("INSERT OR IGNORE INTO members VALUES (?,?,?)", (batch_id, job["id"], index))
    return batch_id


def jobs_for(db, batch):
    return db.execute("SELECT j.* FROM members m JOIN jobs j ON j.id=m.job_id WHERE m.batch_id=? ORDER BY m.source_row", (batch,)).fetchall()


def batch_rows(db,batch,states=(),page_size=500):
    """Close each read page before any repair writes; never materialize all cards."""
    last=(-1,-1)
    while True:
        condition=' AND j.state IN ('+','.join('?' for _ in states)+')' if states else ''
        page=db.execute('''SELECT j.*,m.source_row AS _source_row FROM members m JOIN jobs j ON j.id=m.job_id
          WHERE m.batch_id=? AND (m.source_row,m.job_id)>(?,?)'''+condition+
          ' ORDER BY m.source_row,m.job_id LIMIT ?',(batch,*last,*states,page_size)).fetchall()
        if not page:return
        last=(page[-1]['_source_row'],page[-1]['id'])
        yield from page


def check_receipt(receipt, digest=None):
    if not receipt or not {"card.html", "card.png", "parsed.json"}.issubset(receipt):
        return False
    try:
        for record in receipt.values():
            path = Path(record["path"])
            if not path.is_file():
                return False
            if digest is None:
                with path.open('rb') as stream:
                    actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            else:
                actual = digest(path)
            if actual != record["sha256"]:
                return False
    except (OSError, KeyError, TypeError):
        return False
    return True


def retained_in_database(db, row):
    """Explicit hash-bound cleanup, never a blanket waiver for missing files."""
    if row['state'] != 'verified' or not row['result_json']:
        return False
    saved = db.execute('SELECT * FROM database_retention WHERE job_id=?', (row['id'],)).fetchone()
    return bool(saved and photo_retained(db, row['id'], json.loads(row['result_json']))
        and saved['result_sha256'] == hashlib.sha256(row['result_json'].encode()).hexdigest()
        and saved['receipt_sha256'] == hashlib.sha256((row['receipt_json'] or '').encode()).hexdigest())


def repair_external_photo(db,row):
    """A lost external picture must not reset an intact saved board result."""
    if row['state']!='verified' or not row['result_json']:return False
    parsed=json.loads(row['result_json'])
    if parsed.get('student_photo',{}).get('storage')!='files':return False
    saved=db.execute('SELECT * FROM database_retention WHERE job_id=?',(row['id'],)).fetchone()
    if (not saved or saved['result_sha256']!=hashlib.sha256(row['result_json'].encode()).hexdigest()
        or saved['receipt_sha256']!=hashlib.sha256((row['receipt_json'] or '').encode()).hexdigest()):return False
    if photo_retained(db,row['id'],parsed):return False
    with db:
        changed=db.execute("""INSERT INTO photo_recovery(job_id,state,updated_at) VALUES(?,'pending',?)
          ON CONFLICT(job_id) DO UPDATE SET state='pending',retry_after=0,updated_at=excluded.updated_at
          WHERE photo_recovery.state='complete'""",(row['id'],now())).rowcount
        if changed:db.execute("INSERT INTO result_events(job_id,kind,at) VALUES(?,'photo_missing',?)",(row['id'],now()))
    return True


def photo_retained(db, job_id, parsed, verify=False):
    metadata = parsed.get('student_photo')
    if not metadata:
        return True
    if metadata.get('storage')=='files':
        from .media import read_asset
        try:read_asset(db,job_id,metadata,verify=verify);return True
        except (OSError,ValueError,KeyError):return False
    row = db.execute('SELECT sha256,length(payload) AS bytes' + (',payload' if verify else '') +
                     ' FROM student_photos WHERE job_id=?', (job_id,)).fetchone()
    return bool(row and row['sha256'] == metadata.get('sha256') and row['bytes'] == metadata.get('bytes')
                and (not verify or hashlib.sha256(row['payload']).hexdigest() == row['sha256']))


def retain_in_database(db, row, reason, manifest=None, commit=True):
    """Commit the complete parsed result before removing any capture files."""
    if row['state'] != 'verified':
        raise ValueError('Only verified results may discard captured originals')
    parsed = json.loads(row['result_json'] or '{}')
    from .demographics import explicitly_estimated_hssc
    if (str(parsed.get('roll')) != row['roll'] or str(parsed.get('year')) != str(row['year'])
            or str(parsed.get('exam_type', '2')) != row['exam_type']
            or parsed.get('course', 'SSC') != row['course']
            or not all(parsed.get(field) for field in ('name','father_name','student_cnic','father_cnic'))
            or not (parsed.get('date_of_birth') or explicitly_estimated_hssc(parsed))
            or not photo_retained(db, row['id'], parsed, verify=True)
            or not set(IDENTITY_FIELDS).issubset(parsed)):
        raise ValueError('Database identity is incomplete or differs from exam/roll key')
    with db if commit else nullcontext():
        db.execute('''INSERT INTO database_retention VALUES (?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET result_sha256=excluded.result_sha256,
            receipt_sha256=excluded.receipt_sha256,retained_at=excluded.retained_at,
            reason=excluded.reason,manifest=excluded.manifest''',
            (row['id'],hashlib.sha256(row['result_json'].encode()).hexdigest(),
             hashlib.sha256((row['receipt_json'] or '').encode()).hexdigest(),now(),reason,
             str(manifest) if manifest else None))


def _write_reparsed_artifacts(parsed, previous_receipt):
    """Write a derived revision; preserve every original card/JSON/receipt file."""
    payload = (json.dumps(parsed, indent=2, ensure_ascii=False) + "\n").encode()
    digest = hashlib.sha256(payload).hexdigest()
    original = previous_receipt.get("parsed.json", previous_receipt["card.html"])
    root = Path(original["path"]).parent / "reparsed" / parsed['parser_version'] / digest[:16]
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / "parsed.json.tmp"
    temporary.write_bytes(payload)
    temporary.replace(root / "parsed.json")
    receipt = {**previous_receipt, "parsed.json": {"path": str((root / "parsed.json").resolve()), "sha256": digest}}
    temporary = root / "receipt.json.tmp"
    temporary.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    temporary.replace(root / "receipt.json")
    return receipt


def reparse_cached_results(db, batch, receipt_checker=None, seen=None):
    """Upgrade cached cards locally under the caller's exclusive store lock.

    Version-aware export/resume is essential: verified v1 rows otherwise never
    pass through the parser again and would permanently miss the new columns.
    """
    summary = {"upgraded": 0, "recovered": 0, "already_current": 0, "unavailable": 0, "needs_review": 0}
    receipt_checker = receipt_checker or check_receipt
    # Pending imports can contain hundreds of thousands of rolls; only captured
    # responses can be reparsed. Stream those rows instead of materializing all.
    rows = batch_rows(db,batch,('verified','needs_review','failed'))
    for row in rows:
        if seen is not None:
            if row['id'] in seen:
                continue
            seen.add(row['id'])
        if repair_external_photo(db,row):
            summary['already_current']+=1
            continue
        receipt = json.loads(row["receipt_json"] or "{}")
        previous = json.loads(row["result_json"] or "{}")
        if retained_in_database(db, row):
            summary['already_current'] += 1
            continue
        if receipt:
            if not receipt_checker(receipt):
                summary["unavailable"] += 1
                continue
        elif row["state"] != "verified":
            # A parser-rejected response already has captured HTML/PNG even
            # though no parsed.json or verified receipt could be created.
            attempt = db.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
            folder = Path(attempt["evidence"]) if attempt is not None and attempt["evidence"] else None
            if folder is None or any(not (folder / name).is_file() for name in ("card.html", "card.png")):
                summary["unavailable"] += 1
                continue
            receipt = {name: {"path": str((folder / name).resolve()), "sha256": hashlib.sha256((folder / name).read_bytes()).hexdigest()} for name in ("card.html", "card.png")}
            previous.setdefault("fetched_at", attempt["ended_at"] or attempt["started_at"])
            previous["recovered_from_attempt"] = attempt["id"]
        else:
            summary["unavailable"] += 1
            continue
        if row["state"] == "verified" and previous.get("parser_version") == parser_version(row['exam_type'], row['course']) and set(IDENTITY_FIELDS).issubset(previous):
            summary["already_current"] += 1
            continue
        try:
            source = Path(receipt["card.html"]["path"]).read_text(encoding="utf-8")
            parsed = {**previous, **parse_result(source, row["roll"], row["year"], exam_type=row['exam_type'], course=row['course']), "reparsed_at": now()}
            if row['course'] == 'HSSC' and not parsed.get('date_of_birth'):
                from .demographics import explicitly_estimated_hssc
                if not explicitly_estimated_hssc(parsed):
                    raise ReviewRequired('Cached HSSC card still needs a verified DOB or explicit collection-only age policy')
            if not photo_retained(db, row['id'], parsed, verify=True):
                raise ReviewRequired('Cached student photo is not durably retained in SQLite')
        except ReviewRequired as exc:
            with db:
                db.execute("UPDATE jobs SET state='needs_review',error=?,updated_at=? WHERE id=?", (f"Cached card requires review after parser upgrade: {exc}", now(), row["id"]))
            summary["needs_review"] += 1
            continue
        # Files precede the DB commit; a failed write leaves the old row intact.
        upgraded_receipt = _write_reparsed_artifacts(parsed, receipt)
        with db:
            db.execute("UPDATE jobs SET state='verified',error=NULL,result_json=?,receipt_json=?,updated_at=? WHERE id=?", (json.dumps(parsed), json.dumps(upgraded_receipt), now(), row["id"]))
        summary["upgraded"] += 1
        if row["state"] != "verified":
            summary["recovered"] += 1
    return summary


def prepare_jobs(db, batch, retry_unresolved=False, digests=None):
    """Recover claims only while the caller holds the exclusive process lock."""
    from .publishing import ArtifactDigests
    digests = digests if digests is not None else ArtifactDigests()
    verifier = lambda receipt: check_receipt(receipt, digests)
    reparse_cached_results(db, batch, receipt_checker=verifier)
    with db:
        db.execute("UPDATE jobs SET state='pending',error='Recovered interrupted claim',updated_at=? WHERE state='running'", (now(),))
        db.execute("UPDATE attempts SET ended_at=?,outcome='interrupted' WHERE ended_at IS NULL", (now(),))
    for row in batch_rows(db,batch):
        if repair_external_photo(db,row):continue
        if row["state"] == "verified" and not retained_in_database(db,row) and not verifier(json.loads(row["receipt_json"] or "{}")):
            with db:
                db.execute("UPDATE jobs SET state='pending',error='Artifact missing or hash mismatch',updated_at=? WHERE id=?", (now(), row["id"]))
        elif retry_unresolved and row["state"] in ("needs_review", "failed"):
            with db:
                db.execute("UPDATE jobs SET state='pending',updated_at=? WHERE id=?", (now(), row["id"]))
    return [dict(row) for row in batch_rows(db,batch,('pending',))]
