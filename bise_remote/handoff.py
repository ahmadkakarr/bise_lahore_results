"""Private, acknowledged source-to-central inbox transfer; no portal submissions.

Invoked only over the operator's authenticated host SSH/pct route. The central
inbox resides in the existing registration authority DB, not a cloned ledger.
"""
import argparse
import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from .common import now
from bise_results.media import checked_path,durable_asset
from bise_results.photos import inspect_photo


def export(root,limit=50):
    from youthgames_shared.catalog import load
    root=Path(root).resolve();load(root/'catalog.json')
    with closing(sqlite3.connect(root/'pipeline.sqlite3',timeout=30)) as db:
        rows=db.execute("SELECT revision,payload FROM ready_outbox WHERE state='ready' ORDER BY job_id LIMIT ?",(limit,)).fetchall()
    entries=[]
    for revision,payload in rows:
        record=json.loads(payload);ref=record['photo_reference']
        try:
            photo=checked_path(root/'photos',ref['relative_path']).read_bytes()
            if len(photo)!=ref['bytes'] or hashlib.sha256(photo).hexdigest()!=ref['sha256']:raise ValueError('Handoff original differs')
        except (OSError,ValueError):
            # One missing/corrupt asset must not head-of-line block every other
            # student. Queue source-only repair; never reset the saved result.
            with closing(sqlite3.connect(root/'results.sqlite3',timeout=30)) as source,source:
                source.execute("""INSERT INTO photo_recovery(job_id,state,updated_at) VALUES(?,'pending',?)
                  ON CONFLICT(job_id) DO UPDATE SET state='pending',retry_after=0,updated_at=excluded.updated_at
                  WHERE photo_recovery.state NOT IN ('running','quarantined')""",(ref['job_id'],now()))
                source.execute("INSERT INTO result_events(job_id,kind,at) VALUES(?,'photo_missing',?)",(ref['job_id'],now()))
            with closing(sqlite3.connect(root/'pipeline.sqlite3',timeout=30)) as destination,destination:
                destination.execute("UPDATE ready_outbox SET state='blocked_source_media' WHERE revision=? AND state='ready'",(revision,))
            continue
        entries.append({'revision':revision,'record':record,'photo':base64.b64encode(photo).decode()})
    return {'schema_version':1,'source':'ssc-2026-part1','portal':'https://youthgames.punjab.gov.pk','campaign':'punjab-youthgames-2026','entries':entries}


def ingest(database,root,message):
    from bise_results.media import key
    if (message.get('schema_version')!=1 or message.get('source')!='ssc-2026-part1' or message.get('portal')!='https://youthgames.punjab.gov.pk'
        or message.get('campaign')!='punjab-youthgames-2026' or len(message.get('entries',[]))>100):raise ValueError('Invalid central source envelope')
    database=Path(database).resolve()
    if not database.is_file():raise ValueError('Existing canonical registration DB required; never seed a second ledger')
    with closing(sqlite3.connect(database,timeout=30)) as db:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA busy_timeout=30000')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='registered_applicants'").fetchone():raise ValueError('Canonical registration schema missing')
        db.executescript('''CREATE TABLE IF NOT EXISTS source_inbox(source TEXT,revision TEXT,portal TEXT,campaign TEXT,applicant_key TEXT,
          source_job INTEGER,payload TEXT,photo TEXT,state TEXT,received_at TEXT,PRIMARY KEY(source,revision));
          CREATE INDEX IF NOT EXISTS source_inbox_identity ON source_inbox(portal,campaign,applicant_key,state);''')
        receipts=[]
        for entry in message['entries']:
            record=entry['record'];ref=record['photo_reference'];person=key(record['data']['BForm'])
            version={**record,'source':{k:v for k,v in record['source'].items() if k!='event_seq'}}
            revision=hashlib.sha256(json.dumps(version,sort_keys=True).encode()).hexdigest()
            if (entry['revision']!=revision or person!=ref['applicant_key'] or record['roll']!=ref['roll']
               or (ref.get('year'),ref.get('course'),ref.get('exam_type'))!=(2026,'SSC','1')):raise ValueError('Handoff identity/revision mismatch')
            data=base64.b64decode(entry['photo'],validate=True);meta=inspect_photo(data)
            if meta['sha256']!=ref['sha256'] or meta['bytes']!=ref['bytes']:raise ValueError('Handoff portrait mismatch')
            relative=durable_asset(root,data,meta)
            if relative!=ref['relative_path']:raise ValueError('Handoff content address differs')
            with db:
                db.execute('BEGIN IMMEDIATE')
                confirmed=db.execute('SELECT status,tracking FROM registered_applicants WHERE portal=? AND campaign=? AND applicant_key=?',
                    (message['portal'],message['campaign'],person)).fetchone()
                existing=db.execute('SELECT state FROM jobs WHERE portal=? AND campaign=? AND applicant_key=?',
                    (message['portal'],message['campaign'],person)).fetchone()
                state='already_registered' if confirmed else 'existing_central_job' if existing else 'awaiting_registration_policy'
                db.execute('INSERT OR IGNORE INTO source_inbox VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (message['source'],revision,message['portal'],message['campaign'],person,record['source']['job_id'],json.dumps(record),
                     json.dumps({**ref,'source_media_root':str(Path(root).resolve())}),state,now()))
                if confirmed:
                    db.execute("UPDATE source_inbox SET state='already_registered' WHERE portal=? AND campaign=? AND applicant_key=?",
                       (message['portal'],message['campaign'],person))
                else:
                    db.execute("UPDATE source_inbox SET state='superseded' WHERE source=? AND source_job=? AND revision!=? AND state='awaiting_registration_policy'",
                       (message['source'],record['source']['job_id'],revision))
                saved=db.execute('SELECT state FROM source_inbox WHERE source=? AND revision=?',(message['source'],revision)).fetchone()[0]
            receipts.append({'revision':revision,'state':saved,'tracking':confirmed['tracking'] if confirmed else None})
        return {'source':message['source'],'receipts':receipts,'registration_started':False}


def acknowledge(root,message):
    if message.get('source')!='ssc-2026-part1':raise ValueError('Wrong source acknowledgement')
    with closing(sqlite3.connect(Path(root)/'pipeline.sqlite3',timeout=30)) as db,db:
        db.execute('''CREATE TABLE IF NOT EXISTS delivery_receipts(revision TEXT PRIMARY KEY,state TEXT,tracking TEXT,acknowledged_at TEXT)''')
        for receipt in message['receipts']:
            if receipt['state'] not in ('awaiting_registration_policy','existing_central_job','already_registered'):raise ValueError('Invalid central receipt')
            row=db.execute('SELECT revision FROM ready_outbox WHERE revision=?',(receipt['revision'],)).fetchone()
            if not row:raise ValueError('Unknown ready revision')
            db.execute('INSERT OR REPLACE INTO delivery_receipts VALUES(?,?,?,?)',(receipt['revision'],receipt['state'],receipt.get('tracking'),now()))
            db.execute("UPDATE ready_outbox SET state='delivered',acknowledged_at=? WHERE revision=?",(now(),receipt['revision']))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('export','ingest','ack'));parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--database',type=Path);parser.add_argument('--limit',type=int,default=50);args=parser.parse_args()
    if args.action=='export':result=export(args.root,args.limit)
    elif args.action=='ingest':result=ingest(args.database,args.root,json.load(sys.stdin))
    else:result=acknowledge(args.root,json.load(sys.stdin))
    print(json.dumps(result))
