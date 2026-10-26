"""Independent portrait backfill. Never resets or overwrites a saved board identity."""
import argparse
import asyncio
import fcntl
import json
from pathlib import Path
import shutil
import signal
import sqlite3
import time
from types import SimpleNamespace

from playwright.async_api import async_playwright
from bise_results.media import key, save_file
from bise_results.store import now, retain_in_database
from bise_results.runner import lookup, Pacer
from .common import atomic
from .worker import capacity


def connect(root):
    db=sqlite3.connect(root/'results.sqlite3',timeout=30);db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA busy_timeout=30000')
    return db


def claim_photo(db,at=None):
    """Choose from a compact ordered queue before reading a single result JSON."""
    with db:
        db.execute('BEGIN IMMEDIATE')
        candidate=db.execute("""SELECT job_id,attempts FROM photo_recovery INDEXED BY photo_recovery_ready
          WHERE state IN ('pending','retry') AND retry_after<=?
          ORDER BY attempts,job_id LIMIT 1""",(time.time() if at is None else at,)).fetchone()
        if candidate is None:return None
        row=db.execute("SELECT * FROM jobs WHERE id=? AND state='verified'",(candidate['job_id'],)).fetchone()
        if row is None:
            db.execute("UPDATE photo_recovery SET state='quarantined',detail='Source result is no longer verified',updated_at=? WHERE job_id=?",(now(),candidate['job_id']))
            return None
        db.execute("UPDATE photo_recovery SET state='running',attempts=attempts+1,updated_at=? WHERE job_id=?",(now(),row['id']))
        return {**dict(row),'photo_attempts':candidate['attempts']}


def commit_photo(db,job,fetched,payload,root):
    """Compare again under the write lock; the original identity stays authoritative."""
    with db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
        old=json.loads(row['result_json'])
        for field in ('roll','year','course','exam_type','name','father_name','date_of_birth'):
            if str(old.get(field,''))!=str(fetched.get(field,'')):
                raise ValueError('Backfill identity mismatch: '+field)
        for field in ('student_cnic','father_cnic'):
            if key(old.get(field))!=key(fetched.get(field)):
                raise ValueError('Backfill identity mismatch: '+field)
        old['student_photo']=fetched['student_photo']
        save_file(db,row['id'],old,payload,root/'photos')
        db.execute('UPDATE jobs SET result_json=?,updated_at=? WHERE id=?',(json.dumps(old),now(),row['id']))
        refreshed=db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone()
        retain_in_database(db,refreshed,'Original portrait recovered; original board identity preserved',commit=False)
        db.execute("UPDATE photo_recovery SET state='complete',detail=NULL,updated_at=? WHERE job_id=?",(now(),row['id']))
        # Replacing corrupt bytes with the same original hash is still a new
        # availability event, even if result JSON and reference are identical.
        db.execute("INSERT INTO result_events(job_id,kind,at) VALUES(?,'photo_recovered',?)",(row['id'],now()))


async def run(root,limit=0):
    root=Path(root).resolve();config=json.loads((root/'config.json').read_text())
    stopped=asyncio.Event();loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,stopped.set)
    with (root/'runtime/photos.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=connect(root)
        with db:
            db.execute("UPDATE photo_recovery SET state='pending' WHERE state='running'")
            db.execute("""INSERT OR IGNORE INTO photo_recovery(job_id,updated_at)
              SELECT id,updated_at FROM jobs WHERE state='verified' AND year=2026 AND course='SSC' AND exam_type='1'
              AND NOT EXISTS(SELECT 1 FROM photo_assets WHERE job_id=jobs.id)
              AND NOT EXISTS(SELECT 1 FROM photo_recovery WHERE job_id=jobs.id)""")
        completed=0;attempted=0
        args=SimpleNamespace(captcha_mode='auto',save_student_photos=True,photo_storage='files',missing_dob_policy='require')
        try:
            async with async_playwright() as playwright:
                browser=await playwright.chromium.launch(headless=True,executable_path=config.get('browser_executable','/usr/bin/chromium'))
                context=await browser.new_context();page=await context.new_page();pacer=Pacer(config['interval_seconds'])
                while not stopped.is_set() and (not limit or attempted<limit):
                    if capacity(root,config)[0]!='ready':
                        atomic(root/'runtime/photos.json',{'status':'paused_storage','at':now()});await asyncio.sleep(5);continue
                    row=claim_photo(db)
                    if not row:
                        atomic(root/'runtime/photos.json',{'status':'waiting_for_results','at':now(),'saved_this_run':completed})
                        if limit:break
                        await asyncio.sleep(5);continue
                    job=dict(row);attempted+=1;folder=root/'photo-evidence'/f"job-{job['id']}"
                    folder.mkdir(parents=True,exist_ok=True)
                    atomic(root/'runtime/photos.json',{'status':'fetching','job':job['id'],'at':now(),'saved_this_run':completed})
                    try:
                        fetched,_=await lookup(page,job,folder,args,pacer,asyncio.Lock())
                        metadata=fetched.get('student_photo')
                        if not metadata:raise ValueError('Board portrait unavailable')
                        payload=(folder/('student-photo.'+metadata['extension'])).read_bytes()
                        commit_photo(db,job,fetched,payload,root);completed+=1
                        print(json.dumps({'job':job['id'],'photo':'saved','sha256':metadata['sha256']}),flush=True)
                    except Exception as exc:
                        if not browser.is_connected():raise RuntimeError('Portrait browser disconnected; service will recover') from exc
                        mismatch='identity mismatch' in str(exc)
                        delay=min(86400,60*2**min(job['photo_attempts'],10))
                        with db:db.execute('UPDATE photo_recovery SET state=?,retry_after=?,detail=?,updated_at=? WHERE job_id=?',
                          ('quarantined' if mismatch else 'retry',time.time()+delay,str(exc)[:500],now(),job['id']))
                        print(json.dumps({'job':job['id'],'photo':'quarantined' if mismatch else 'retry','reason':str(exc)[:500]}),flush=True)
                    finally:
                        # No permanent portrait ever lives under photo-evidence.
                        shutil.rmtree(folder)
                await context.close();await browser.close()
        finally:
            db.close();atomic(root/'runtime/photos.json',{'status':'stopped' if stopped.is_set() else 'pilot_complete','at':now(),'saved_this_run':completed})


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--limit',type=int,default=0)
    args=parser.parse_args();asyncio.run(run(args.root,args.limit))
