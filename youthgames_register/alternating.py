"""Durable next-target state; concurrency never changes the intended POST order."""
import asyncio
from collections import deque
import json


class Alternating:
    def __init__(self,db,config,references,limit=0):
        self.db=db;self.key=config['selection']['dispatch_key'];self.pattern=config['selection']['dispatch_pattern']
        db.executescript('''CREATE TABLE IF NOT EXISTS alternating_turns(
          key TEXT PRIMARY KEY,next_group TEXT NOT NULL,sequence INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS alternating_dispatches(
          key TEXT NOT NULL,sequence INTEGER NOT NULL,job INTEGER NOT NULL,group_name TEXT NOT NULL,
          dispatched_at TEXT NOT NULL,PRIMARY KEY(key,sequence));''')
        with db:db.execute('INSERT OR IGNORE INTO alternating_turns VALUES(?,?,0)',(self.key,self.pattern[0]))
        target=db.execute('SELECT next_group FROM alternating_turns WHERE key=?',(self.key,)).fetchone()[0]
        groups={group:deque() for group in self.pattern};self.groups={}
        for ref in references:
            row=db.execute('SELECT payload FROM jobs WHERE id=?',(ref['id'],)).fetchone()
            group=json.loads(row[0])['data']['Tehsil']
            if group not in groups:raise ValueError('Applicant belongs to an unexpected alternating group')
            groups[group].append(ref);self.groups[ref['id']]=group
        ordered=[]
        while any(groups.values()):
            if not groups[target]:target=next(g for g in self.pattern if groups[g])
            ordered.append(groups[target].popleft());target=self.pattern[(self.pattern.index(target)+1)%len(self.pattern)]
        self.references=ordered[:limit] if limit else ordered
        self.ids=deque(r['id'] for r in self.references);self.changed=asyncio.Event()

    def enqueue(self,job):
        if job not in self.ids:self.ids.append(job)

    async def wait(self,job,stop):
        while not stop.is_set():
            if self.ids and self.ids[0]==job:return
            changed=self.changed
            try:await asyncio.wait_for(changed.wait(),1)
            except TimeoutError:pass
        raise asyncio.CancelledError()

    def release(self,job):
        try:self.ids.remove(job)
        except ValueError:return
        event=self.changed;self.changed=asyncio.Event();event.set()

    def record(self,job,at):
        if not self.ids or self.ids[0]!=job:raise ValueError('POST dispatch order changed')
        group=self.groups[job];target=self.pattern[(self.pattern.index(group)+1)%len(self.pattern)]
        sequence=self.db.execute('SELECT sequence FROM alternating_turns WHERE key=?',(self.key,)).fetchone()[0]+1
        self.db.execute('INSERT INTO alternating_dispatches VALUES(?,?,?,?,?)',(self.key,sequence,job,group,at))
        self.db.execute('UPDATE alternating_turns SET sequence=?,next_group=? WHERE key=?',(sequence,target,self.key))
