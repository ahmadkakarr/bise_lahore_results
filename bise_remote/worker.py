"""Guest-side supervised collector; commits and resumes independently of SSH."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time

from .common import atomic, counts, digest, now


def evidence_size(root):
    total=0
    for path in (root/'evidence').rglob('*'):
        try:
            if path.is_file() and not path.is_symlink():total+=path.stat().st_size
        except FileNotFoundError:
            pass  # The collector can delete a committed capture during this scan.
    return total


def collector_command(root,c,workers,limit):
    command=[sys.executable,'-u','-m','bise_results','collect',str(root/'input/rolls.csv'),
        '--year',str(c['year']),'--class',str(c['class']),'--workers',str(workers),
        '--ocr-workers',str(c['ocr_workers']),'--interval',str(c['interval_seconds']),
        '--limit',str(limit),'--database',str(root/'results.sqlite3'),'--artifacts',str(root/'evidence'),
        '--headless','--captcha-mode','auto','--no-alarm','--min-available-memory-mb','512',
        '--min-free-disk-gib',str(c['disk_pause_gib']),'--max-evidence-mib',str(c['evidence_quota_mib']),
        '--missing-dob-policy',c.get('missing_dob_policy','require'),
        '--estimated-age-at-exam',str(c.get('estimated_age_at_exam',18))]
    if c.get('save_student_photos'):command.append('--save-student-photos')
    if c.get('browser_executable'):command+=['--browser-executable',c['browser_executable']]
    return command


def capacity(root, c, paused=False):
    free = shutil.disk_usage(root).free
    evidence = evidence_size(root)
    minimum = c['disk_resume_gib'] if paused else c['disk_pause_gib']
    if evidence >= c['evidence_quota_mib']*1024**2:
        return 'paused_evidence_quota', free, evidence
    if free < minimum*1024**3 or os.statvfs(root).f_favail < 128:
        return 'paused_storage', free, evidence
    host = root/'runtime/host-storage.json'
    if host.exists():
        try:
            value = json.loads(host.read_text())
            healthy=value.get('safe') is True and 0 <= time.time()-value['observed_at'] <= 180
        except (OSError,ValueError,KeyError,TypeError):healthy=False
        if not healthy:
            return 'paused_host_storage', free, evidence
    else:
        return 'paused_host_storage', free, evidence
    return 'ready', free, evidence


def sweep(root):
    """Recover the crash window after a verified DB commit, before cleanup."""
    from bise_results.store import locked_store
    from bise_results.retention import discard_verified_capture
    path=root/'results.sqlite3'
    if not path.exists():return
    with locked_store(path) as db:
        for folder in (root/'evidence').glob('job-*'):
            if folder.is_symlink() or not folder.name[4:].isdigit():continue
            row=db.execute('SELECT state FROM jobs WHERE id=?',(int(folder.name[4:]),)).fetchone()
            if row and row['state']=='verified':
                try:discard_verified_capture(db,int(folder.name[4:]),folder)
                except (OSError,ValueError):pass


def run(root):
    root=Path(root).resolve();c=json.loads((root/'config.json').read_text())
    if c.get('blocked_reason'):
        atomic(root/'runtime/status.json',{'status':'held_missing_dob_source',
               'reason':c['blocked_reason'],'heartbeat_at':now(),'collector_started':False})
        return 2
    if c.get('input_sha256') and digest(root/'input/rolls.csv')!=c['input_sha256']:
        atomic(root/'runtime/status.json',{'status':'held_input_checksum','heartbeat_at':now()})
        return 2
    request=json.loads((root/'runtime/request.json').read_text())
    stopped=False;child=None;stop_deadline=None;terminated=False
    def interrupt():
        nonlocal stop_deadline
        if child and child.poll() is None and stop_deadline is None:
            child.send_signal(signal.SIGINT);stop_deadline=time.monotonic()+60
    def stop(*args):
        nonlocal stopped
        stopped=True
        interrupt()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    state={'job':c['job'],'started_at':now(),'status':'starting','limit':request['limit'],
           'workers':request.get('workers',c['workers']),'last_saved_at':None,'request_id':request['id'],
           'expected_records':c.get('expected_records'), 'worker_activity':{}}
    database=root/'results.sqlite3';last_checkpoint=0;failures=0
    state['saved_at_start']=counts(database).get('verified',0)
    def publish(status,**extra):
        try:summary=counts(database)
        except sqlite3.Error:summary=state.get('counts',{'total':0})
        state.update(status=status,heartbeat_at=now(),counts=summary,**extra)
        atomic(root/'runtime/status.json',state)
        return summary
    with (root/'runtime/worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        sweep(root)
        storage_paused=False
        while not stopped:
            status,free,evidence=capacity(root,c,storage_paused)
            if status!='ready':
                if status=='paused_storage':storage_paused=True
                publish(status,free_disk_bytes=free,evidence_bytes=evidence)
                for _ in range(10):
                    if stopped:break
                    time.sleep(1)
                continue
            storage_paused=False;stop_deadline=None;terminated=False
            command=collector_command(root,c,state['workers'],request['limit'])
            child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
            lines=queue.Queue()
            def pump():
                for line in child.stdout:lines.put(line)
                lines.put(None)
            reader=threading.Thread(target=pump,daemon=True);reader.start()
            last_line=time.monotonic();last_heartbeat=0;reason=None;log_path=root/'runtime/events.jsonl'
            log=log_path.open('a');ended=False
            while not ended:
                try:line=lines.get(timeout=1)
                except queue.Empty:line=''
                if line is None:ended=True
                elif line:
                    last_line=time.monotonic()
                    if log.tell()>20*1024**2:
                        log.close()
                        for n in (2,1):
                            previous=log_path.with_suffix('.'+str(n))
                            if previous.exists():previous.replace(log_path.with_suffix('.'+str(n+1)))
                        log_path.replace(log_path.with_suffix('.1'));log=log_path.open('a')
                    log.write(line);log.flush()
                    try:
                        event=json.loads(line)
                        state['activity']=event
                        if event.get('worker'):
                            state['worker_activity'][str(event['worker'])]=event
                        if event.get('reason') in ('evidence_quota','low_disk_space'):reason=event['reason']
                        if event.get('state')=='verified':state['last_saved_at']=now()
                    except ValueError:pass
                if time.monotonic()-last_heartbeat>5:
                    summary=publish('running',pid=child.pid,free_disk_bytes=shutil.disk_usage(root).free)
                    saved=summary.get('verified',0)
                    if saved//500>last_checkpoint//500:
                        atomic(root/'checkpoints/latest.json',{'at':now(),'counts':summary,
                               'input_sha256':c['input_sha256'],'source_sha256':c['source_sha256']})
                        last_checkpoint=saved
                    last_heartbeat=time.monotonic()
                    guard=capacity(root,c)[0]
                    if guard!='ready' and child.poll() is None:
                        reason=guard
                        if guard=='paused_storage':storage_paused=True
                        interrupt()
                if time.monotonic()-last_line>600 and child.poll() is None:
                    reason='watchdog_no_activity';interrupt()
                if stop_deadline and child.poll() is None and time.monotonic()>stop_deadline:
                    if not terminated:
                        child.terminate();terminated=True;stop_deadline=time.monotonic()+15
                    else:
                        child.kill();stop_deadline=None
            code=child.wait();log.close();reader.join(timeout=5)
            summary=publish('checking',exit_code=code)
            atomic(root/'checkpoints/latest.json',{'at':now(),'counts':summary,
                   'input_sha256':c['input_sha256'],'source_sha256':c['source_sha256']})
            if stopped:break
            if reason:
                publish('recovering',reason=reason);continue
            if code not in (0,2):
                failures+=1;publish('recovering',reason='collector_exit',retry_in_seconds=min(300,30*failures))
                for _ in range(min(300,30*failures)):
                    if stopped:break
                    time.sleep(1)
                continue
            if request['limit']:
                publish('pilot_complete' if code==0 else 'pilot_complete_with_issues');return 0
            if summary.get('pending',0)==0:
                publish('complete_with_issues' if summary.get('failed',0)+summary.get('needs_review',0) else 'complete')
                return 0
        publish('stopped');return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    raise SystemExit(run(parser.parse_args().root))
