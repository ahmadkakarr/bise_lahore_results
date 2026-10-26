"""Bounded live SSC migration/photo pilot; never calls the games portal."""
import argparse
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

from .common import atomic,counts,now
from .worker import collector_command,capacity
from .photo_recovery import run as recover


def identity(value):
    parsed=json.loads(value)
    for field in ('student_photo','student_photo_status','student_photo_error'):parsed.pop(field,None)
    return hashlib.sha256(json.dumps(parsed,sort_keys=True).encode()).hexdigest()


def run(root):
    from bise_results.media import read_asset
    from bise_results.store import retained_in_database
    root=Path(root).resolve();c=json.loads((root/'config.json').read_text())
    if capacity(root,c)[0]!='ready':raise ValueError('Pilot storage gate not ready')
    with closing(sqlite3.connect(root/'results.sqlite3')) as db:
        before={r[0]:identity(r[1]) for r in db.execute("SELECT id,result_json FROM jobs WHERE state='verified'")}
    before_counts=counts(root/'results.sqlite3')
    result=subprocess.run(collector_command(root,c,1,3),check=False)
    if result.returncode not in (0,2):raise RuntimeError('Pilot collector failed')
    asyncio.run(recover(root,3))
    with closing(sqlite3.connect(root/'results.sqlite3')) as db:
        db.row_factory=sqlite3.Row
        for row in db.execute("SELECT id,result_json FROM jobs WHERE state='verified'"):
            if row['id'] in before and identity(row['result_json'])!=before.pop(row['id']):raise ValueError('Existing board result changed')
        if before:raise ValueError('Existing saved row lost')
        if db.execute('SELECT count(*) FROM student_photos').fetchone()[0]:raise ValueError('New workflow wrote image BLOBs')
        photos=0
        for row in db.execute('SELECT j.* FROM jobs j JOIN photo_assets a ON a.job_id=j.id'):
            read_asset(db,row['id'],json.loads(row['result_json'])['student_photo'],verify=True)
            if not retained_in_database(db,row):raise ValueError('File-photo resume retention invalid')
            photos+=1
        if photos<2:raise ValueError('Insufficient original photo successes; inspect pilot')
        if db.execute('PRAGMA quick_check').fetchall()[0][0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Pilot database integrity failed')
    after=counts(root/'results.sqlite3')
    if after.get('verified',0)<=before_counts.get('verified',0):raise ValueError('Pilot saved no new result')
    report={'status':'passed','at':now(),'before':before_counts,'after':after,'original_photo_files':photos,
       'prior_identities_preserved':True,'blobs_written':0,'hashes_verified':True,'resume_retention_verified':True,'applicant_posts':0}
    atomic(root/'runtime/acceptance.json',report);print(json.dumps(report),flush=True)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);run(parser.parse_args().root)
