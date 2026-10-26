"""Immutable source cohorts and evenly distributed, auditable UC assignments."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from bise_normalize.publish import locked
from youthgames_shared.catalog import signature


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def opened(path, readonly=False):
    path = Path(path).resolve()
    if readonly:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        db = sqlite3.connect(path, uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS allocation_schemes(
                scheme TEXT PRIMARY KEY, definition_hash TEXT NOT NULL,
                definition TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
                catalog_hash TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS allocation_targets(
                scheme TEXT NOT NULL REFERENCES allocation_schemes(scheme),
                uc_number INTEGER NOT NULL, division TEXT NOT NULL, division_id TEXT NOT NULL,
                district TEXT NOT NULL, district_id TEXT NOT NULL,
                tehsil TEXT NOT NULL, tehsil_id TEXT NOT NULL, uc TEXT NOT NULL, uc_id TEXT NOT NULL,
                PRIMARY KEY(scheme,uc_number), UNIQUE(scheme,uc_id));
              CREATE TABLE IF NOT EXISTS allocations(
                scheme TEXT NOT NULL, year INTEGER NOT NULL, course TEXT NOT NULL,
                exam_type TEXT NOT NULL, roll TEXT NOT NULL, applicant_key TEXT NOT NULL,
                uc_number INTEGER NOT NULL, source_record TEXT NOT NULL,
                assigned_at TEXT NOT NULL,
                PRIMARY KEY(scheme,year,course,exam_type,roll),
                FOREIGN KEY(scheme,uc_number) REFERENCES allocation_targets(scheme,uc_number));
              CREATE INDEX IF NOT EXISTS allocations_applicant ON allocations(applicant_key);
              CREATE INDEX IF NOT EXISTS allocations_uc ON allocations(scheme,uc_number,roll);
              CREATE TABLE IF NOT EXISTS sport_batches(
                id TEXT PRIMARY KEY,scheme TEXT NOT NULL,policy_hash TEXT NOT NULL,
                policy TEXT NOT NULL,created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS sport_assignments(
                scheme TEXT NOT NULL,applicant_key TEXT NOT NULL,roll TEXT NOT NULL,
                gender TEXT NOT NULL,uc_number INTEGER NOT NULL,sport TEXT NOT NULL,
                batch TEXT NOT NULL REFERENCES sport_batches(id),assigned_at TEXT NOT NULL,
                PRIMARY KEY(scheme,applicant_key));
              CREATE TRIGGER IF NOT EXISTS protect_sport_policy_update BEFORE UPDATE ON sport_batches
              BEGIN SELECT RAISE(ABORT,'Approved sport policy history is immutable'); END;
              CREATE TRIGGER IF NOT EXISTS protect_sport_policy_delete BEFORE DELETE ON sport_batches
              BEGIN SELECT RAISE(ABORT,'Approved sport policy history cannot be deleted'); END;
              CREATE TRIGGER IF NOT EXISTS protect_sport_policy_replace BEFORE INSERT ON sport_batches
              WHEN EXISTS(SELECT 1 FROM sport_batches b WHERE b.id=NEW.id)
              BEGIN SELECT RAISE(ABORT,'Sport policy batch already exists'); END;
              CREATE TRIGGER IF NOT EXISTS protect_sport_assignment_update BEFORE UPDATE ON sport_assignments
              BEGIN SELECT RAISE(ABORT,'Sport assignment is immutable; preserve the approved mapping'); END;
              CREATE TRIGGER IF NOT EXISTS protect_sport_assignment_delete BEFORE DELETE ON sport_assignments
              BEGIN SELECT RAISE(ABORT,'Sport assignment history cannot be deleted'); END;
              CREATE TRIGGER IF NOT EXISTS protect_sport_assignment_replace BEFORE INSERT ON sport_assignments
              WHEN EXISTS(SELECT 1 FROM sport_assignments s WHERE s.scheme=NEW.scheme AND s.applicant_key=NEW.applicant_key)
              BEGIN SELECT RAISE(ABORT,'Sport assignment already exists'); END;
              CREATE TABLE IF NOT EXISTS allocation_pilots(
                scheme TEXT NOT NULL, uc_number INTEGER NOT NULL, roll TEXT NOT NULL,
                applicant_key TEXT NOT NULL, selected_at TEXT NOT NULL,
                PRIMARY KEY(scheme,uc_number), UNIQUE(scheme,applicant_key),
                FOREIGN KEY(scheme,uc_number) REFERENCES allocation_targets(scheme,uc_number));
              CREATE TABLE IF NOT EXISTS allocation_http_pilots(
                scheme TEXT NOT NULL, uc_number INTEGER NOT NULL, roll TEXT NOT NULL,
                applicant_key TEXT NOT NULL, selected_at TEXT NOT NULL,
                PRIMARY KEY(scheme,uc_number), UNIQUE(scheme,applicant_key),
                FOREIGN KEY(scheme,uc_number) REFERENCES allocation_targets(scheme,uc_number));
            ''')
            view=db.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='jobs'").fetchone()
            if view and 'sport_assignments' not in view[0]:db.execute('DROP VIEW jobs')
            db.executescript('''CREATE VIEW IF NOT EXISTS jobs AS
                SELECT a.scheme AS allocation_scheme, a.roll,a.year,a.course,a.exam_type,
                  'verified' AS state,
                  json_set(a.source_record,'$.Division',t.division,'$.District',t.district,
                    '$.Tehsil',t.tehsil,'$.UC',t.uc,'$.Sport',coalesce(s.sport,json_extract(a.source_record,'$.Sport'),''),
                    '$.sport_allocation',CASE WHEN s.batch IS NULL THEN NULL ELSE
                      json_object('scheme',s.scheme,'batch',s.batch,'applicant_key',s.applicant_key,
                        'sport',s.sport,'gender',s.gender,'uc_number',s.uc_number,'assigned_at',s.assigned_at,
                        'basis','operator_confirmed_sports_ratio') END,'$.administrative_allocation',
                    json_object('scheme',a.scheme,'uc_number',a.uc_number,'uc_id',t.uc_id,
                      'tehsil_id',t.tehsil_id,'assigned_at',a.assigned_at,
                      'basis','operator_confirmed_administrative_allocation',
                      'residence_inferred',json('false'))) AS result_json
                FROM allocations a JOIN allocation_targets t
                ON t.scheme=a.scheme AND t.uc_number=a.uc_number
                LEFT JOIN sport_assignments s ON s.scheme=a.scheme AND s.applicant_key=a.applicant_key;
            ''')
            with db:
                yield db
        finally:
            db.close()


def assign(db, definition, targets, records, catalog_hash):
    """Freeze this exact cohort. Reruns never reshuffle, append or replace it."""
    scheme = definition['scheme']
    meaningful = {k: v for k, v in definition.items() if k not in ('paths',)}
    definition_hash = signature({'settings': meaningful, 'targets': targets})
    existing = db.execute('SELECT * FROM allocation_schemes WHERE scheme=?', (scheme,)).fetchone()
    if existing:
        if existing['definition_hash'] != definition_hash:
            raise ValueError('Allocation definition changed; use a new scheme rather than reassigning students')
        return {'status': 'already_allocated', 'assigned': db.execute('SELECT count(*) FROM allocations WHERE scheme=?', (scheme,)).fetchone()[0]}
    records = sorted(records, key=lambda r: (int(r['roll']), r['roll']))
    if not records:
        raise ValueError('No eligible source identities; allocation was not created')
    expected = definition.get('expected_records')
    if expected is not None and len(records) != expected:
        raise ValueError(f'Expected {expected} source records; found {len(records)}. Review the new scope before allocating')
    if len({r['roll'] for r in records}) != len(records):
        raise ValueError('Allocation source contains duplicate roll keys')
    if not definition['administrative_allocation_confirmed']:
        raise ValueError('Administrative allocation requires operator confirmation')
    created = now()
    fingerprint = signature(records)
    with db:
        db.execute('INSERT INTO allocation_schemes VALUES(?,?,?,?,?,?)',
                   (scheme, definition_hash, json.dumps(definition), fingerprint, catalog_hash, created))
        for target in targets:
            db.execute('INSERT INTO allocation_targets VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (scheme, target['number'], *[str(target[k]) for k in ('Division','division_id','District','district_id','Tehsil','tehsil_id','UC','uc_id')]))
        for index, record in enumerate(records):
            key = ''.join(c for c in record.get('student_cnic', '') if c.isascii() and c.isdigit())
            db.execute('INSERT INTO allocations VALUES(?,?,?,?,?,?,?,?,?)',
                       (scheme, record['year'], record['course'], record['exam_type'], record['roll'], key,
                        targets[index % len(targets)]['number'], json.dumps(record, ensure_ascii=False), created))
    return {'status': 'allocated', 'assigned': len(records), 'source_fingerprint': fingerprint}
