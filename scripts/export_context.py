#!/usr/bin/env python3
"""Private, dependency-free development handoff; never uploads or submits forms."""
import argparse
import base64
from collections import Counter,defaultdict
from datetime import datetime,timezone
import hashlib
import json
import lzma
import os
from pathlib import Path,PurePosixPath
import platform
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]
DATABASES=('results.sqlite3','normalization.sqlite3','allocations.sqlite3','youthgames.sqlite3')
BULK={'data','artifacts','output','normalized_output','registration_output','analysis_output'}
IGNORED={'.venv','venv','__pycache__','.git','node_modules','.pytest_cache','.ruff_cache',
         'context_exports','.context_exports','.export-stage'}
PATH_KEYS={'path','csv','manifest','provenance','shared_file','source_database','allocation_database',
           'registration_database','catalog','normalization_base','normalization_profile',
           'registration_settings','registration_profile','photo'}
RELATED={
 'automation-platform':['scripts/install_dev_supervisor_service.py','tests/test_service_resource_policy.py',
                        'docs/local-service-reliability.md'],
 'whatsappbot':['worker.js','lib/session-lease.js','lib/connection-check.js',
                'scripts/check-connection.mjs','test/session-lease.test.mjs',
                'test/connection-check.test.mjs','README.md','package.json'],
}


def stamp():return datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S_UTC')
def now():return datetime.now(timezone.utc).isoformat()
def sha_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()
def log(phase,**values):print(json.dumps({'phase':phase,**values},ensure_ascii=False),flush=True)
def quoted(name):return '"'+name.replace('"','""')+'"'
def safe_member(name):
    p=PurePosixPath(name)
    return bool(name) and bool(p.parts) and not p.is_absolute() and '..' not in p.parts and '\\' not in name
def redact(text):
    text=re.sub(r'(\w+://)[^\s/@:]+:[^\s/@]+@',r'\1<redacted>@',text)
    text=re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}','Bearer <redacted>',text)
    text=re.sub(r'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b','<redacted>',text)
    text=re.sub(r'''(?im)((?:password|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)["']?\s*[:=]\s*)("[^"\n]*"|'[^'\n]*')''',r'\1"<redacted>"',text)
    return text


class Exporter:
    def __init__(self,root,destination,stage,*,data='full',chat='auto',chat_root=None,volume_mib=180,related=True):
        self.root=Path(root).resolve();self.destination=Path(destination).resolve();self.stage=Path(stage)
        self.data,self.chat=data,chat;self.chat_root=Path(chat_root or Path.home()/'.codex/sessions')
        self.volume_bytes=volume_mib*1024**2;self.related=related
        self.items={};self.gaps=[];self.notes=[];self.records=[];self.archives=[]
        self.databases={};self.skip_counts=Counter();self.skip_bytes=Counter();self.receipts_seen=set()
        self.chat_stats={};self.started=now();self.image_hashes=set()

    def gap(self,source,reason):
        value={'source':str(source),'reason':str(reason)}
        if value not in self.gaps:self.gaps.append(value)

    def add(self,path,category='code',*,archive_name=None,expected=None,required=True):
        path=Path(path)
        try:
            if path.is_symlink():raise ValueError('Symlink is not followed')
            if not path.is_file():raise ValueError('Referenced file is missing or not regular')
            path=path.resolve()
            if archive_name is None:
                if not path.is_relative_to(self.root):raise ValueError('Reference leaves the authorised project')
                archive_name='project/'+path.relative_to(self.root).as_posix()
            if not safe_member(archive_name):raise ValueError('Unsafe archive path')
            old=self.items.get(archive_name)
            if old:
                if expected and old.get('expected') not in (None,expected):self.gap(archive_name,'Conflicting historical evidence hashes')
                if expected:old['expected']=expected
                return
            self.items[archive_name]={'path':path,'category':category,'expected':expected,
                                      'original':str(path),'generated':False}
        except (OSError,ValueError) as error:
            if required:self.gap(archive_name or path,error)

    def generated(self,name,value,category='code'):
        if not safe_member(name):raise ValueError('Unsafe generated path')
        path=self.stage/name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if isinstance(value,bytes):path.write_bytes(value)
        else:path.write_text(value if isinstance(value,str) else json.dumps(value,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        self.add(path,category,archive_name=name)
        self.items[name]['generated']=True;self.items[name]['original']=None

    def collect_source(self):
        log('source_inventory')
        for folder,dirs,files in os.walk(self.root,followlinks=False):
            base=Path(folder);rel=base.relative_to(self.root)
            keep=[]
            for name in sorted(dirs):
                p=base/name
                if name in IGNORED or (not rel.parts and name in BULK) or p.is_symlink() or p==self.destination or p.is_relative_to(self.destination) or (p/'BUNDLE.json').is_file():
                    self.skip_counts['generated/cache/output directories']+=1
                else:keep.append(name)
            dirs[:]=keep
            for name in sorted(files):
                p=base/name
                if p.is_symlink():self.skip_counts['symlinks']+=1;continue
                if name.startswith('.env') and name not in ('.env.example','.env.template'):
                    try:
                        keys=[s.split('=',1)[0].strip() for s in p.read_text().splitlines() if '=' in s and not s.lstrip().startswith('#')]
                        self.generated('context/environment/'+p.relative_to(self.root).as_posix()+'.keys.json',{'variable_names':keys,'values':'omitted'})
                    except (OSError,UnicodeError):pass
                    self.skip_counts['secret environment values']+=1;continue
                if p.suffix.lower() in ('.zip','.gz','.7z','.pyc','.pem','.key','.p12','.pfx') or name.endswith(('-wal','-shm','.lock')):
                    self.skip_counts['archives/secrets/transient files']+=1;continue
                if p.is_file():self.add(p)
        if self.related:
            related_base=Path('/home/ahmad/code/deomeeautomations')
            for repo,names in RELATED.items():
                for name in names:
                    p=related_base/repo/name
                    if p.is_file():self.add(p,archive_name=f'related/{repo}/{name}')
            self.notes.append('related/ contains only the named cross-project changed source files, not complete exports of external repositories.')

    def snapshot_databases(self):
        if self.data=='none':
            self.generated('context/DATABASES.json',{'status':'intentionally_excluded'})
            return
        paths={p.name:p for p in (self.root/'data').glob('*') if p.is_file() and p.suffix in ('.sqlite3','.sqlite','.db')}
        for name in DATABASES:
            if name not in paths:self.gap('data/'+name,'Required application database is missing')
        for name,source in sorted(paths.items()):
            log('database_snapshot',database=name,mode=self.data)
            started=now();target=self.stage/'snapshots'/name;target.parent.mkdir(mode=0o700,exist_ok=True)
            try:
                if source.is_symlink() or not source.resolve().is_relative_to(self.root):
                    raise ValueError('Database symlinks or references outside the project are not followed')
                with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as live:
                    # Use backup(), not raw file/WAL copies or immutable=1.
                    # Each DB is coherent independently while writers continue.
                    if self.data=='full':
                        deadline=time.monotonic()+300
                        def progress(status,remaining,total):
                            if time.monotonic()>deadline:raise TimeoutError('Online backup exceeded five minutes; retry when writer is quieter')
                        with sqlite3.connect(target) as backup:live.backup(backup,pages=1024,progress=progress,sleep=.02)
                        db=sqlite3.connect(target)
                    else:
                        live.execute('BEGIN');db=live
                    try:
                        integrity=[r[0] for r in db.execute('PRAGMA quick_check')]
                        if integrity!=['ok']:raise ValueError('SQLite quick_check failed: '+str(integrity[:3]))
                        schema=[dict(zip(('type','name','table','sql'),r)) for r in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name')]
                        counts={r[0]:db.execute('SELECT count(*) FROM '+quoted(r[0])).fetchone()[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
                        summary={}
                        for table in counts:
                            columns={r[1] for r in db.execute('PRAGMA table_info('+quoted(table)+')')}
                            if 'state' in columns:
                                summary[table]=dict(db.execute('SELECT state,count(*) FROM '+quoted(table)+' GROUP BY state'))
                        self.databases[name]={'backup_started_at':started,'backup_finished_at':now(),
                            'method':'SQLite online backup API' if self.data=='full' else 'read transaction / schema only',
                            'original_bytes':source.stat().st_size,'integrity':integrity,'table_counts':counts,'state_counts':summary,'schema':schema}
                    finally:
                        if db is not live:db.close()
                if self.data=='full':
                    arc='project/data/'+name;self.add(target,'database',archive_name=arc)
                    self.items[arc]['original']=str(source);self.items[arc]['generated']=True
            except (OSError,sqlite3.Error,ValueError,TimeoutError) as error:self.gap('data/'+name,error)
        self.generated('context/DATABASES.json',self.databases)
        self.notes.append('Full SQLite rows, indexes, triggers and history are preserved. Snapshots are coherent per database, not one cross-database transaction. Originals/WAL are never rewritten.')

    def follow_receipt(self,path,expected=None):
        path=Path(path)
        if not path.is_absolute():path=self.root/path
        name=str(path)
        self.add(path,'evidence',expected=expected)
        if not path.resolve().is_relative_to(self.root):return
        if name in self.receipts_seen:return
        self.receipts_seen.add(name)
        if path.name.endswith('receipt.json'):
            try:
                value=json.loads(path.read_text())
                for entry in value.get('files',{}).values():
                    self.follow_receipt(entry['path'],entry.get('sha256'))
            except (OSError,ValueError,KeyError,TypeError) as error:self.gap(path,'Receipt closure: '+str(error))

    def add_tree(self,path,category='evidence',required=True):
        path=Path(path)
        if not path.is_absolute():path=self.root/path
        if path.is_file():self.add(path,category,required=required);return
        if not path.is_dir() or path.is_symlink():
            if required:self.gap(path,'Referenced directory is missing or a symlink')
            return
        if not path.resolve().is_relative_to(self.root):
            if required:self.gap(path,'Directory reference leaves the authorised project')
            return
        for base,dirs,files in os.walk(path,followlinks=False):
            dirs[:]=[d for d in dirs if d not in IGNORED and not (Path(base)/d).is_symlink()]
            for name in sorted(files):
                if not name.endswith(('.lock','-wal','-shm','.tmp')):self.add(Path(base)/name,category,required=required)

    def collect_references(self):
        if self.data!='full':return
        log('referenced_inputs_and_receipts')
        for name in ('youthgames_catalog.json',):self.add(self.root/'data'/name,'evidence')
        for item in list(self.items.values()):
            path=item['path']
            if path.suffix!='.json' or path.parent.name not in ('registration_profiles','normalization_profiles','allocation_profiles'):continue
            try:value=json.loads(path.read_text())
            except (OSError,ValueError):continue
            def visit(value,trail=()):
                if isinstance(value,dict):
                    for key,text in value.items():
                        # Normalization rules use provenance as a descriptive
                        # label, not a filename. Only registration.input has
                        # the actual provenance-file contract.
                        is_path=key in PATH_KEYS and (key!='provenance' or
                            (path.parent.name=='registration_profiles' and trail==('input',)))
                        if is_path and isinstance(text,str) and text and not re.match(r'^https?://',text):
                            ref=(path.parent/text).resolve()
                            if ref.suffix in ('.sqlite3','.sqlite','.db'):continue
                            if ref.is_file():self.add(ref,'evidence')
                            elif key in ('csv','manifest','provenance','shared_file','path'):self.gap(ref,'Configured input missing before export')
                        elif isinstance(text,(dict,list)):visit(text,trail+(key,))
                elif isinstance(value,list):
                    for child in value:visit(child,trail)
            visit(value)
        for item in list(self.items.values()):
            path=item['path']
            if path.name=='manifest.json' and not item['generated']:
                try:
                    value=json.loads(path.read_text());base=path.parent.parent if path.parent.name=='Data' else path.parent
                    for rel,entry in value.get('files',{}).items():
                        if isinstance(rel,str) and safe_member(rel) and isinstance(entry,str):self.add(base/rel,'evidence',expected=entry)
                except (OSError,ValueError,AttributeError):pass
        registration=self.stage/'snapshots/youthgames.sqlite3'
        if registration.exists():
            with sqlite3.connect(registration) as db:
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table,column in (('attempts','evidence'),('jobs','receipt'),('registered_applicants','receipt')):
                    if table not in tables or column not in {r[1] for r in db.execute('PRAGMA table_info('+quoted(table)+')')}:continue
                    for row in db.execute('SELECT DISTINCT '+quoted(column)+' FROM '+quoted(table)+' WHERE '+quoted(column)+' IS NOT NULL'):
                        if row[0]:self.follow_receipt(row[0])
                if 'batches' in tables:
                    for row in db.execute('SELECT DISTINCT snapshot FROM batches WHERE snapshot IS NOT NULL'):
                        if row[0]:self.add_tree(row[0])
        # Preserve unresolved collection evidence, plus available old captures.
        # DB-retained verified results do not require deleted historical images.
        self.add_tree(self.root/'artifacts/results',required=False)
        result_db=self.stage/'snapshots/results.sqlite3'
        if result_db.exists():
            with sqlite3.connect(result_db) as db:
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'jobs' in tables and 'receipt_json' in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                    for row in db.execute('SELECT receipt_json FROM jobs WHERE receipt_json IS NOT NULL'):
                        try:
                            for entry in json.loads(row[0]).values():
                                if Path(entry['path']).is_file():self.add(entry['path'],'evidence',expected=entry.get('sha256'))
                        except (ValueError,KeyError,TypeError):pass

    def collect_chat(self):
        if self.chat=='none':self.chat_stats={'status':'intentionally_excluded'};return
        log('readable_project_chat')
        sessions=[]
        for path in sorted(self.chat_root.rglob('*.jsonl')):
            try:
                with path.open() as stream:meta=json.loads(stream.readline())
                if meta.get('type')=='session_meta' and Path(meta['payload'].get('cwd','/')).resolve()==self.root:sessions.append(path)
            except (OSError,ValueError,KeyError):continue
        if not sessions:
            self.gap('local chat','No project-matching local Codex session found; browser ChatGPT history cannot be fetched by this offline exporter')
            return
        messages=[];seen=set();images=0;attachments=set();last=None
        for path in sessions:
            limit=path.stat().st_size
            with path.open('rb') as stream:
                while stream.tell()<limit:
                    line=stream.readline(min(limit-stream.tell(),64*1024**2))
                    try:r=json.loads(line)
                    except ValueError:continue
                    p=r.get('payload',{})
                    if r.get('type')!='response_item' or p.get('type')!='message' or p.get('role') not in ('user','assistant'):continue
                    if p.get('channel') not in (None,'final','commentary'):continue
                    key=hashlib.sha256(line).hexdigest()
                    if key in seen:continue
                    seen.add(key);parts=[]
                    for content in p.get('content',[]):
                        kind=content.get('type')
                        if kind in ('input_text','output_text','text'):
                            text=content.get('text','');parts.append(redact(text))
                            for match in re.findall(r'(/home/ahmad/\.codex/attachments/[^\n"<>]+)',text):
                                candidate=Path(match.strip().rstrip('`'))
                                if candidate.is_file():attachments.add(candidate)
                        elif kind in ('input_image','image'):
                            url=content.get('image_url',content.get('url',''))
                            if isinstance(url,dict):url=url.get('url','')
                            match=re.fullmatch(r'data:image/(png|jpeg|jpg|webp);base64,(.*)',url,re.DOTALL) if isinstance(url,str) else None
                            if match:
                                try:
                                    raw=base64.b64decode(match[2],validate=True);h=hashlib.sha256(raw).hexdigest()
                                    name=f'context/chat/images/{h[:20]}.{match[1]}'
                                    if h not in self.image_hashes:self.generated(name,raw);self.image_hashes.add(h);images+=1
                                    parts.append(f'![Attached image](images/{Path(name).name})')
                                except ValueError:self.gap('chat image','Invalid local image encoding')
                            else:
                                parts.append('[Image reference unavailable in local text export]')
                                self.gap('chat image','Only a remote/unavailable image reference exists in the local session')
                    if parts:
                        when=r.get('timestamp','');last=max(last or when,when)
                        messages.append((when,p['role'],p.get('channel'), '\n\n'.join(parts)))
        attachment_sources=[]
        for number,path in enumerate(sorted(attachments)):
            try:
                raw=path.read_text(encoding='utf-8');clean=redact(raw)
                name=f'context/chat/attachments/{number:03d}-{path.name}'
                self.generated(name,clean)
                attachment_sources.append({'original_path':str(path),'bundle_path':name,
                    'original_sha256':sha_file(path),'secret_redaction_applied':clean!=raw})
            except (OSError,UnicodeError) as error:self.gap(path,'Chat attachment: '+str(error))
        lines=['# Readable locally available project chat','',
               'User/assistant visible messages only. System/developer prompts, private reasoning, raw tool payloads and reusable secret strings are excluded. Local session segments are merged chronologically; this does not fetch unavailable browser-only chats.','']
        for when,role,channel,text in sorted(messages,key=lambda m:m[0]):
            lines.extend([f'## {when} — {role}'+(f' ({channel})' if channel else ''),'',text,''])
        self.generated('context/chat/CHAT.md','\n'.join(lines))
        self.chat_stats={'status':'captured_local_visible_messages','sessions':len(sessions),'messages':len(messages),
            'images':images,'attachments':len(attachments),'captured_through':last,'attachment_sources':attachment_sources,
            'session_files':[{'name':p.name,'bytes_at_discovery':p.stat().st_size} for p in sessions]}
        self.generated('context/chat/PROVENANCE.json',self.chat_stats)

    def collect_diagnostics(self):
        # Small existing verification records are useful context. Bulk caches,
        # raw captures and unbounded service/tool logs remain outside this part.
        for folder in ('coexistence','mobile-health','usb-stable-identity'):
            for path in sorted((self.root/'artifacts'/folder).glob('*')):
                if path.is_file() and path.suffix in ('.json','.log') and path.stat().st_size<=1024**2:
                    self.generated('context/diagnostics/'+folder+'/'+path.name,redact(path.read_text(errors='replace')))
        for output in (self.root/'registration_output',self.root/'normalized_output/registration_output'):
            for path in sorted(output.glob('*/Runs/*/Summary.json')):
                if path.stat().st_size<=1024**2:
                    self.generated('context/diagnostics/run-summaries/'+path.relative_to(self.root).as_posix(),redact(path.read_text(errors='replace')))

    def context_documents(self):
        info={'created_at':self.started,'project_root':str(self.root),'python':sys.version,'platform':platform.platform(),
              'cpu_count':os.cpu_count(),'data_mode':self.data,'chat':self.chat_stats,'database_count':len(self.databases),
              'original_paths':'Preserved inside database records; project/ keeps the original relative layout.',
              'no_automatic_upload':True,'source_database_modified':False}
        if not (self.root/'.git').exists():
            info['git']={'status':'not_present','detail':'This checkout has no .git history to collect. No repository was initialised.'}
        else:
            self.gap('Git history','Git metadata exists; this exporter captures the working source but does not yet package Git object history')
        self.generated('context/ENVIRONMENT.json',info)
        tree='\n'.join(sorted(name for name in self.items if name.startswith(('project/','related/')) and self.items[name]['category']=='code'))
        self.generated('context/SOURCE_TREE.txt',tree+'\n')
        self.generated('START_HERE.md',f'''# BISE Lahore development handoff

Created {self.started}. Read `project/docs/PROJECT_HANDOFF.md`, then
`project/README.md`, `context/DATABASES.json` and `context/chat/CHAT.md`.

Upload `Code-and-Context.zip` first for code review and planning. Its source,
tests, profiles, schemas, readable chat and project map are self-contained for
development review. Full private data parts are separate: add the relevant
`Data-*.zip` and `Evidence-*.zip` when actual row/state analysis is required.
All ZIPs extract into the same folder layout. There is no automatic upload.

Evidence ZIPs contain a `.tar.xz` container (named in the manifest): solid compression shares the repeated
portal HTML/layout across receipts without removing any original bytes.
After extracting the ZIPs, use Python tarfile to unpack each evidence container
with `filter='data'` into the same directory. Code and database ZIPs are ordinary
independently readable ZIPs. The manifest records each logical evidence path
and its outer ZIP/container. Do not flatten receipt paths.

The data parts contain original student records/identifiers and operational
history, not synthetic samples. The operator explicitly requested local copies.
`MANIFEST.json` indexes every captured file, its SHA-256 and archive. `BUNDLE.json`
and `SHA256SUMS.txt` outside the ZIPs verify complete archive files.

Large generated caches, historical duplicate backups, old output copies,
venv/toolchains, bytecode, raw WAL/SHM/lock files and prior exports are excluded.
Referenced registration receipts/proofs, frozen batch inputs and existing
unresolved result evidence are collected in full-data mode. SQLite backup API
preserves committed WAL data while writers continue. Snapshots are coherent
per DB, not a cross-database atomic transaction. Originals are never changed.

This is a local development handoff, not permission to submit forms. Absolute
receipt/source paths inside DBs retain their original provenance. Consult the
manifest path map before restoring on another host. Do not clear confirmation
ledgers, fabricate receipts or retry ambiguous external sends automatically.

`CAPTURE_REPORT.json` states all gaps and intentional exclusions. COMPLETE means
the selected existing local scope was captured; it does not imply cloud data,
browser-only chats, external repositories or Git history that never existed.

Recheck archive integrity locally:

    python3 project/scripts/export_context.py --verify /path/to/export-directory

Suggested first prompt: Read START_HERE and PROJECT_HANDOFF, inspect the source
and schemas, explain the current flow and gaps, then propose a reviewable patch
with focused offline regressions. Do not make live registrations or send messages.
''')

    def freeze_code(self):
        # Stable source/config/chat copies keep an editor or a live log append
        # from producing a misleading COMPLETE report during ZIP creation.
        for name,item in list(self.items.items()):
            if item['category']!='code' or item['generated']:continue
            source=item['path'];target=self.stage/'source-freeze'/name
            try:
                before=source.stat();target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target);after=source.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):self.gap(name,'Source changed while being frozen')
                item['path']=target
            except OSError as error:
                self.gap(name,error);del self.items[name]

    def write_zip(self,name,items):
        target=self.destination/name;count=0
        method=zipfile.ZIP_DEFLATED
        log('zip_archive',archive=name,files=len(items))
        with zipfile.ZipFile(target,'w',compression=method,compresslevel=6,allowZip64=True) as archive:
            for arc,item in items:
                try:
                    path=item['path'];before=path.stat()
                    info=zipfile.ZipInfo(arc);info.compress_type=method
                    info.external_attr=(stat.S_IFREG|(0o700 if before.st_mode&0o111 else 0o600))<<16
                    h=hashlib.sha256();size=0
                    with path.open('rb') as source,archive.open(info,'w',force_zip64=True) as output:
                        for chunk in iter(lambda:source.read(1024*1024),b''):
                            output.write(chunk);h.update(chunk);size+=len(chunk)
                            if item['category']=='database' and size%(128*1024**2)==0:
                                log('database_compression',archive=name,read_mib=size//1024**2)
                    after=path.stat();digest=h.hexdigest()
                    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):self.gap(arc,'Source changed while being packed; rerun for a stable capture')
                    if item.get('expected') and item['expected']!=digest:self.gap(arc,'Original evidence does not match its referenced SHA-256')
                    self.records.append({'path':arc,'archive':name,'bytes':size,'sha256':digest,
                        'category':item['category'],'original_path':item['original'],'generated':item['generated']})
                    count+=1
                    if count%2000==0:log('packing',archive=name,files=count,compressed_mib=round(target.stat().st_size/1024**2,1))
                except (OSError,ValueError) as error:self.gap(arc,error)
        os.chmod(target,0o600)
        self.archives.append({'name':name,'bytes':target.stat().st_size,'sha256':sha_file(target),'files':count})

    def write_evidence(self):
        items=iter(sorted((a,i) for a,i in self.items.items() if i['category']=='evidence'))
        pending=next(items,None);part=1
        class Reader:
            def __init__(self,stream):self.stream=stream;self.hash=hashlib.sha256();self.size=0
            def read(self,n=-1):
                data=self.stream.read(n);self.hash.update(data);self.size+=len(data);return data
        while pending:
            name=f'Evidence-{part:03d}.zip';container_name=f'Evidence-{part:03d}.tar.xz'
            container=self.stage/container_name;count=0
            log('solid_evidence',archive=name)
            with lzma.open(container,'wb',preset=3) as compressed:
                with tarfile.open(fileobj=compressed,mode='w|',format=tarfile.PAX_FORMAT,copybufsize=1024**2) as tar:
                    while pending:
                        arc,item=pending;path=item['path']
                        before=path.stat();info=tarfile.TarInfo(arc);info.size=before.st_size
                        info.mode=0o600;info.mtime=0;info.uid=info.gid=0
                        with path.open('rb') as source:
                            reader=Reader(source);tar.addfile(info,reader)
                        after=path.stat();digest=reader.hash.hexdigest()
                        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):self.gap(arc,'Evidence changed while packed')
                        if item.get('expected') and digest!=item['expected']:self.gap(arc,'Original evidence does not match its referenced SHA-256')
                        self.records.append({'path':arc,'archive':name,'container':container_name,
                            'bytes':reader.size,'sha256':digest,'category':'evidence',
                            'original_path':item['original'],'generated':False})
                        count+=1;pending=next(items,None)
                        if count%2000==0:log('packing',archive=name,files=count,compressed_mib=round(container.stat().st_size/1024**2,1))
                        if container.stat().st_size>=self.volume_bytes:break
            target=self.destination/name
            with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:archive.write(container,container_name)
            os.chmod(target,0o600);container.unlink()
            self.archives.append({'name':name,'bytes':target.stat().st_size,'sha256':sha_file(target),
                                  'files':count,'container':container_name,'compression':'solid xz / ZIP stored wrapper'})
            part+=1

    def pack(self):
        # Independent DB ZIPs, and byte-preserving solid-compressed proofs.
        for arc,item in sorted(self.items.items()):
            if item['category']=='database':self.write_zip('Data-'+Path(arc).stem+'.zip',[(arc,item)])
        self.write_evidence()
        report={'status':'INCOMPLETE' if self.gaps else 'COMPLETE','started_at':self.started,'finished_at':now(),
            'selected_scope':{'data':self.data,'chat':self.chat,'related_changed_files':self.related},
            'gaps':self.gaps,'notes':self.notes,'excluded_classes':dict(self.skip_counts),
            'chat':self.chat_stats,'databases':{k:{a:b for a,b in v.items() if a!='schema'} for k,v in self.databases.items()},
            'automatic_upload':False,'source_database_modified':False}
        self.generated('CAPTURE_REPORT.json',report)
        # Source records are added as they are written, then the final manifest
        # is appended into that ZIP. It intentionally does not hash itself.
        self.write_zip('Code-and-Context.zip',[(a,i) for a,i in sorted(self.items.items()) if i['category']=='code'])
        manifest={'schema_version':1,'status':report['status'],'project_root':str(self.root),'files':self.records}
        target=self.destination/'Code-and-Context.zip'
        with zipfile.ZipFile(target,'a',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            archive.writestr('MANIFEST.json',json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
        self.archives[-1].update(bytes=target.stat().st_size,sha256=sha_file(target))
        for filename,value in (('BUNDLE.json',{'schema_version':1,'status':report['status'],'archives':self.archives}),('CAPTURE_REPORT.json',report)):
            path=self.destination/filename;path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n');os.chmod(path,0o600)
        (self.destination/'SHA256SUMS.txt').write_text(''.join(f"{a['sha256']}  {a['name']}\n" for a in self.archives))
        (self.destination/'START_HERE.md').write_text((self.stage/'START_HERE.md').read_text())
        return report


def verify(directory):
    directory=Path(directory);bundle=json.loads((directory/'BUNDLE.json').read_text())
    archives={};checked=0
    try:
        for info in bundle['archives']:
            if not safe_member(info['name']) or len(PurePosixPath(info['name']).parts)!=1:raise ValueError('Unsafe archive filename')
            path=directory/info['name']
            if sha_file(path)!=info['sha256']:raise ValueError('Archive checksum mismatch: '+info['name'])
            archive=zipfile.ZipFile(path);archives[info['name']]=archive
            names=archive.namelist()
            if len(set(names))!=len(names) or any(not safe_member(n) for n in names):raise ValueError('Duplicate/unsafe ZIP member')
        manifest=json.loads(archives['Code-and-Context.zip'].read('MANIFEST.json'))
        expected=set();actual=set();by_archive=defaultdict(dict)
        for info in manifest['files']:
            if not safe_member(info['path']):raise ValueError('Unsafe manifest member')
            key=(info['archive'],info['path'])
            if key in expected:raise ValueError('Duplicate manifest member')
            expected.add(key);by_archive[info['archive']][info['path']]=info
        def check(stream,info):
            h=hashlib.sha256();size=0
            with stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk);size+=len(chunk)
            if size!=info['bytes'] or h.hexdigest()!=info['sha256']:raise ValueError('File checksum mismatch: '+info['path'])
        for entry in bundle['archives']:
            name=entry['name'];archive=archives[name]
            log('verify_file_contents',archive=name,files=len(by_archive[name]))
            if entry.get('container'):
                if archive.namelist()!=[entry['container']]:raise ValueError('Unexpected evidence ZIP member')
                with archive.open(entry['container']) as source,tarfile.open(fileobj=source,mode='r|xz') as tar:
                    for member in tar:
                        key=(name,member.name)
                        if not member.isfile() or not safe_member(member.name) or key in actual:raise ValueError('Unsafe or duplicate evidence member')
                        if key not in expected:raise ValueError('Unmanifested evidence member')
                        actual.add(key);check(tar.extractfile(member),by_archive[name][member.name]);checked+=1
            else:
                for path in archive.namelist():
                    if path=='MANIFEST.json':continue
                    key=(name,path)
                    if key not in expected:raise ValueError('Unmanifested ZIP member')
                    actual.add(key);check(archive.open(path),by_archive[name][path]);checked+=1
        if actual!=expected:raise ValueError('Manifest does not cover every archived file')
        return {'status':'verified','capture_status':bundle['status'],'archives':len(archives),'files':checked}
    finally:
        for archive in archives.values():archive.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--output',type=Path,help='Parent directory for dated export; default PROJECT/context_exports')
    parser.add_argument('--data',choices=('full','schema','none'),default='full')
    parser.add_argument('--chat',choices=('auto','none'),default='auto')
    parser.add_argument('--chat-root',type=Path,help='Optional local Codex sessions directory')
    parser.add_argument('--volume-mib',type=int,default=180,help='Evidence volume target by compressed bytes (default 180 MiB, entries remain intact)')
    parser.add_argument('--no-related',action='store_true',help='Exclude the small related-project changed-file appendix')
    parser.add_argument('--verify',type=Path,help='Verify an existing export instead of creating one')
    args=parser.parse_args(argv)
    if args.verify:
        try:log('verification',**verify(args.verify));return 0
        except (OSError,ValueError,KeyError,zipfile.BadZipFile) as error:log('verification_failed',error=str(error));return 1
    if args.volume_mib<1:parser.error('--volume-mib must be positive')
    root=args.root.resolve()
    if not root.is_dir():parser.error('Project root does not exist')
    os.umask(0o077)
    destination=(args.output or root/'context_exports').resolve()/stamp()
    destination.mkdir(parents=True,exist_ok=False,mode=0o700)
    try:
        # Lower only this exporter process's priority, not other applications.
        if hasattr(os,'nice'):os.nice(5)
        with tempfile.TemporaryDirectory(prefix='.export-stage-',dir=destination) as stage:
            exporter=Exporter(root,destination,stage,data=args.data,chat=args.chat,chat_root=args.chat_root,
                volume_mib=args.volume_mib,related=not args.no_related)
            exporter.collect_source();exporter.snapshot_databases();exporter.collect_references()
            exporter.collect_chat();exporter.collect_diagnostics();exporter.freeze_code();exporter.context_documents();report=exporter.pack()
        log('verifying_archive_checksums',directory=str(destination))
        verification=verify(destination)
        log('finished',status=report['status'],directory=str(destination),
            archives=[{'name':a['name'],'mib':round(a['bytes']/1024**2,2)} for a in exporter.archives],
            files=verification['files'],gaps=len(report['gaps']))
        return 0 if report['status']=='COMPLETE' else 2
    except (OSError,ValueError,sqlite3.Error,zipfile.BadZipFile,KeyboardInterrupt) as error:
        (destination/'EXPORT_FAILED.json').write_text(json.dumps({'status':'INCOMPLETE','reason':type(error).__name__+': '+str(error)})+'\n')
        log('failed',directory=str(destination),error=type(error).__name__+': '+str(error));return 130 if isinstance(error,KeyboardInterrupt) else 1


if __name__=='__main__':raise SystemExit(main())
