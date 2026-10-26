"""Durable registration history; uncertain retries require explicit operator opt-in."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sqlite3

from .config import signature, sha

PROTECTED = {'registered', 'uncertain', 'already_registered_external', 'submitting'}


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def opened(path):
    path = Path(path)
    if path.with_suffix('.remote-owner.json').exists():
        raise ValueError('Campaign writer moved to the server; use bise_remote watch/start --job registration')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another registration process owns this database') from exc
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA recursive_triggers=ON')
        db.executescript('''
          CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, profile TEXT, campaign TEXT, hashes TEXT, catalog TEXT, snapshot TEXT);
          CREATE TABLE IF NOT EXISTS profiles (name TEXT PRIMARY KEY, batch TEXT REFERENCES batches(id));
          CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, portal TEXT, campaign TEXT, applicant_key TEXT, roll TEXT,
             payload TEXT, payload_hash TEXT, state TEXT, detail TEXT, receipt TEXT, tracking TEXT, updated_at TEXT,
             UNIQUE(portal,campaign,applicant_key));
          CREATE TABLE IF NOT EXISTS members (batch TEXT REFERENCES batches(id), job INTEGER REFERENCES jobs(id), PRIMARY KEY(batch,job));
          CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, job INTEGER REFERENCES jobs(id), started_at TEXT,
             intent_at TEXT, dispatched_at TEXT, outcome TEXT, evidence TEXT);
          CREATE TABLE IF NOT EXISTS conflicts (batch TEXT, job INTEGER, detail TEXT, UNIQUE(batch,job,detail));
          CREATE TABLE IF NOT EXISTS submission_recoveries (
             attempt INTEGER PRIMARY KEY REFERENCES attempts(id), job INTEGER REFERENCES jobs(id),
             reason TEXT NOT NULL, not_before TEXT NOT NULL, evidence TEXT NOT NULL, recovered_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS portal_pacing (
             portal TEXT NOT NULL, campaign TEXT NOT NULL, dispatch_interval REAL NOT NULL,
             retry_round INTEGER NOT NULL, throttle_round INTEGER NOT NULL,
             not_before TEXT NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL,
             PRIMARY KEY(portal,campaign));
          CREATE TABLE IF NOT EXISTS portal_dispatch_clock (
             portal TEXT NOT NULL,campaign TEXT NOT NULL,dispatched_at TEXT NOT NULL,
             PRIMARY KEY(portal,campaign));
          CREATE TABLE IF NOT EXISTS prepared_inputs (
             profile TEXT PRIMARY KEY,context_hash TEXT NOT NULL,input_hash TEXT NOT NULL,updated_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS connection_pacing (
             portal TEXT NOT NULL,campaign TEXT NOT NULL,connection_key TEXT NOT NULL,
             dispatch_interval REAL NOT NULL,retry_round INTEGER NOT NULL,throttle_round INTEGER NOT NULL,
             not_before TEXT NOT NULL,reason TEXT NOT NULL,updated_at TEXT NOT NULL,
             PRIMARY KEY(portal,campaign,connection_key));
          CREATE TABLE IF NOT EXISTS connection_dispatch_clock (
             portal TEXT NOT NULL,campaign TEXT NOT NULL,connection_key TEXT NOT NULL,dispatched_at TEXT NOT NULL,
             PRIMARY KEY(portal,campaign,connection_key));
          CREATE TABLE IF NOT EXISTS recovery_delays (
             portal TEXT NOT NULL,campaign TEXT NOT NULL,connection_key TEXT NOT NULL,
             server_not_before TEXT,transport_not_before TEXT,transport_round INTEGER NOT NULL,
             updated_at TEXT NOT NULL,PRIMARY KEY(portal,campaign,connection_key));
          CREATE TABLE IF NOT EXISTS attempt_connections (
             attempt INTEGER PRIMARY KEY REFERENCES attempts(id),connection_key TEXT NOT NULL,connection_name TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS connection_slots (
             portal TEXT NOT NULL,campaign TEXT NOT NULL,connection_key TEXT NOT NULL,interface TEXT NOT NULL,
             initialized_at TEXT NOT NULL,PRIMARY KEY(portal,campaign,connection_key));
          CREATE TABLE IF NOT EXISTS connection_observations (
             id INTEGER PRIMARY KEY,portal TEXT NOT NULL,campaign TEXT NOT NULL,connection_key TEXT NOT NULL,
             public_ip_hash TEXT NOT NULL,observed_at TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS connection_observations_latest
             ON connection_observations(portal,campaign,connection_key,id);
          CREATE TABLE IF NOT EXISTS uncertain_retries (
             prior_attempt INTEGER PRIMARY KEY REFERENCES attempts(id),job INTEGER NOT NULL REFERENCES jobs(id),
             profile TEXT NOT NULL,run_path TEXT NOT NULL,original_job TEXT NOT NULL,authorized_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS registered_applicants (
             portal TEXT NOT NULL, campaign TEXT NOT NULL, applicant_key TEXT NOT NULL,
             job INTEGER NOT NULL REFERENCES jobs(id), status TEXT NOT NULL,
             confirmed_at TEXT NOT NULL, tracking TEXT, receipt TEXT, payload_hash TEXT,
             PRIMARY KEY(portal,campaign,applicant_key),
             CHECK(status IN ('registered','already_registered_external'))
          ) WITHOUT ROWID;
          CREATE INDEX IF NOT EXISTS jobs_ready_queue ON jobs(portal,campaign,state,roll,id);
          CREATE INDEX IF NOT EXISTS jobs_ready_identity_queue ON jobs(portal,campaign,state,roll,id,applicant_key);
          CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state,id);
          CREATE TABLE IF NOT EXISTS imported_registrations(
            portal TEXT NOT NULL,campaign TEXT NOT NULL,applicant_key TEXT NOT NULL,status TEXT NOT NULL,
            tracking TEXT,confirmed_at TEXT,source_hash TEXT NOT NULL,payload_hash TEXT,
            PRIMARY KEY(portal,campaign,applicant_key));
          CREATE TRIGGER IF NOT EXISTS protect_imported_registration_attempt BEFORE UPDATE OF state ON jobs
            WHEN NEW.state IN ('filling','submitting') AND EXISTS(SELECT 1 FROM imported_registrations r
              WHERE r.portal=NEW.portal AND r.campaign=NEW.campaign AND r.applicant_key=NEW.applicant_key)
            BEGIN SELECT RAISE(ABORT,'Applicant confirmed in imported campaign ledger'); END;
          CREATE TRIGGER IF NOT EXISTS immutable_imported_registration_update BEFORE UPDATE ON imported_registrations
            BEGIN SELECT RAISE(ABORT,'Imported terminal registration is immutable'); END;
          CREATE TRIGGER IF NOT EXISTS immutable_imported_registration_delete BEFORE DELETE ON imported_registrations
            BEGIN SELECT RAISE(ABORT,'Imported terminal registration cannot be deleted'); END;
          CREATE INDEX IF NOT EXISTS attempts_by_job ON attempts(job,id);
          CREATE INDEX IF NOT EXISTS attempts_unfinished ON attempts(id) WHERE outcome IS NULL;
          CREATE TRIGGER IF NOT EXISTS protect_attempt_connection_update BEFORE UPDATE ON attempt_connections
          BEGIN SELECT RAISE(ABORT,'Dispatched attempt connection is immutable'); END;
          CREATE TRIGGER IF NOT EXISTS protect_attempt_connection_delete BEFORE DELETE ON attempt_connections
          BEGIN SELECT RAISE(ABORT,'Dispatched attempt connection cannot be deleted'); END;
          CREATE TRIGGER IF NOT EXISTS protect_attempt_connection_replace BEFORE INSERT ON attempt_connections
          WHEN EXISTS(SELECT 1 FROM attempt_connections c WHERE c.attempt=NEW.attempt)
          BEGIN SELECT RAISE(ABORT,'Dispatched attempt connection cannot be replaced'); END;
          CREATE TRIGGER IF NOT EXISTS protect_uncertain_retry_update BEFORE UPDATE ON uncertain_retries
          BEGIN SELECT RAISE(ABORT,'Uncertain retry history is immutable'); END;
          CREATE TRIGGER IF NOT EXISTS protect_uncertain_retry_delete BEFORE DELETE ON uncertain_retries
          BEGIN SELECT RAISE(ABORT,'Uncertain retry history cannot be deleted'); END;
          CREATE TRIGGER IF NOT EXISTS protect_uncertain_retry_replace BEFORE INSERT ON uncertain_retries
          WHEN EXISTS(SELECT 1 FROM uncertain_retries r WHERE r.prior_attempt=NEW.prior_attempt)
          BEGIN SELECT RAISE(ABORT,'Uncertain retry history cannot be replaced'); END;
          CREATE TRIGGER IF NOT EXISTS retain_registered_insert AFTER INSERT ON jobs
          WHEN NEW.state IN ('registered','already_registered_external') BEGIN
             INSERT OR IGNORE INTO registered_applicants VALUES
               (NEW.portal,NEW.campaign,NEW.applicant_key,NEW.id,NEW.state,COALESCE(NEW.updated_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')),NEW.tracking,NEW.receipt,NEW.payload_hash);
          END;
          CREATE TRIGGER IF NOT EXISTS retain_registered_update AFTER UPDATE OF state ON jobs
          WHEN NEW.state IN ('registered','already_registered_external') BEGIN
             INSERT OR IGNORE INTO registered_applicants VALUES
               (NEW.portal,NEW.campaign,NEW.applicant_key,NEW.id,NEW.state,COALESCE(NEW.updated_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')),NEW.tracking,NEW.receipt,NEW.payload_hash);
          END;
          CREATE TRIGGER IF NOT EXISTS protect_registered_job_update BEFORE UPDATE ON jobs
          WHEN OLD.state IN ('registered','already_registered_external') OR EXISTS(
            SELECT 1 FROM registered_applicants r WHERE r.portal=OLD.portal AND r.campaign=OLD.campaign AND r.applicant_key=OLD.applicant_key)
          BEGIN SELECT RAISE(ABORT,'Confirmed registration is immutable; resubmission/reset blocked'); END;
          CREATE TRIGGER IF NOT EXISTS protect_registered_job_delete BEFORE DELETE ON jobs
          WHEN OLD.state IN ('registered','already_registered_external') OR EXISTS(
            SELECT 1 FROM registered_applicants r WHERE r.portal=OLD.portal AND r.campaign=OLD.campaign AND r.applicant_key=OLD.applicant_key)
          BEGIN SELECT RAISE(ABORT,'Confirmed registration cannot be deleted'); END;
          CREATE TRIGGER IF NOT EXISTS protect_registered_key_insert BEFORE INSERT ON jobs
          WHEN EXISTS(SELECT 1 FROM registered_applicants r
            WHERE r.portal=NEW.portal AND r.campaign=NEW.campaign AND r.applicant_key=NEW.applicant_key)
          BEGIN SELECT RAISE(ABORT,'Applicant already registered in this portal/campaign'); END;
          CREATE TRIGGER IF NOT EXISTS protect_registered_attempt BEFORE INSERT ON attempts
          WHEN EXISTS(SELECT 1 FROM jobs j JOIN registered_applicants r
            ON r.portal=j.portal AND r.campaign=j.campaign AND r.applicant_key=j.applicant_key WHERE j.id=NEW.job)
          BEGIN SELECT RAISE(ABORT,'Registered applicant cannot start a new attempt'); END;
          CREATE TRIGGER IF NOT EXISTS protect_registration_ledger_update BEFORE UPDATE ON registered_applicants
          BEGIN SELECT RAISE(ABORT,'Registration ledger is append-only'); END;
          CREATE TRIGGER IF NOT EXISTS protect_registration_ledger_replace BEFORE INSERT ON registered_applicants
          WHEN EXISTS(SELECT 1 FROM registered_applicants r
             WHERE r.portal=NEW.portal AND r.campaign=NEW.campaign AND r.applicant_key=NEW.applicant_key)
          BEGIN SELECT RAISE(IGNORE); END;
          CREATE TRIGGER IF NOT EXISTS protect_registration_ledger_delete BEFORE DELETE ON registered_applicants
          BEGIN SELECT RAISE(ABORT,'Registration ledger is append-only'); END;
          CREATE VIEW IF NOT EXISTS registered_students AS
            SELECT r.job AS job_id,j.roll,json_extract(j.payload,'$.data.FullName') AS student_name,
                   r.applicant_key,r.portal,r.campaign,r.status,r.confirmed_at AS registered_at,
                   r.tracking AS tracking_id,r.receipt AS receipt_path
            FROM registered_applicants r JOIN jobs j ON j.id=r.job;
        ''')
        with db:
            # Backfill terminal records without changing a single existing job,
            # receipt or timestamp. The same migration is safe on every restart.
            db.execute('''INSERT OR IGNORE INTO registered_applicants
                SELECT portal,campaign,applicant_key,id,state,COALESCE(updated_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')),tracking,receipt,payload_hash
                FROM jobs j WHERE state IN ('registered','already_registered_external')
                AND NOT EXISTS(SELECT 1 FROM registered_applicants r WHERE r.portal=j.portal
                    AND r.campaign=j.campaign AND r.applicant_key=j.applicant_key)''')
            db.execute("UPDATE jobs SET state='uncertain',detail='Interrupted after submission intent; reconcile before retry',updated_at=? WHERE state='submitting'", (now(),))
            db.execute("UPDATE jobs SET state='ready',detail='Interrupted before submission intent',updated_at=? WHERE state IN ('filling','filled')", (now(),))
            db.execute("UPDATE attempts SET outcome='uncertain' WHERE outcome IS NULL AND intent_at IS NOT NULL")
            db.execute("UPDATE attempts SET outcome='interrupted_before_submit' WHERE outcome IS NULL AND intent_at IS NULL")
        try:
            yield db
        finally:
            db.close()
            fcntl.flock(lock, fcntl.LOCK_UN)


def import_batch(db, config, hashes, catalog, snapshot, applicants):
    batch = signature({'profile': config['profile'], 'campaign': config['campaign'], 'hashes': hashes,
                       'catalog': catalog, 'selection': config['selection'], 'aliases': config['location_aliases']})[:24]
    with db:
        db.execute('INSERT OR IGNORE INTO batches VALUES (?,?,?,?,?,?)', (batch, config['profile'], config['campaign'], json.dumps(hashes), json.dumps(catalog), str(snapshot)))
        db.execute('INSERT OR REPLACE INTO profiles VALUES (?,?)', (config['profile'], batch))
        seen = {}
        for item in applicants:
            key = item['data']['BForm']
            if not key.isascii() or not key.isdigit() or len(key) != 13:
                key = 'invalid:' + signature([item['roll'], item['data']])
            payload_hash = signature(item)
            existing = db.execute('SELECT * FROM jobs WHERE portal=? AND campaign=? AND applicant_key=?', (config['portal'].split('#')[0].rstrip('/'), config['campaign'], key)).fetchone()
            if existing is None:
                cursor = db.execute('INSERT INTO jobs(portal,campaign,applicant_key,roll,payload,payload_hash,state,detail,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                    (config['portal'].split('#')[0].rstrip('/'), config['campaign'], key, item['roll'], json.dumps(item, ensure_ascii=False), payload_hash, item['validation']['state'], '; '.join(item['validation']['issues']), now()))
                job_id = cursor.lastrowid
            else:
                job_id = existing['id']
                if existing['payload_hash'] != payload_hash and existing['state'] in PROTECTED | {'rejected'}:
                    db.execute('INSERT OR IGNORE INTO conflicts VALUES (?,?,?)', (batch, job_id, 'Changed applicant snapshot retained as conflict; previous external outcome preserved'))
                elif key in seen and seen[key] != payload_hash:
                    db.execute("UPDATE jobs SET state='blocked_input',detail='Conflicting input rows share one BForm',updated_at=? WHERE id=? AND state NOT IN ('registered','uncertain','already_registered_external')", (now(), job_id))
                    db.execute('INSERT OR IGNORE INTO conflicts VALUES (?,?,?)', (batch, job_id, 'Conflicting source rows share one applicant key'))
                elif existing['state'] not in PROTECTED | {'rejected'} and (existing['payload_hash'] != payload_hash or existing['state'] != item['validation']['state']):
                    db.execute('UPDATE jobs SET roll=?,payload=?,payload_hash=?,state=?,detail=?,updated_at=? WHERE id=?', (item['roll'], json.dumps(item, ensure_ascii=False), payload_hash, item['validation']['state'], '; '.join(item['validation']['issues']), now(), job_id))
            seen[key] = payload_hash
            db.execute('INSERT OR IGNORE INTO members VALUES (?,?)', (batch, job_id))
    return batch


def known_registrations(db, config):
    """One indexed metadata lookup; completed applicants never need a new form."""
    result={row['applicant_key']:row for row in db.execute('''SELECT applicant_key,job,status FROM registered_applicants
        WHERE portal=? AND campaign=?''',(config['portal'].split('#')[0].rstrip('/'),config['campaign']))}
    for row in db.execute('''SELECT applicant_key,NULL AS job,status FROM imported_registrations WHERE portal=? AND campaign=?''',
                          (config['portal'].split('#')[0].rstrip('/'),config['campaign'])):
        result.setdefault(row['applicant_key'],row)
    return result


def bind_completed_batch(db,config,hashes,snapshot,completed):
    """Bind an already-completed input to a profile without rebuilding applicants."""
    batch=signature({'profile':config['profile'],'campaign':config['campaign'],'hashes':hashes,
                     'completed_jobs':sorted(row['job'] for row in completed if row['job'] is not None)})[:24]
    with db:
        db.execute('INSERT OR IGNORE INTO batches VALUES (?,?,?,?,?,?)',
            (batch,config['profile'],config['campaign'],json.dumps(hashes),'{}',str(snapshot)))
        db.execute('INSERT OR REPLACE INTO profiles VALUES (?,?)',(config['profile'],batch))
        db.executemany('INSERT OR IGNORE INTO members VALUES (?,?)',((batch,row['job']) for row in completed if row['job'] is not None))
    return batch


def jobs(db, profile, states=None, rolls=()):
    batch = db.execute('SELECT batch FROM profiles WHERE name=?', (profile,)).fetchone()
    if batch is None:
        raise ValueError('Profile has no prepared batch; run prepare first')
    sql = 'SELECT j.* FROM jobs j JOIN members m ON m.job=j.id WHERE m.batch=?'
    args = [batch['batch']]
    if states:
        sql += ' AND j.state IN (' + ','.join('?' for _ in states) + ')'
        args += list(states)
    if rolls:
        sql += ' AND j.roll IN (' + ','.join('?' for _ in rolls) + ')'
        args += list(rolls)
    # Conflicts prevent any attempt even when a prior ready job was preserved.
    if states and 'ready' in states:
        sql += ' AND NOT EXISTS(SELECT 1 FROM conflicts c WHERE c.batch=m.batch AND c.job=j.id)'
        sql += ' AND NOT EXISTS(SELECT 1 FROM registered_applicants r WHERE r.portal=j.portal AND r.campaign=j.campaign AND r.applicant_key=j.applicant_key)'
    return db.execute(sql + ' ORDER BY j.roll,j.id', args).fetchall()


def begin(db, job):
    with db:
        changed = db.execute("""UPDATE jobs SET state='filling',updated_at=? WHERE id=? AND state='ready'
            AND NOT EXISTS(SELECT 1 FROM registered_applicants r WHERE r.portal=jobs.portal
                AND r.campaign=jobs.campaign AND r.applicant_key=jobs.applicant_key)""", (now(), job['id'])).rowcount
        if changed != 1:
            raise ValueError('Job is not ready; no submission may be attempted')
        return db.execute('INSERT INTO attempts(job,started_at) VALUES (?,?)', (job['id'], now())).lastrowid


def ready_job_refs(db, config, limit=0):
    """Covering-index queue: no student payloads or historical receipts loaded."""
    profile=db.execute('SELECT batch FROM profiles WHERE name=?',(config['profile'],)).fetchone()
    if profile is None:raise ValueError('Profile has no prepared batch')
    sql='''SELECT j.id,j.roll FROM jobs j WHERE j.portal=? AND j.campaign=? AND j.state='ready'
        AND EXISTS(SELECT 1 FROM members m WHERE m.batch=? AND m.job=j.id)
        AND NOT EXISTS(SELECT 1 FROM conflicts c WHERE c.batch=? AND c.job=j.id)
        AND NOT EXISTS(SELECT 1 FROM registered_applicants r WHERE r.portal=j.portal
            AND r.campaign=j.campaign AND r.applicant_key=j.applicant_key)
        ORDER BY j.roll,j.id'''
    args=[config['portal'].split('#')[0].rstrip('/'),config['campaign'],profile['batch'],profile['batch']]
    if limit:sql+=' LIMIT ?';args.append(limit)
    return db.execute(sql,args).fetchall()


def authorize_uncertain_retries(db, config, run_path, job_ids=None):
    """Opt-in once per prior attempt, only unchanged applicants in this input.

    The original attempt remains uncertain with its original receipt. A later
    run can authorize another try if a NEW attempt also loses its response.
    This relies on the portal rejecting duplicate BForms; it is not exactly-once
    delivery. The registered ledger remains authoritative and immutable.
    """
    batch=db.execute('SELECT batch FROM profiles WHERE name=?',(config['profile'],)).fetchone()
    if batch is None:raise ValueError('Profile has no prepared batch')
    sql='''SELECT j.*,a.id AS prior_attempt FROM jobs j JOIN attempts a
        ON a.id=(SELECT max(x.id) FROM attempts x WHERE x.job=j.id)
        WHERE j.portal=? AND j.campaign=? AND j.state='uncertain' AND j.tracking IS NULL
        AND a.outcome='uncertain' AND a.intent_at IS NOT NULL
        AND EXISTS(SELECT 1 FROM members m WHERE m.batch=? AND m.job=j.id)
        AND NOT EXISTS(SELECT 1 FROM conflicts c WHERE c.batch=? AND c.job=j.id)
        AND NOT EXISTS(SELECT 1 FROM registered_applicants r WHERE r.portal=j.portal
            AND r.campaign=j.campaign AND r.applicant_key=j.applicant_key)
        AND NOT EXISTS(SELECT 1 FROM uncertain_retries r WHERE r.prior_attempt=a.id)'''
    args=[config['portal'].split('#')[0].rstrip('/'),config['campaign'],batch['batch'],batch['batch']]
    rolls=config['selection']['rolls']
    if rolls:
        sql+=' AND j.roll IN ('+','.join('?' for _ in rolls)+')';args.extend(rolls)
    if job_ids is not None:
        if not job_ids:return []
        sql+=' AND j.id IN ('+','.join('?' for _ in job_ids)+')';args.extend(job_ids)
    retried=[]
    with db:
        for record in db.execute(sql+' ORDER BY j.roll,j.id',args).fetchall():
            old=dict(record);prior=old.pop('prior_attempt')
            try:item=json.loads(old['payload'])
            except (ValueError,TypeError):continue
            if signature(item)!=old['payload_hash'] or item.get('validation',{}).get('state')!='ready':continue
            stamp=now()
            db.execute('INSERT INTO uncertain_retries VALUES (?,?,?,?,?,?)',
                (prior,old['id'],config['profile'],str(run_path),json.dumps(old,ensure_ascii=False),stamp))
            changed=db.execute("""UPDATE jobs SET state='ready',detail=?,updated_at=?
                WHERE id=? AND state='uncertain' AND payload_hash=? AND receipt IS ?
                AND NOT EXISTS(SELECT 1 FROM registered_applicants r WHERE r.portal=jobs.portal
                    AND r.campaign=jobs.campaign AND r.applicant_key=jobs.applicant_key)""",
                (f'Operator opted into retry of uncertain attempt {prior}; original history preserved',
                 stamp,old['id'],old['payload_hash'],old['receipt'])).rowcount
            if changed!=1:raise ValueError('Uncertain applicant changed during retry authorization')
            retried.append({'job':old['id'],'prior_attempt':prior})
    return retried


def last_dispatch_time(db,portal,campaign):
    row=db.execute('SELECT dispatched_at FROM portal_dispatch_clock WHERE portal=? AND campaign=?',(portal,campaign)).fetchone()
    if row:return row[0]
    value=db.execute('''SELECT max(a.dispatched_at) FROM attempts a JOIN jobs j ON j.id=a.job
        WHERE j.portal=? AND j.campaign=?''',(portal,campaign)).fetchone()[0]
    if value:
        with db:db.execute('INSERT OR IGNORE INTO portal_dispatch_clock VALUES (?,?,?)',(portal,campaign,value))
    return value


def retain_connection_history(db,portal,campaign,route):
    """One-time conservative inheritance from legacy IP keys to stable devices.

    Legacy rows and immutable attempt associations are retained unchanged.
    Their route-to-device mapping can be unknown after NAT changes, so every
    new device slot inherits the most restrictive legacy clocks/cooldowns.
    """
    with db:
        existing=db.execute('SELECT 1 FROM connection_slots WHERE portal=? AND campaign=? AND connection_key=?',
            (portal,campaign,route.key)).fetchone()
        if not existing:
            legacy=db.execute('''SELECT p.* FROM connection_pacing p WHERE p.portal=? AND p.campaign=?
                AND NOT EXISTS(SELECT 1 FROM connection_slots s WHERE s.portal=p.portal
                    AND s.campaign=p.campaign AND s.connection_key=p.connection_key)''',(portal,campaign)).fetchall()
            clocks=[r[0] for r in db.execute('''SELECT c.dispatched_at FROM connection_dispatch_clock c
                WHERE c.portal=? AND c.campaign=? AND NOT EXISTS(SELECT 1 FROM connection_slots s
                    WHERE s.portal=c.portal AND s.campaign=c.campaign AND s.connection_key=c.connection_key)''',(portal,campaign))]
            deadlines=[r['not_before'] for r in legacy]
            deadlines.extend(r[0] for r in db.execute('''SELECT r.not_before FROM submission_recoveries r
                JOIN jobs j ON j.id=r.job LEFT JOIN attempt_connections c ON c.attempt=r.attempt
                WHERE j.portal=? AND j.campaign=? AND NOT EXISTS(SELECT 1 FROM connection_slots s
                    WHERE s.portal=j.portal AND s.campaign=j.campaign AND s.connection_key=c.connection_key)''',(portal,campaign)))
            if deadlines:
                deadline=max(deadlines,key=datetime.fromisoformat)
                db.execute('''INSERT INTO connection_pacing VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(portal,campaign,connection_key) DO NOTHING''',
                    (portal,campaign,route.key,max([route.interval]+[r['dispatch_interval'] for r in legacy]),
                     max([0]+[r['retry_round'] for r in legacy]),max([0]+[r['throttle_round'] for r in legacy]),
                     deadline,'Inherited legacy IP pacing without resetting cooldown',now()))
            if clocks:
                db.execute('''INSERT INTO connection_dispatch_clock VALUES (?,?,?,?)
                    ON CONFLICT(portal,campaign,connection_key) DO UPDATE
                    SET dispatched_at=max(dispatched_at,excluded.dispatched_at)''',
                    (portal,campaign,route.key,max(clocks,key=datetime.fromisoformat)))
            db.execute('INSERT INTO connection_slots VALUES (?,?,?,?,?)',(portal,campaign,route.key,route.interface,now()))
        if route.public_ip is None:return
        observed=signature({'public_ip':route.public_ip})
        last=db.execute('''SELECT public_ip_hash FROM connection_observations
            WHERE portal=? AND campaign=? AND connection_key=? ORDER BY id DESC LIMIT 1''',(portal,campaign,route.key)).fetchone()
        if not last or last[0]!=observed:
            db.execute('''INSERT INTO connection_observations(portal,campaign,connection_key,public_ip_hash,observed_at)
                VALUES (?,?,?,?,?)''',(portal,campaign,route.key,observed,now()))


def intent(db, job_id, attempt):
    with db:
        changed = db.execute("UPDATE jobs SET state='submitting',updated_at=? WHERE id=? AND state IN ('filling','filled')", (now(), job_id)).rowcount
        if changed != 1:
            raise ValueError('Submission intent requires a filled, unsubmitted job')
        db.execute('UPDATE attempts SET intent_at=? WHERE id=? AND job=? AND outcome IS NULL', (now(), attempt, job_id))


def dispatched(db, job_id, attempt, connection_key=None, connection_name=None, ordering=None):
    with db:
        row = db.execute('SELECT * FROM attempts WHERE id=? AND job=?', (attempt, job_id)).fetchone()
        if not row or not row['intent_at'] or row['dispatched_at'] or row['outcome']:
            raise ValueError('Attempt is not armed for one POST')
        stamp=now()
        db.execute('UPDATE attempts SET dispatched_at=? WHERE id=?', (stamp, attempt))
        db.execute('''INSERT INTO portal_dispatch_clock SELECT portal,campaign,? FROM jobs WHERE id=?
            ON CONFLICT(portal,campaign) DO UPDATE SET dispatched_at=max(dispatched_at,excluded.dispatched_at)''',(stamp,job_id))
        if connection_key:
            db.execute('INSERT INTO attempt_connections VALUES (?,?,?)',(attempt,connection_key,connection_name))
            db.execute('''INSERT INTO connection_dispatch_clock SELECT portal,campaign,?,? FROM jobs WHERE id=?
                ON CONFLICT(portal,campaign,connection_key) DO UPDATE SET dispatched_at=max(dispatched_at,excluded.dispatched_at)''',
                (connection_key,stamp,job_id))
        if ordering:ordering.record(job_id,stamp)
    if ordering:ordering.release(job_id)


def finish(db, job_id, attempt, state, detail='', receipt=None, tracking=None):
    if state not in ('registered', 'rejected', 'uncertain', 'already_registered_external', 'ready', 'blocked_input'):
        raise ValueError('Unsupported terminal state')
    with db:
        prior = db.execute('SELECT intent_at FROM attempts WHERE id=? AND job=?', (attempt, job_id)).fetchone()
        if prior is None or (state == 'ready' and prior['intent_at']):
            raise ValueError('A submission intent cannot be silently reset to ready')
        db.execute('UPDATE jobs SET state=?,detail=?,receipt=?,tracking=?,updated_at=? WHERE id=?', (state, detail, str(receipt) if receipt else None, tracking, now(), job_id))
        db.execute('UPDATE attempts SET outcome=?,evidence=? WHERE id=?', (state, str(receipt) if receipt else None, attempt))


def check_receipt(path):
    try:
        receipt = json.loads(Path(path).read_text())
        return bool(receipt['files']) and all(Path(entry['path']).is_file() and sha(Path(entry['path']).read_bytes()) == entry['sha256'] for entry in receipt['files'].values())
    except (ValueError, OSError, KeyError, TypeError):
        return False
