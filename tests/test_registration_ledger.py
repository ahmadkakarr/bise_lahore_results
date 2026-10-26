"""Durable exclusion of completed applicants, including SQL resets and reimports."""

from contextlib import contextmanager,closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from youthgames_register import app
from youthgames_register.store import opened,import_batch,jobs,begin,intent,dispatched,finish,known_registrations,authorize_uncertain_retries
from tests.test_youthgames import applicant,config,catalog


class RegistrationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'jobs.sqlite3'
        photo=self.root/'photo.jpeg';photo.write_bytes(b'synthetic image')
        self.settings=config();self.item=applicant(photo)

    def complete(self,db,state='registered'):
        import_batch(db,self.settings,{'csv':'synthetic'},catalog(),self.root,[self.item])
        job=jobs(db,self.settings['profile'])[0]
        attempt=begin(db,job);intent(db,job['id'],attempt);dispatched(db,job['id'],attempt)
        finish(db,job['id'],attempt,state,tracking='SYNTHETIC-123' if state=='registered' else None)
        return db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()

    def test_registered_cannot_reset_delete_replace_or_start_new_attempt(self):
        with opened(self.path) as db:
            job=self.complete(db);before=dict(job)
            for sql,args in [
                ("UPDATE jobs SET state='ready' WHERE id=?",(job['id'],)),
                ("UPDATE jobs SET tracking=NULL WHERE id=?",(job['id'],)),
                ('DELETE FROM jobs WHERE id=?',(job['id'],)),
                ('INSERT INTO attempts(job,started_at) VALUES (?,?)',(job['id'],'synthetic')),
                ("INSERT OR REPLACE INTO jobs(portal,campaign,applicant_key,state) VALUES (?,?,?,'ready')",
                    (job['portal'],job['campaign'],job['applicant_key']))]:
                with self.subTest(sql=sql),self.assertRaises(sqlite3.IntegrityError),db:
                    db.execute(sql,args)
            self.assertEqual(dict(db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()),before)
            with self.assertRaises(ValueError):begin(db,job)
            self.assertEqual(jobs(db,self.settings['profile'],('ready',)),[])

    def test_ledger_is_append_only_even_for_replace(self):
        with opened(self.path) as db:
            self.complete(db);before=tuple(db.execute('SELECT * FROM registered_applicants').fetchone())
            for sql in ('DELETE FROM registered_applicants',"UPDATE registered_applicants SET tracking='CHANGED'"):
                with self.assertRaises(sqlite3.IntegrityError),db:db.execute(sql)
            values=list(before);values[6]='CHANGED'
            with db:db.execute('INSERT OR REPLACE INTO registered_applicants VALUES (?,?,?,?,?,?,?,?,?)',values)
            self.assertEqual(tuple(db.execute('SELECT * FROM registered_applicants').fetchone()),before)

    def test_reopen_profile_change_payload_change_do_not_requeue(self):
        with opened(self.path) as db:before=dict(self.complete(db))
        with opened(self.path) as db:
            settings=deepcopy(self.settings);settings['profile']='Another profile'
            changed=deepcopy(self.item);changed['data']['FullName']='Changed source name'
            import_batch(db,settings,{'csv':'changed'},catalog(),self.root,[changed])
            self.assertEqual(jobs(db,settings['profile'],('ready',)),[])
            self.assertEqual(dict(db.execute('SELECT * FROM jobs').fetchone()),before)
            self.assertEqual(db.execute('SELECT count(*) FROM registered_applicants').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT tracking_id FROM registered_students').fetchone()[0],'SYNTHETIC-123')

    def test_external_registration_is_also_permanently_excluded(self):
        with opened(self.path) as db:
            job=self.complete(db,'already_registered_external')
            self.assertEqual(known_registrations(db,self.settings)[job['applicant_key']]['status'],'already_registered_external')
            with self.assertRaises(sqlite3.IntegrityError),db:
                db.execute("UPDATE jobs SET state='ready'")

    def test_uncertain_retry_is_explicit_atomic_idempotent_and_retains_immutable_history(self):
        with opened(self.path) as db:
            job=self.complete(db,'uncertain');old=dict(job)
            attempt=dict(db.execute('SELECT * FROM attempts').fetchone())
            self.assertEqual(jobs(db,self.settings['profile'],('ready',)),[])
            retried=authorize_uncertain_retries(db,self.settings,self.root/'run')
            self.assertEqual(retried,[{'job':job['id'],'prior_attempt':attempt['id']}])
            self.assertEqual(authorize_uncertain_retries(db,self.settings,self.root/'run'),[])
            self.assertEqual(dict(db.execute('SELECT * FROM attempts').fetchone()),attempt)
            audit=db.execute('SELECT * FROM uncertain_retries').fetchone()
            self.assertEqual(json.loads(audit['original_job']),old)
            for sql in ('DELETE FROM uncertain_retries',"UPDATE uncertain_retries SET profile='changed'",
                        'INSERT OR REPLACE INTO uncertain_retries SELECT * FROM uncertain_retries'):
                with self.assertRaises(sqlite3.IntegrityError),db:db.execute(sql)
            new=begin(db,jobs(db,self.settings['profile'],('ready',))[0]);self.assertNotEqual(new,attempt['id'])
            intent(db,job['id'],new);dispatched(db,job['id'],new)
            finish(db,job['id'],new,'already_registered_external')
            self.assertEqual(authorize_uncertain_retries(db,self.settings,self.root/'another'),[])
            self.assertEqual(db.execute('SELECT outcome FROM attempts WHERE id=?',(attempt['id'],)).fetchone()[0],'uncertain')

    def test_uncertain_retry_excludes_other_profile_conflicts_rolls_and_tracking(self):
        with opened(self.path) as db:
            job=self.complete(db,'uncertain')
            other=deepcopy(self.settings);other['profile']='Empty'
            import_batch(db,other,{},catalog(),self.root,[])
            self.assertEqual(authorize_uncertain_retries(db,other,self.root),[])
            selected=deepcopy(self.settings);selected['selection']['rolls']=['not-this-roll']
            self.assertEqual(authorize_uncertain_retries(db,selected,self.root),[])
            batch=db.execute('SELECT batch FROM profiles WHERE name=?',(self.settings['profile'],)).fetchone()[0]
            with db:db.execute('INSERT INTO conflicts VALUES (?,?,?)',(batch,job['id'],'synthetic conflict'))
            self.assertEqual(authorize_uncertain_retries(db,self.settings,self.root),[])
            with db:
                db.execute('DELETE FROM conflicts')
                db.execute("UPDATE jobs SET tracking='UNCONFIRMED-TRACKING'")
            self.assertEqual(authorize_uncertain_retries(db,self.settings,self.root),[])
            self.assertEqual(db.execute('SELECT count(*) FROM uncertain_retries').fetchone()[0],0)

    def test_legacy_registered_rows_backfill_without_changing_job(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('''CREATE TABLE jobs(id INTEGER PRIMARY KEY,portal TEXT,campaign TEXT,applicant_key TEXT,
              roll TEXT,payload TEXT,payload_hash TEXT,state TEXT,detail TEXT,receipt TEXT,tracking TEXT,updated_at TEXT,
              UNIQUE(portal,campaign,applicant_key))''')
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(1,self.settings['portal'].split('#')[0].rstrip('/'),
                self.settings['campaign'],self.item['data']['BForm'],'001234',json.dumps(self.item),'synthetic',
                'registered','legacy',None,'SYNTHETIC-OLD',None))
            before=db.execute('SELECT * FROM jobs').fetchone()
        for _ in range(2):
            with opened(self.path) as db:
                self.assertEqual(tuple(db.execute('SELECT * FROM jobs').fetchone()),before)
                self.assertEqual(db.execute('SELECT count(*) FROM registered_applicants').fetchone()[0],1)


class CompletedInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_completed_input_never_loads_or_validates_portal_form(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);settings=config();photo=root/'photo.jpeg';photo.write_bytes(b'synthetic image');item=applicant(photo)
            with opened(root/'jobs.sqlite3') as db:
                import_batch(db,settings,{'csv':'old'},catalog(),root,[item])
                job=jobs(db,settings['profile'])[0];attempt=begin(db,job);intent(db,job['id'],attempt)
                finish(db,job['id'],attempt,'registered',tracking='SYNTHETIC-123')
                @contextmanager
                def snapshot(*args,**kwargs):
                    yield [item['data']],[{'roll':item['roll']}],{}, {'csv':'new'}
                with patch.object(app,'input_snapshot',snapshot),patch.object(app,'seal_snapshot',return_value=root), \
                     patch.object(app.web,'catalog',AsyncMock(side_effect=AssertionError('must not open form'))) as live:
                    result=await app.prepare_in(db,object(),settings,root/'profile.json')
                self.assertTrue(result['empty_input']);self.assertEqual(result['skipped_registered'],1)
                live.assert_not_called()
                self.assertEqual(jobs(db,settings['profile'],('ready',)),[])
