"""Derived progress only; registration receipts/SQLite remain authoritative."""
import fcntl
import json
import os
from pathlib import Path
import tempfile

TERMINAL={'registered','already_registered_external'}
PENDING={'ready','filling','filled','submitting','not_prepared'}


def bucket(state,eligible=True):
    if state in TERMINAL:return 'completed'
    return 'remaining' if eligible and state in PENDING else 'held'


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    handle,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(handle,'w') as stream:stream.write(json.dumps(value,indent=2)+'\n')
        temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)


def atomic_current(path,value):
    """A draining older run cannot overwrite a newer run's current counts."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:previous=json.loads(path.read_text())
        except (FileNotFoundError,ValueError):previous={}
        stamp=previous.get('run_started_at') if isinstance(previous,dict) else None
        if isinstance(stamp,str) and stamp>value.get('run_started_at',''):return
        atomic_json(path,value)


class BatchProgress:
    def __init__(self):
        self.totals={};self.jobs={};self.completed_at_start=None

    def seed(self,values):
        counts={key:int(values[key]) for key in ('total','completed','remaining','held')}
        if min(counts.values())<0 or counts['total']!=sum(counts[k] for k in ('completed','remaining','held')):
            raise ValueError('Batch progress counts do not balance')
        jobs={str(k):[dict(part) for part in v] for k,v in values.get('jobs',{}).items()}
        if self.completed_at_start is None:self.completed_at_start=counts['completed']
        self.jobs=jobs;self.totals=counts

    def transition(self,job,state):
        records=self.jobs.get(str(job))
        if records is None:return
        totals=dict(self.totals);updated=[]
        # Source duplicates may share a job while having different eligibility.
        for record in records:
            old=record['bucket'];new=bucket(state,record['eligible'])
            if old=='completed':new=old  # Never undo a confirmed success.
            totals[old]-=record['rows'];totals[new]+=record['rows']
            updated.append({**record,'bucket':new})
        self.jobs[str(job)]=updated;self.totals=totals

    def summary(self):
        totals=self.totals  # Rich renders concurrently; retain one immutable version.
        if not totals:return {}
        return {**totals,'completed_before_run':self.completed_at_start,
            'completed_this_run':totals['completed']-self.completed_at_start}


def refresh_ui(ui):
    refresh=getattr(ui,'refresh_batch',None)
    if callable(refresh):refresh()
