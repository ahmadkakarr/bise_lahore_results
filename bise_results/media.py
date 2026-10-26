"""Immutable portrait files and exam-bound database references; no image BLOBs."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


SCHEMA='''
CREATE TABLE IF NOT EXISTS media_roots(name TEXT PRIMARY KEY,path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS photo_assets(job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
 root_name TEXT NOT NULL,relative_path TEXT NOT NULL,sha256 TEXT NOT NULL,
 mime_type TEXT NOT NULL,bytes INTEGER NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL,
 source_url TEXT NOT NULL,applicant_key TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS result_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS result_events_job ON result_events(job_id,seq);
CREATE TRIGGER IF NOT EXISTS source_result_insert AFTER INSERT ON jobs WHEN NEW.state='verified'
 BEGIN INSERT INTO result_events(job_id,kind,at) VALUES(NEW.id,'result',NEW.updated_at);END;
CREATE TRIGGER IF NOT EXISTS source_result_update AFTER UPDATE OF result_json,state ON jobs
 WHEN NEW.state='verified' AND (OLD.state!=NEW.state OR OLD.result_json IS NOT NEW.result_json)
 BEGIN INSERT INTO result_events(job_id,kind,at) VALUES(NEW.id,'result',NEW.updated_at);END;
CREATE TRIGGER IF NOT EXISTS source_photo_insert AFTER INSERT ON photo_assets
 BEGIN INSERT INTO result_events(job_id,kind,at) VALUES(NEW.job_id,'photo',NEW.updated_at);END;
CREATE TRIGGER IF NOT EXISTS source_photo_update AFTER UPDATE ON photo_assets
 WHEN OLD.sha256!=NEW.sha256 OR OLD.relative_path!=NEW.relative_path
 BEGIN INSERT INTO result_events(job_id,kind,at) VALUES(NEW.job_id,'photo',NEW.updated_at);END;
CREATE TABLE IF NOT EXISTS photo_recovery(job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
 state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
 retry_after REAL NOT NULL DEFAULT 0,detail TEXT,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS photo_recovery_queue ON photo_recovery(state,retry_after,job_id);
CREATE INDEX IF NOT EXISTS photo_recovery_ready ON photo_recovery(attempts,job_id)
 WHERE state IN ('pending','retry');
CREATE TRIGGER IF NOT EXISTS photo_recovery_result AFTER UPDATE OF state,result_json ON jobs
 WHEN NEW.state='verified' AND NOT EXISTS(SELECT 1 FROM photo_assets WHERE job_id=NEW.id)
 BEGIN INSERT OR IGNORE INTO photo_recovery(job_id,updated_at) VALUES(NEW.id,NEW.updated_at);END;
'''


def key(value):
    value=re.sub(r'[-\s]','',str(value or ''))
    if not re.fullmatch(r'[0-9]{13}',value):raise ValueError('Photo needs exact student B-Form identity')
    return value


def checked_path(root,relative):
    root=Path(root).absolute();relative=Path(relative)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:raise ValueError('Unsafe media path')
    path=root/relative
    if not path.resolve().is_relative_to(root.resolve()):raise ValueError('Media path leaves root')
    if any(p.is_symlink() for p in (root,path,*path.parents) if p==root or root in p.parents):
        raise ValueError('Media symlinks are forbidden')
    return path


def durable_asset(root,payload,metadata):
    digest=hashlib.sha256(payload).hexdigest()
    if digest!=metadata['sha256'] or not re.fullmatch(r'[a-f0-9]{64}',digest):raise ValueError('Photo digest differs')
    relative=Path('originals')/digest[:2]/digest[2:4]/(digest+'.'+metadata['extension'])
    path=checked_path(root,relative);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.exists():
        old_hash=hashlib.sha256(path.read_bytes()).hexdigest()
        if old_hash==digest:return relative.as_posix()
        # Preserve the damaged bytes, then restore this content address from the
        # freshly validated source payload. A crash here leaves a repairable
        # missing reference, never a DB reference to unflushed replacement bytes.
        quarantine=checked_path(root,Path('quarantine')/(digest+'-'+old_hash+'.corrupt'))
        quarantine.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if quarantine.exists():
            if hashlib.sha256(quarantine.read_bytes()).hexdigest()!=old_hash:raise ValueError('Quarantine digest differs')
            path.unlink()
        else:os.replace(path,quarantine)
        directory=os.open(quarantine.parent,os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    fd,name=tempfile.mkstemp(prefix='.photo-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),0o600);stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        # Persist newly-created hash-prefix directories as well as the file's
        # immediate directory before publishing any database reference.
        for parent in (path.parent,*path.parent.parents):
            if parent!=Path(root).absolute() and Path(root).absolute() not in parent.parents:break
            directory=os.open(parent,os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:Path(name).unlink(missing_ok=True)
    return relative.as_posix()


def save_file(db,job_id,parsed,payload,root):
    from .photos import inspect_photo
    from .store import now
    actual=inspect_photo(payload);metadata=parsed['student_photo']
    if any(metadata.get(k)!=v for k,v in actual.items()):raise ValueError('Photo metadata mismatch')
    row=db.execute('SELECT roll,year,course,exam_type FROM jobs WHERE id=?',(job_id,)).fetchone()
    if row is None or any(str(parsed.get(k))!=str(row[k]) for k in ('roll','year','course','exam_type')):
        raise ValueError('Photo/result source key mismatch')
    person=key(parsed.get('student_cnic'));root=Path(root).absolute()
    checked_path(root,'originals')
    relative=durable_asset(root,payload,actual)
    known=db.execute("SELECT path FROM media_roots WHERE name='originals'").fetchone()
    if known and Path(known[0]).resolve()!=root:raise ValueError('Media root differs; use explicit restore/remap')
    db.execute("INSERT OR IGNORE INTO media_roots VALUES('originals',?)",(str(root),))
    db.execute('''INSERT INTO photo_assets VALUES(?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(job_id) DO UPDATE SET root_name=excluded.root_name,relative_path=excluded.relative_path,
      sha256=excluded.sha256,mime_type=excluded.mime_type,bytes=excluded.bytes,width=excluded.width,
      height=excluded.height,source_url=excluded.source_url,applicant_key=excluded.applicant_key,updated_at=excluded.updated_at''',
      (job_id,'originals',relative,actual['sha256'],actual['mime_type'],actual['bytes'],actual['width'],actual['height'],metadata['source_url'],person,now()))
    metadata.update(storage='files',root_name='originals',relative_path=relative,applicant_key=person)
    parsed['student_photo_status']='available'
    return metadata


def read_asset(db,job_id,metadata,verify=True):
    row=db.execute('''SELECT a.*,r.path AS media_root,j.roll,j.year,j.course,j.exam_type,j.result_json
       FROM photo_assets a JOIN media_roots r ON r.name=a.root_name JOIN jobs j ON j.id=a.job_id
       WHERE a.job_id=?''',(job_id,)).fetchone()
    if row is None:raise ValueError('Original photo reference missing')
    item=dict(row);record=json.loads(item['result_json'] or '{}')
    if (item['sha256']!=metadata.get('sha256') or item['bytes']!=metadata.get('bytes')
        or key(record.get('student_cnic'))!=item['applicant_key']
        or any(str(record.get(k))!=str(item[k]) for k in ('roll','year','course','exam_type'))):
        raise ValueError('Photo identity/reference differs from saved result')
    path=checked_path(item['media_root'],item['relative_path'])
    if not path.is_file() or path.stat().st_size!=item['bytes']:raise ValueError('Original photo missing or truncated')
    if verify:
        payload=path.read_bytes()
        if hashlib.sha256(payload).hexdigest()!=item['sha256']:raise ValueError('Original photo hash mismatch')
        return payload
    return item


def reference(db,job):
    parsed=json.loads(job['result_json']);meta=parsed.get('student_photo',{})
    item=read_asset(db,job['id'],meta,verify=False)
    return {'kind':'original_file_reference','job_id':job['id'],'roll':job['roll'],
       'year':job['year'],'course':job['course'],'exam_type':job['exam_type'],
       'applicant_key':item['applicant_key'],'sha256':item['sha256'],'bytes':item['bytes'],
       'mime_type':item['mime_type'],'root_name':item['root_name'],'relative_path':item['relative_path']}
