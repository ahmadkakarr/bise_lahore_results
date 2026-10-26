"""Supervise the existing HTTP registration runner; no second submission engine."""
import argparse
import asyncio
from contextlib import closing
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time

from .common import atomic,now


class Status:
    def __init__(self,root,config,pilot):
        self.root=root;self.config=config;self.pilot=pilot;self.started=now();self.workers={}
        self.stage='starting';self.expected=2 if pilot else json.loads((root/'reports/Summary.json').read_text())['total_ready']
        self.lock=threading.Lock();self.stop=threading.Event();self.result={}
        self.thread=threading.Thread(target=self.loop,daemon=True)

    def attach(self,directory):self.directory=directory

    def event(self,phase,**values):
        with self.lock:
            if values.get('worker'):self.workers[str(values['worker'])]={**values,'phase':values.get('state',phase)}
            if phase=='stage':self.stage=values.get('message',phase)
            with (self.root/'runtime/events.jsonl').open('a') as stream:
                stream.write(json.dumps({'at':now(),'phase':phase,**values})+'\n')

    def publish(self):
        counts={};groups={};tracking=[]
        with closing(sqlite3.connect((self.root/'state/registration.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            rows=db.execute("SELECT json_extract(payload,'$.data.Tehsil'),state,count(*) FROM jobs GROUP BY 1,2").fetchall()
            for group,state,number in rows:
                groups.setdefault(group,{})[state]=number;counts[state]=counts.get(state,0)+number
            tracking=[{'roll':r[0],'group':r[1],'tracking':r[2]} for r in db.execute("SELECT roll,json_extract(payload,'$.data.Tehsil'),tracking FROM jobs WHERE state='registered' ORDER BY updated_at DESC LIMIT 5")]
        with self.lock:
            value={'kind':'registration','job':'Inter 2024 Universities / Open Category','mode':'pilot' if self.pilot else 'full',
                'started_at':self.started,'heartbeat_at':now(),'workers':4,'transport':'httpx','stage':self.stage,
                'status':self.result.get('status','running'),'expected_records':self.expected,
                'counts':{'verified':counts.get('registered',0)+counts.get('already_registered_external',0),
                          'pending':sum(counts.get(k,0) for k in ('ready','filling','filled','submitting')),
                          'needs_review':sum(counts.get(k,0) for k in ('uncertain','blocked_input','rejected')),
                          'failed':0,'total':self.expected},
                'registration_states':counts,'groups':groups,'recent_tracking':tracking,
                'worker_activity':dict(self.workers),'disk_free_bytes':shutil.disk_usage(self.root).free,
                'provisional_dob':True,'result':self.result}
        atomic(self.root/'runtime/status.json',value)

    def loop(self):
        while not self.stop.wait(3):
            try:self.publish()
            except (OSError,sqlite3.Error):pass


def run(root,pilot=True):
    from youthgames_register.http_async import run as submit
    root=Path(root).resolve();profile=root/'config'/('register-pilot.json' if pilot else 'register.json')
    config=json.loads(profile.read_text());status=Status(root,config,pilot);status.thread.start()
    try:
        result=asyncio.run(submit(profile,workers=4,limit=2 if pilot else 0,ui=status,
                                 prepare_ahead=2,max_in_flight=2,auto_recover=True,dispatch_interval=6,
                                 retry_uncertain=not pilot,uncertain_retries=2))
        if pilot:
            with closing(sqlite3.connect(root/'state/registration.sqlite3')) as db:
                rows=db.execute("SELECT roll,state,tracking FROM jobs WHERE roll IN (?,?)",config['selection']['rolls']).fetchall()
            result['pilot_passed']=len(rows)==2 and all(row[1]=='registered' and row[2] for row in rows)
        status.result=result
        atomic(root/'runtime/last-result.json',result)
        return 0 if result.get('status')=='complete' and not result.get('blocked') and (not pilot or result['pilot_passed']) else 2
    except BaseException as error:
        status.result={'status':'failed','error_type':type(error).__name__,'detail':str(error)[:500]}
        raise
    finally:
        status.stop.set();status.thread.join(timeout=5);status.publish()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--full',action='store_true');args=parser.parse_args()
    raise SystemExit(run(args.root,not args.full))
