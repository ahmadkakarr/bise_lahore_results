"""Audited participation changes for unposted records in a frozen allocation."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from . import app
from .store import opened as allocation_store,now
from youthgames_register.config import load as registration_config,resolved,input_snapshot,signature


def current(database,scheme):
    if not Path(database).is_file():return None
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='participation_changes'").fetchone():return None
        row=db.execute('SELECT policy FROM participation_changes WHERE scheme=? ORDER BY id DESC LIMIT 1',(scheme,)).fetchone()
        return json.loads(row[0]) if row else None


def replace_profile(path,value,expected):
    """An explicit audited change may replace exactly its reviewed preimage."""
    path=Path(path)
    if json.loads(path.read_text())!=expected:
        raise ValueError('Profile changed during participation update; preserve and inspect operator edits')
    if expected==value:return
    fd,name=tempfile.mkstemp(prefix='.participation-',suffix='.json',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            stream.write(json.dumps(value,indent=2,ensure_ascii=False)+'\n');stream.flush();os.fsync(stream.fileno())
        Path(name).replace(path)
    finally:Path(name).unlink(missing_ok=True)


def fingerprint(db):
    return signature([tuple(r) for r in db.execute('''SELECT id,state,payload_hash,tracking,receipt,updated_at FROM jobs
        WHERE state IN ('registered','already_registered_external') ORDER BY id''')])


def apply(profile,subcategory,client=None):
    if subcategory not in ('Open Category','Private'):raise ValueError('Use Open Category or Private')
    c=app.load(profile);p=app.paths(c,profile)
    if not c.get('source_pool'):raise ValueError('Participation change requires an existing frozen named-school allocation')
    from .private_pool import catalog
    cached=catalog(p['catalog'])
    if subcategory not in cached['data']['participation']['subCategoriesByCategoryName'].get(c['category'],[]):
        raise ValueError('Requested subcategory is not currently accepted by the portal')
    settings=registration_config(p['registration_profile'])
    portal=settings['portal'].split('#')[0].rstrip('/')
    # Keep read-only normalization imports independent from the writer adapter.
    from youthgames_register.store import opened as registration_store,import_batch
    # The normal registration writer lock prevents racing an active submitter.
    with registration_store(p['registration_database']) as registrations:
        terminal_before=fingerprint(registrations)
        with allocation_store(p['allocation_database'],readonly=True) as allocations:
            cohort=[dict(r) for r in allocations.execute('SELECT roll,applicant_key FROM allocations WHERE scheme=? ORDER BY roll',(c['scheme'],))]
        records=[dict(r) for r in registrations.execute("""SELECT json_extract(a.value,'$.roll') AS roll,
            json_extract(a.value,'$.applicant_key') AS applicant_key,j.id,j.state,
            json_extract(j.payload,'$.data.SubCategory') AS old_subcategory,json_extract(j.payload,'$.data') AS prior_data,
            EXISTS(SELECT 1 FROM attempts t WHERE t.job=j.id AND (t.intent_at IS NOT NULL OR t.dispatched_at IS NOT NULL)) AS posted
            FROM json_each(?) a LEFT JOIN jobs j ON j.applicant_key=json_extract(a.value,'$.applicant_key')
            AND j.portal=? AND j.campaign=?""",(json.dumps(cohort),portal,settings['campaign']))]
        previous=current(p['allocation_database'],c['scheme'])
        preserve=defaultdict(list)
        blocked=[]
        for row in records:
            old=row['old_subcategory']
            terminal=row['state'] in ('registered','already_registered_external')
            if terminal and old and old!=subcategory:preserve[old].append(row['roll'])
            if not terminal and row['posted'] and old!=subcategory:blocked.append(row['roll'])
        if blocked:raise ValueError(f'{len(blocked)} nonterminal students already have submission intent/history; preserve their category before changing the cohort')
        normal=json.loads(p['normalization_profile'].read_text())
        preview=app.normalization_settings(profile,participation={'subcategory':subcategory,'preserve_rules':[{'when':{'rolls':rolls},'set':{'SubCategory':label}} for label,rolls in sorted(preserve.items())]})
        reviewed=deepcopy(preview)
        reviewed['defaults']['SubCategory']=normal['defaults']['SubCategory'];reviewed['rules']=normal['rules']
        if reviewed!=normal:raise ValueError('Unrelated normalization profile edits detected; preserve them before participation update')
        changed=previous is None or previous['subcategory']!=subcategory
        backup=None
        if changed:
            stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup=p['registration_database'].parent/'backups'/('participation-'+stamp)
            backup.mkdir(parents=True,exist_ok=False)
            with closing(sqlite3.connect(backup/'youthgames.sqlite3')) as output:registrations.backup(output)
            with closing(sqlite3.connect(p['allocation_database'].as_uri()+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(backup/'allocations.sqlite3')) as output:source.backup(output)
            for key in ('normalization_profile','registration_profile'):
                (backup/(key+'.json')).write_bytes(p[key].read_bytes())
            # Rules preserve the actual submitted subcategory in all reports,
            # while the ready subset follows the newly authorized subcategory.
            policy={'subcategory':subcategory,'preserve_rules':[{'when':{'rolls':rolls},'set':{'SubCategory':label}} for label,rolls in sorted(preserve.items())],
                    'basis':'explicit_operator_participation_change','created_at':now(),
                    'terminal_snapshot_sha256':terminal_before,'backup':str(backup)}
            with allocation_store(p['allocation_database']) as allocations:
                allocations.executescript('''CREATE TABLE IF NOT EXISTS participation_changes(
                    id INTEGER PRIMARY KEY,scheme TEXT NOT NULL,policy TEXT NOT NULL,created_at TEXT NOT NULL);
                    CREATE TRIGGER IF NOT EXISTS protect_participation_update BEFORE UPDATE ON participation_changes
                    BEGIN SELECT RAISE(ABORT,'Participation history is immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS protect_participation_delete BEFORE DELETE ON participation_changes
                    BEGIN SELECT RAISE(ABORT,'Participation history cannot be deleted'); END;''')
                allocations.execute('INSERT INTO participation_changes(scheme,policy,created_at) VALUES (?,?,?)',
                                    (c['scheme'],json.dumps(policy),now()))
        else:policy=previous
        normal=json.loads(p['normalization_profile'].read_text())
        generated=app.normalization_settings(profile)
        expected=deepcopy(generated)
        # Only participation defaults and this transition's historical rules
        # are authorized here; unrelated operator edits are not overwritten.
        expected['defaults']['SubCategory']=normal['defaults']['SubCategory']
        expected['rules']=normal['rules']
        if expected!=normal:raise ValueError('Unrelated normalization profile edits detected; preserve them before participation update')
        next_settings=deepcopy(settings)
        next_settings['selection']['subcategory']=subcategory
        next_settings['confirmation']['fields']=list(dict.fromkeys(settings['confirmation']['fields']+['SubCategory']))
        replace_profile(p['registration_profile'],next_settings,settings)
        replace_profile(p['normalization_profile'],generated,normal)
        handoff=app.sync(profile)
        settings=registration_config(p['registration_profile'])
        # Prepare only: one verified form GET, fresh catalog, zero applicant POSTs.
        from youthgames_register.http_pilot import Client,prepare_applicants
        with input_snapshot(settings,p['registration_profile'],allow_empty=True) as (rows,origins,copies,hashes):
            if rows:
                response=(client or Client()).get()
                root,applicants,snapshot=prepare_applicants(settings,p['registration_profile'],list(zip(rows,origins)),copies,hashes,response)
                if any(a['validation']['state']!='ready' for a in applicants):raise ValueError('Prepared input includes blocked students; no submissions were sent')
                prior={r['applicant_key']:json.loads(r['prior_data']) for r in records if r['prior_data'] and r['state'] not in ('registered','already_registered_external')}
                for item in applicants:
                    old=prior.get(item['data']['BForm'])
                    if old and item['data']!={**old,'SubCategory':subcategory}:
                        raise ValueError('Fields other than SubCategory changed; inspect normalization before updating prepared jobs')
                import_batch(registrations,settings,hashes,root,snapshot,applicants)
            ready=len(rows)
        if fingerprint(registrations)!=terminal_before:raise ValueError('Confirmed registration snapshot changed unexpectedly')
        from .resume import context_hash
        context=context_hash(profile,p,settings)
        with registrations:
            registrations.execute('INSERT OR REPLACE INTO prepared_inputs VALUES (?,?,?,?)',
                                  (settings['profile'],context,settings['input']['expected_csv_sha256'],now()))
        return {'status':'changed' if changed else 'already_applied','scheme':c['scheme'],'subcategory':subcategory,
                'ready_records':ready,'completed_preserved':sum(r['state'] in ('registered','already_registered_external') for r in records),
                'historical_category_records':sum(len(r['when']['rolls']) for r in policy['preserve_rules']),
                'nonparticipation_fields_preserved':True,'terminal_snapshot_preserved':True,'applicant_posts':0,'backup':policy['backup'],'handoff':handoff}
