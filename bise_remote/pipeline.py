"""Incremental source-event normalization and a durable, revisioned ready outbox.

This service never submits applications. A central registration consumer must
claim entries against its campaign/BForm ledger before a portal POST.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import signal
import sqlite3
import time

from .common import atomic, now

SCHEMA='''
CREATE TABLE IF NOT EXISTS cursor(id INTEGER PRIMARY KEY CHECK(id=1),seq INTEGER NOT NULL,policy TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS normalized(job_id INTEGER PRIMARY KEY,bform TEXT,state TEXT NOT NULL,payload TEXT NOT NULL,revision TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS normalized_identity ON normalized(bform);
CREATE INDEX IF NOT EXISTS normalized_state ON normalized(state,job_id);
CREATE TABLE IF NOT EXISTS ready_outbox(revision TEXT PRIMARY KEY,job_id INTEGER NOT NULL,bform TEXT NOT NULL,
 payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'ready',created_at TEXT NOT NULL,acknowledged_at TEXT);
CREATE INDEX IF NOT EXISTS ready_delivery ON ready_outbox(state,job_id);
'''


def process(root,limit=500):
    from bise_normalize.config import load
    from bise_normalize.engine import Prepared, transform
    from bise_results.presentation import LABELS, display
    from bise_results.reports import RESULT_COLUMNS
    from bise_results.media import reference,read_asset
    from youthgames_shared.catalog import load as catalog_load,validate_fields,age_eligibility
    root=Path(root).resolve();settings=load(root/'normalization.json')
    try:catalog=catalog_load(root/'catalog.json',max_age_hours=settings['portal']['max_catalog_age_hours']);catalog_error=None
    except (OSError,ValueError) as exc:catalog=None;catalog_error=str(exc)
    policy=hashlib.sha256((json.dumps(settings,sort_keys=True)+(root/'catalog.json').read_text()).encode()).hexdigest()
    source=sqlite3.connect((root/'results.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=30);source.row_factory=sqlite3.Row
    destination=sqlite3.connect(root/'pipeline.sqlite3',timeout=30);destination.row_factory=sqlite3.Row
    destination.execute('PRAGMA journal_mode=WAL');destination.executescript(SCHEMA)
    prepared=Prepared(':memory:')
    try:
        previous=destination.execute('SELECT * FROM cursor WHERE id=1').fetchone()
        if previous and previous['policy']!=policy:
            # Reevaluate all source events on an explicit settings/catalog change.
            # Never erase delivered history or advance an old policy's cursor.
            with destination:destination.execute('UPDATE cursor SET seq=0,policy=? WHERE id=1',(policy,))
            previous=destination.execute('SELECT * FROM cursor WHERE id=1').fetchone()
        seq=previous['seq'] if previous else 0
        source.execute('BEGIN')
        events=source.execute('SELECT seq,job_id FROM result_events WHERE seq>? ORDER BY seq LIMIT ?',(seq,limit)).fetchall()
        with destination:
            for event in events:
                job=source.execute('SELECT * FROM jobs WHERE id=?',(event['job_id'],)).fetchone()
                if not job or job['state']!='verified':continue
                record=json.loads(job['result_json']);record['state']='verified'
                raw={LABELS[k]:str(display(record,k) or '') if k!='result_card' else '' for k in RESULT_COLUMNS}
                if (job['year'],job['course'],job['exam_type'])!=(2026,'SSC','1'):
                    raise ValueError('Source contains another exam; checkpoint preserved')
                item=transform(raw,job['id'],settings,prepared,[])
                photo_issues=[]
                try:
                    ref=reference(source,job);read_asset(source,job['id'],record.get('student_photo',{}),verify=True)
                    item['photo_reference']=ref
                    if ref['bytes']>262144:photo_issues.append('photo_exceeds_portal_limit')
                except (OSError,ValueError,KeyError) as exc:photo_issues.append('original_photo_missing_or_invalid')
                if catalog:
                    values,ids,issues,changes=validate_fields(item['data'],catalog);item['data']=values
                    age=age_eligibility(values,catalog)
                else:
                    ids={};issues=['portal_catalog_unavailable'];age={'state':'unknown'}
                item['portal']={'age':age,'issues':issues,'option_ids':ids}
                item['issues']=sorted(set(item['issues']+photo_issues))
                state=('age_ineligible' if age['state']=='ineligible' else 'ready' if age['state']=='eligible' and not issues and not item['issues'] else 'blocked')
                item['source']={'job_id':job['id'],'year':2026,'course':'SSC','exam_type':'1','event_seq':event['seq']}
                # Revision excludes event sequence: duplicate events produce no duplicate delivery.
                version={**item,'source':{k:v for k,v in item['source'].items() if k!='event_seq'}}
                revision=hashlib.sha256(json.dumps(version,sort_keys=True).encode()).hexdigest()
                payload=json.dumps(item,ensure_ascii=False,sort_keys=True)
                destination.execute('INSERT INTO normalized VALUES(?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET bform=excluded.bform,state=excluded.state,payload=excluded.payload,revision=excluded.revision,updated_at=excluded.updated_at',
                   (job['id'],item['data']['BForm'],state,payload,revision,now()))
                destination.execute("UPDATE ready_outbox SET state='superseded' WHERE job_id=? AND revision!=? AND state='ready'",(job['id'],revision))
                if state=='ready':destination.execute("""INSERT INTO ready_outbox(revision,job_id,bform,payload,created_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(revision) DO UPDATE SET state='ready' WHERE ready_outbox.state IN ('superseded','blocked_source_media')""",
                    (revision,job['id'],item['data']['BForm'],payload,now()))
                else:destination.execute("UPDATE ready_outbox SET state='superseded' WHERE job_id=? AND state='ready'",(job['id'],))
                # Duplicate identity quarantines all undelivered members, including earlier pages.
                identity=item['data']['BForm']
                if identity and destination.execute('SELECT count(*) FROM normalized WHERE bform=?',(identity,)).fetchone()[0]>1:
                    destination.execute("UPDATE normalized SET state='identity_conflict' WHERE bform=?",(identity,))
                    destination.execute("UPDATE ready_outbox SET state='superseded' WHERE bform=? AND state='ready'",(identity,))
            if events:
                destination.execute('INSERT INTO cursor VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET seq=excluded.seq,policy=excluded.policy',(events[-1]['seq'],policy))
        result=dict(destination.execute('SELECT state,count(*) FROM normalized GROUP BY state'))
        result.update(event_cursor=events[-1]['seq'] if events else seq,events_processed=len(events),registration_enabled=False)
        atomic(root/'runtime/pipeline.json',{'status':'catalog_unavailable' if catalog_error else 'running','catalog_error':catalog_error,'at':now(),**result})
        return result
    finally:prepared.close();source.close();destination.close()


def run(root,once=False):
    root=Path(root).resolve();stopped=False
    def stop(*args):
        nonlocal stopped
        stopped=True
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    with (root/'runtime/pipeline.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while not stopped:
            from youthgames_shared.catalog import ensure_fresh
            try:ensure_fresh(root/'catalog.json')
            except Exception as exc:
                # Preserve last catalog; per-record eligibility stays blocked
                # until the official rules can be refreshed successfully.
                atomic(root/'runtime/catalog-health.json',{'status':'unavailable','at':now(),'error':str(exc)[:300]})
            result=process(root)
            if once:return result
            if not result['events_processed']:
                for _ in range(5):
                    if stopped:break
                    time.sleep(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--once',action='store_true')
    args=parser.parse_args();print(json.dumps(run(args.root,args.once)))
