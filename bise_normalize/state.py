"""Durable transform cache and indexed published evaluations; portal DB is read-only."""

from contextlib import contextmanager, closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from youthgames_shared.catalog import PORTAL, signature
from . import VERSION
from .publish import locked


def registration_snapshot(path, campaign):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('Registration database is missing; cannot exclude previous uploads')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')  # Reads committed WAL data as one consistent semantic snapshot.
        rows = db.execute('SELECT applicant_key,state FROM jobs WHERE portal=? AND campaign=? ORDER BY applicant_key', (PORTAL, campaign)).fetchall()
        return dict(rows)
    finally:
        db.close()


def allocated_states(states,source,scheme):
    """Unrelated uploads must not invalidate a frozen allocation's exports."""
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        keys={r[0] for r in db.execute('SELECT applicant_key FROM allocations WHERE scheme=?',(scheme,))}
    return {key:value for key,value in states.items() if key in keys}


def transform_context(config, captured):
    effective = deepcopy(config)
    for key in ('profile', 'selection', 'target', 'output', 'portal'):
        effective.pop(key, None)
    effective['source'] = {'exam': effective['source']['exam']}
    effective['validation'].pop('catalog', None)
    effective['validation'].pop('eligibility', None)
    for entry in effective['enrichment']['sources']:
        entry.pop('path', None)
    effective['enrichment']['overrides'] = bool(effective['enrichment']['overrides'])
    return signature({'version': VERSION, 'config': effective,
                      'enrichment': {k: v[1] for k, v in captured.items() if k.startswith('enrichment:') or k == 'overrides'}})


class Ledger:
    def __init__(self, db, context):
        self.db, self.context = db, context
        self.hits = self.transformed = 0

    def transform(self, raw, number, compute):
        key = signature({'context': self.context, 'raw': raw})
        hit = self.db.execute('SELECT payload FROM transforms WHERE key=?', (key,)).fetchone()
        if hit:
            self.hits += 1
            record = json.loads(hit[0])
            record['source_rows'] = [number]
            return record
        record = compute()
        self.db.execute('INSERT INTO transforms VALUES (?,?)', (key, json.dumps(record, ensure_ascii=False)))
        self.transformed += 1
        return record

    def published(self, prepared, config, fingerprint, manifest_hash):
        profile = config['profile']
        exam = signature(config['source']['exam'])
        self.db.execute('DELETE FROM evaluations WHERE profile=?', (profile,))
        for row in prepared.records():
            person = exam + ':' + row['roll']
            self.db.execute('INSERT INTO people VALUES (?,?,?,?) ON CONFLICT(person) DO UPDATE SET bform=excluded.bform,raw=excluded.raw',
                            (person, row['roll'], row['data']['BForm'], json.dumps(row['raw'], ensure_ascii=False)))
            portal = row['portal']
            self.db.execute('INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?)',
                            (profile, person, row['data']['BForm'], portal['age']['state'], portal['registration_state'], int(portal['ready']), config['portal']['campaign'], json.dumps(row, ensure_ascii=False)))
        self.db.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?)', (profile, fingerprint, manifest_hash))


@contextmanager
def opened(path, context):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        db = sqlite3.connect(path)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS transforms(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS people(person TEXT PRIMARY KEY,roll TEXT,bform TEXT,raw TEXT);
              CREATE INDEX IF NOT EXISTS people_bform ON people(bform);
              CREATE TABLE IF NOT EXISTS evaluations(profile TEXT,person TEXT,bform TEXT,age_state TEXT,
                registration_state TEXT,ready INTEGER,campaign TEXT,payload TEXT,PRIMARY KEY(profile,person));
              CREATE INDEX IF NOT EXISTS evaluations_bform ON evaluations(bform,campaign);
              CREATE INDEX IF NOT EXISTS evaluations_ready ON evaluations(profile,ready);
              CREATE TABLE IF NOT EXISTS runs(profile TEXT PRIMARY KEY,fingerprint TEXT,manifest_hash TEXT);
            ''')
            with db:
                yield Ledger(db, context)
        finally:
            db.close()


def status(path, profile=None, bform=None, roll=None):
    path = Path(path).resolve()
    if not path.is_file():
        return {'status': 'not_initialized', 'database': str(path)}
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        clauses, args = [], []
        for field, value in [('e.profile', profile), ('e.bform', bform), ('p.roll', roll)]:
            if value:
                clauses.append(field + '=?'); args.append(value)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = db.execute('SELECT e.profile,e.age_state,e.registration_state,e.ready,count(*) FROM evaluations e JOIN people p ON p.person=e.person' + where + ' GROUP BY e.profile,e.age_state,e.registration_state,e.ready ORDER BY e.profile,e.age_state,e.registration_state', args).fetchall()
        return {'database': str(path), 'published_snapshot_counts': [dict(zip(['profile', 'age_state', 'registration_state', 'ready', 'count'], row)) for row in rows],
                'note': 'Published snapshot. Run normalization to refresh portal upload states.'}
    finally:
        db.close()
