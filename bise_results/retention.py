"""Explicit DB-only retention and scoped deletion of redundant BISE artifacts."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

from .store import locked_store, retain_in_database


def discard_verified_capture(db, job_id, directory):
    directory=Path(directory)
    if directory.is_symlink() or directory.name != f'job-{job_id}':
        raise ValueError('Capture cleanup requires the exact non-symlink job directory')
    row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if row is None:raise ValueError('Unknown capture job')
    retain_in_database(db,row,'DB-only collection; verified original capture discarded after commit')
    if directory.exists():shutil.rmtree(directory)


def jobs_digest(db):
    digest=hashlib.sha256()
    for row in db.execute('SELECT * FROM jobs ORDER BY id'):
        digest.update(json.dumps(tuple(row),ensure_ascii=False,separators=(',',':')).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def report_roots(root):
    output=root/'output'
    if output.is_dir():
        for path in output.iterdir():
            if path.is_dir() and not path.is_symlink():yield path
    archive=root/'artifacts/report-archive'
    if archive.is_dir():
        yield from (path for path in archive.glob('*/*/*/Files') if path.is_dir() and not path.is_symlink())


def validate_report(path, database, keys, batch_ids):
    manifest=next((p for p in (path/'Data/manifest.json',path/'manifest.json') if p.is_file()),None)
    data=next((p for p in (path/'Data/results.json',path/'results.json') if p.is_file()),None)
    if manifest is None or data is None:return False,'Unmanaged report or structured result index unavailable'
    try:
        info=json.loads(manifest.read_text());rows=json.loads(data.read_text())
        if info.get('database') and Path(info['database']).resolve()!=database:
            return False,'Report belongs to a different database'
        if info.get('batch') not in batch_ids:return False,'Unknown source batch'
        if not isinstance(rows,list):return False,'Unexpected report data format'
        for item in rows:
            if item.get('state')!='verified':continue
            key=(str(item.get('roll')),str(item.get('year',info.get('year'))),
                 item.get('course',info.get('course','SSC')),str(item.get('exam_type',info.get('exam_type','2'))))
            if key not in keys:return False,'A previously exported result is not verified in the current DB'
        return True,None
    except (ValueError,TypeError,KeyError,OSError):
        return False,'Report index cannot be verified'


def delete_tree(path, root, journal, summary):
    """Persist each file deletion intent before unlinking; never follow symlinks."""
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('Cleanup target escaped the project or is a directory symlink')
    for parent,dirs,files in os.walk(path,topdown=False,followlinks=False):
        for name in files:
            file=Path(parent)/name
            st=file.lstat()
            journal.write(json.dumps({'path':str(file.relative_to(root)),'bytes':st.st_size,
                'device':st.st_dev,'inode':st.st_ino,'links':st.st_nlink,'mtime_ns':st.st_mtime_ns})+'\n')
            journal.flush()
            file.unlink()
            summary['deleted_files']+=1;summary['logical_bytes_deleted']+=st.st_size
            if st.st_nlink==1:summary['allocated_bytes_unlinked']+=st.st_blocks*512
        for name in dirs:
            sub=Path(parent)/name
            if sub.is_symlink():
                journal.write(json.dumps({'path':str(sub.relative_to(root)),'symlink':True})+'\n');journal.flush();sub.unlink()
            else:sub.rmdir()
    path.rmdir()


def cleanup(root, database, apply=False, progress=None):
    root=Path(root).resolve();database=Path(database).resolve()
    progress=progress or (lambda message:None)
    if not database.is_relative_to(root):raise ValueError('Cleanup database must be within the project root')
    if not database.is_file():raise ValueError('Existing results database is required')
    capture_root=root/'artifacts/results'
    if capture_root.is_symlink():raise ValueError('Capture root must not be a symlink')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    audit=root/'artifacts/cleanup'/stamp
    audit.mkdir(parents=True,exist_ok=False)
    summary={'status':'planning','applied':apply,'audit':str(audit),'database':str(database),
        'deleted_files':0,'logical_bytes_deleted':0,'allocated_bytes_unlinked':0,'preserved':[]}
    with locked_store(database) as db:
        check=[r[0] for r in db.execute('PRAGMA quick_check')]
        if check!=['ok'] or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Database integrity check failed; no files deleted')
        if db.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone():
            raise ValueError('Interrupted running claims exist; reconcile them before cleanup')
        summary['states']=[dict(r) for r in db.execute('SELECT year,exam_type,state,count(*) AS records FROM jobs GROUP BY year,exam_type,state')]
        before=jobs_digest(db);summary['jobs_sha256_before']=before
        keys=set();eligible=[]
        directories={}
        if capture_root.is_dir():
            with os.scandir(capture_root) as entries:
                for entry in entries:
                    match=re.fullmatch(r'job-([0-9]+)',entry.name)
                    if match and entry.is_dir(follow_symlinks=False):directories[int(match[1])]=Path(entry.path)
        progress('Cleanup: checking verified DB identities and recording retention metadata...')
        # One transaction for the entire retention plan. Files are removed only
        # after all hashes and deletion authorization have committed durably.
        with db:
            for row in db.execute("SELECT * FROM jobs WHERE state='verified'"):
                try:
                    item=json.loads(row['result_json'])
                    from .demographics import explicitly_estimated_hssc
                    from .store import photo_retained
                    if (str(item.get('roll'))!=row['roll'] or str(item.get('year'))!=str(row['year'])
                            or str(item.get('exam_type','2'))!=row['exam_type']
                            or item.get('course','SSC')!=row['course']
                            or not all(item.get(k) for k in ('name','father_name','student_cnic','father_cnic'))
                            or not (item.get('date_of_birth') or explicitly_estimated_hssc(item))
                            or not photo_retained(db,row['id'],item,verify=True)):
                        raise ValueError('Incomplete/mismatched database identity')
                    keys.add((row['roll'],str(row['year']),row['course'],row['exam_type']))
                    directory=directories.get(row['id'])
                    if directory is not None:
                        receipt=json.loads(row['receipt_json'] or '{}')
                        # Saved receipt paths are absolute. Lexical containment
                        # avoids millions of resolve/stat calls on large batches;
                        # deletion independently refuses root symlinks and never
                        # follows nested symlinks.
                        if not all(Path(receipt[name]['path']).is_absolute()
                                   and '..' not in Path(receipt[name]['path']).parts
                                   and Path(receipt[name]['path']).is_relative_to(directory)
                                   for name in ('card.html','card.png','parsed.json')):
                            raise ValueError('Capture directory does not belong to this saved receipt')
                        if apply:retain_in_database(db,row,'Authorized verified-capture cleanup',audit/'Deleted files.jsonl',commit=False)
                        eligible.append(directory)
                except (ValueError,TypeError,KeyError):
                    summary['preserved'].append({'job':row['id'],'reason':'Verified DB identity requires review; captures preserved'})
        summary['verified_capture_directories']=len(eligible)
        batches={r[0] for r in db.execute('SELECT id FROM batches')}
        reports=[]
        for path in report_roots(root):
            valid,reason=validate_report(path,database,keys,batches)
            if valid:reports.append(path)
            else:summary['preserved'].append({'path':str(path),'reason':reason})
        summary['redundant_report_directories']=len(reports)
        if apply and (eligible or reports):
            backup=database.parent/'backups'/f'results-before-cleanup-{stamp}.sqlite3'
            backup.parent.mkdir(exist_ok=True)
            progress('Cleanup: making a coherent database backup before deleting files...')
            copy=sqlite3.connect(backup)
            try:
                db.backup(copy)
                if jobs_digest(copy)!=before:raise ValueError('Backup content verification failed')
            finally:
                copy.close()
            summary['backup']=str(backup)
        plan=audit/'Plan.json'
        plan.write_text(json.dumps({**summary,'capture_roots':[str(p) for p in eligible],
            'report_roots':[str(p) for p in reports]},indent=2)+'\n')
        if apply:
            try:
                with (audit/'Deleted files.jsonl').open('w',buffering=1024*1024) as journal:
                    targets=reports+eligible
                    for index,path in enumerate(targets,1):
                        delete_tree(path,root,journal,summary)
                        if index==1 or index%5000==0 or index==len(targets):
                            progress(f"Cleanup: {index:,}/{len(targets):,} directories; {summary['deleted_files']:,} files deleted; {summary['allocated_bytes_unlinked']/1024**3:.1f} GiB unlinked")
            finally:
                summary['jobs_sha256_after']=jobs_digest(db)
                summary['jobs_unchanged']=summary['jobs_sha256_after']==before
                summary['integrity_check']=[r[0] for r in db.execute('PRAGMA quick_check')]
                (audit/'Summary.json').write_text(json.dumps(summary,indent=2)+'\n')
            if not summary['jobs_unchanged'] or summary['integrity_check']!=['ok']:
                raise ValueError('Post-cleanup database verification failed; inspect backup/audit')
        summary['status']='complete' if apply else 'dry_run'
        (audit/'Summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary
