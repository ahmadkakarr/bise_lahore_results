"""Frozen BISE record snapshot with references to original photo BLOBs.

This is a normalization source, not a standalone collector restore/backup.
No image payload is copied and the original database is opened read-only.
"""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def quoted(name):return '"'+name.replace('"','""')+'"'


def snapshot(source,output):
    source=Path(source).resolve();output=Path(output).resolve()
    if not source.is_file() or source==output:raise ValueError('Distinct existing source database required')
    output.parent.mkdir(parents=True,exist_ok=True)
    manifest_path=output.with_suffix('.manifest.json')
    with source.with_suffix('.lock').open('a') as source_lock,output.with_suffix('.lock').open('a') as output_lock:
        fcntl.flock(source_lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
        fcntl.flock(output_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        source_hash=sha(source)
        wal=Path(str(source)+'-wal')
        wal_hash=sha(wal) if wal.exists() and wal.stat().st_size else None
        if output.exists():
            saved=json.loads(manifest_path.read_text())
            if (saved['source_sha256']!=source_hash or saved.get('source_wal_sha256')!=wal_hash
                    or saved['snapshot_sha256']!=sha(output)):
                raise ValueError('Existing snapshot differs; preserve it and choose a new output path')
            return {**saved,'status':'already_up_to_date'}
        fd,name=tempfile.mkstemp(prefix='.records-only-',suffix='.sqlite3',dir=output.parent);os.close(fd)
        temporary=Path(name)
        try:
            with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(temporary)) as dst:
                src.execute('PRAGMA query_only=ON');src.execute('BEGIN')
                if src.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Source integrity failed')
                if src.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone():
                    raise ValueError('Stop/reconcile running collection claims before freezing records')
                schema=src.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
                counts={}
                for kind,table,_,sql in schema:
                    if kind!='table' or table=='student_photos':continue
                    dst.execute(sql)
                    columns=src.execute('PRAGMA table_info('+quoted(table)+')').fetchall()
                    if any('BLOB' in col[2].upper() for col in columns):raise ValueError('Unexpected image/binary column outside student_photos')
                    insert='INSERT INTO '+quoted(table)+' VALUES ('+','.join('?' for _ in columns)+')'
                    cursor=src.execute('SELECT * FROM '+quoted(table))
                    count=0
                    while True:
                        page=cursor.fetchmany(500)
                        if not page:break
                        dst.executemany(insert,page);count+=len(page)
                    counts[table]=count
                dst.execute('''CREATE TABLE photo_references(
                    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),sha256 TEXT NOT NULL,
                    mime_type TEXT NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL,
                    source_url TEXT NOT NULL,payload_bytes INTEGER NOT NULL,
                    source_database TEXT NOT NULL,source_table TEXT NOT NULL DEFAULT 'student_photos')''')
                dst.executemany('INSERT INTO photo_references VALUES(?,?,?,?,?,?,?,?,?)',
                    ((*row,str(source),'student_photos') for row in src.execute(
                        'SELECT job_id,sha256,mime_type,width,height,source_url,length(payload) FROM student_photos')))
                photo_bytes=src.execute('SELECT COALESCE(sum(length(payload)),0) FROM student_photos').fetchone()[0]
                for kind,_,table,sql in schema:
                    if kind in ('index','trigger','view') and table!='student_photos':dst.execute(sql)
                dst.commit()
                if dst.execute('PRAGMA quick_check').fetchall()!=[('ok',)] or dst.execute('PRAGMA foreign_key_check').fetchall():
                    raise ValueError('Records-only snapshot integrity failed')
                states=dict(dst.execute('SELECT state,count(*) FROM jobs GROUP BY state'))
                refs=dst.execute('SELECT count(*) FROM photo_references').fetchone()[0]
            if sha(source)!=source_hash or (sha(wal) if wal.exists() and wal.stat().st_size else None)!=wal_hash:
                raise ValueError('Source changed during snapshot; no output published')
            value={'status':'created','snapshot_kind':'records_only_with_external_photo_references',
                'created_at':datetime.now(timezone.utc).isoformat(),'source_database':str(source),
                'source_sha256':source_hash,'source_wal_sha256':wal_hash,'source_bytes':source.stat().st_size,
                'snapshot_database':str(output),'snapshot_sha256':sha(temporary),'snapshot_bytes':temporary.stat().st_size,
                'tables':counts,'states':states,'photo_references':refs,'photo_payload_bytes_copied':0,
                'original_photo_bytes_referenced':photo_bytes,'standalone_collector_restore':False,
                'source_mutated':False}
            output_manifest=manifest_path.with_suffix('.json.tmp')
            output_manifest.write_text(json.dumps(value,indent=2)+'\n');os.chmod(output_manifest,0o600)
            os.chmod(temporary,0o444);os.replace(temporary,output);os.replace(output_manifest,manifest_path)
            return value
        finally:temporary.unlink(missing_ok=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(snapshot(args.source,args.output),indent=2))
