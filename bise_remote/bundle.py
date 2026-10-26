"""Coherent database snapshots plus hash-verified external portrait manifests."""
from contextlib import closing
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import time
import uuid

from .common import atomic,digest,now,counts
from bise_results.media import checked_path


def create(root):
    root=Path(root).resolve();folder=root/'snapshots';folder.mkdir(exist_ok=True)
    with (folder/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        ident=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:8]
        target=folder/(ident+'.tar.gz');temporary=target.with_suffix('.tmp')
        required=sum(p.stat().st_size for p in [root/'results.sqlite3',root/'pipeline.sqlite3'] if p.exists())*3
        required+=sum(p.stat().st_size for p in (root/'photos').rglob('*') if p.is_file())+1024**3
        if shutil.disk_usage(root).free<required:raise ValueError('Insufficient backup headroom')
        with tempfile.TemporaryDirectory(prefix='.bundle-',dir=folder) as staging:
            staging=Path(staging);files={};refs={}
            # Pipeline first: the following source snapshot includes its committed events.
            for name in ('pipeline.sqlite3','results.sqlite3'):
                if not (root/name).exists():continue
                with closing(sqlite3.connect((root/name).as_uri()+'?mode=ro',uri=True,timeout=30)) as live,closing(sqlite3.connect(staging/name)) as copy:
                    live.backup(copy,pages=1024,sleep=.02)
                    if copy.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Backup integrity failed')
                    copy.execute('PRAGMA journal_mode=DELETE')
                files[name]=digest(staging/name)
            with closing(sqlite3.connect(staging/'results.sqlite3')) as db:
                for relative,sha,size in db.execute('SELECT relative_path,sha256,bytes FROM photo_assets'):refs[relative]=(sha,size)
            if (staging/'pipeline.sqlite3').exists():
                with closing(sqlite3.connect(staging/'pipeline.sqlite3')) as db:
                    for (payload,) in db.execute('SELECT payload FROM ready_outbox'):
                        ref=json.loads(payload).get('photo_reference')
                        if ref:refs[ref['relative_path']]=(ref['sha256'],ref['bytes'])
            manifest={'schema_version':1,'id':ident,'at':now(),'files':files,'media':{},'scope':{'year':2026,'course':'SSC','exam_type':'1'}}
            try:
                with tarfile.open(temporary,'w:gz',compresslevel=1) as archive:
                    for name in files:archive.add(staging/name,arcname=name,recursive=False)
                    for relative,(sha,size) in sorted(refs.items()):
                        path=checked_path(root/'photos',relative)
                        if path.stat().st_size!=size or digest(path)!=sha:raise ValueError('Backup photo missing/corrupt; source preserved')
                        name='photos/'+relative;manifest['media'][name]={'sha256':sha,'bytes':size}
                        archive.add(path,arcname=name,recursive=False)
                    encoded=json.dumps(manifest,sort_keys=True).encode();entry=tarfile.TarInfo('Manifest.json');entry.size=len(encoded);entry.mode=0o600
                    archive.addfile(entry,io.BytesIO(encoded))
                os.chmod(temporary,0o600);os.replace(temporary,target)
                metadata={'id':ident,'filename':target.name,'sha256':digest(target),'bytes':target.stat().st_size,
                  'created_at':now(),'pinned_until':time.time()+86400,'photos':len(refs),'counts':counts(root/'results.sqlite3'),'format':'file_photo_bundle'}
                atomic(folder/(ident+'.json'),metadata);return metadata
            finally:temporary.unlink(missing_ok=True)


def restore(path,output,metadata):
    path=Path(path);output=Path(output)
    if digest(path)!=metadata['sha256'] or path.stat().st_size!=metadata['bytes']:raise ValueError('Bundle transfer checksum differs')
    target=output/metadata['id']
    if target.exists():raise ValueError('Restore destination exists; preserve and inspect it')
    with tempfile.TemporaryDirectory(prefix='.restore-',dir=output) as staging:
        staging=Path(staging)
        with tarfile.open(path,'r:gz') as archive:
            members=archive.getmembers()
            names=[m.name for m in members]
            if len(names)!=len(set(names)) or any(not m.isfile() or Path(m.name).is_absolute() or '..' in Path(m.name).parts for m in members):raise ValueError('Unsafe bundle entries')
            archive.extractall(staging,filter='data')
        manifest=json.loads((staging/'Manifest.json').read_text())
        expected={*manifest['files'],*manifest['media'],'Manifest.json'}
        if set(names)!=expected or manifest['id']!=metadata['id']:raise ValueError('Bundle manifest mismatch')
        for name,sha in manifest['files'].items():
            if digest(staging/name)!=sha:raise ValueError('Restored DB checksum differs')
        for name,info in manifest['media'].items():
            if digest(staging/name)!=info['sha256'] or (staging/name).stat().st_size!=info['bytes']:raise ValueError('Restored portrait differs')
        for name in manifest['files']:
            with closing(sqlite3.connect(staging/name)) as db:
                if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Restored database integrity failed')
                if name=='results.sqlite3':
                    with db:db.execute("UPDATE media_roots SET path=? WHERE name='originals'",(str((target/'photos').resolve()),))
        os.replace(staging,target)
        # TemporaryDirectory cleanup tolerates the directory having been moved.
    return target
