"""No network: fast resume invalidation and indexed completed-applicant skipping."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bise_allocate import resume
from youthgames_register.store import opened,import_batch,ready_job_refs,known_registrations
from tests.test_youthgames import config,applicant,catalog


class PinnedResumeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.profile=self.root/'allocation.json';self.profile.write_text('{}')
        self.settings=config();self.settings['state'].update(database=str(self.root/'jobs.sqlite3'),output=str(self.root/'output'))
        self.paths={key:self.root/(key+'.json') for key in ('normalization_base','normalization_profile','registration_settings','registration_profile','catalog')}
        self.paths['allocation_database']=self.root/'allocations.sqlite3'
        self.paths['registration_database']=self.root/'jobs.sqlite3'
        for path in self.paths.values():
            if path.suffix=='.json':path.write_text('{}')
        for key in ('csv','manifest','provenance'):
            path=self.root/(key+'.txt');path.write_text('synthetic')
            self.settings['input'][key]=str(path)
        self.paths['registration_profile'].write_text(json.dumps(self.settings))

    def prepare(self,**kwargs):
        with patch.object(resume.app,'load',return_value={'scheme':'Synthetic allocation'}),patch.object(resume.app,'paths',return_value=self.paths),patch.object(resume.app,'sync') as sync:
            result=resume.prepare(self.profile,**kwargs)
        return result,sync.call_count

    def test_first_run_refreshes_then_reruns_skip_normalization_and_do_not_read_source_db(self):
        first,calls=self.prepare();self.assertFalse(first['input_reused']);self.assertEqual(calls,1)
        second,calls=self.prepare();self.assertTrue(second['input_reused']);self.assertEqual(calls,0)
        self.assertFalse((self.root/'source.sqlite3').exists())

    def test_explicit_refresh_and_config_or_policy_changes_invalidate(self):
        self.prepare()
        refreshed,calls=self.prepare(refresh=True);self.assertFalse(refreshed['input_reused']);self.assertEqual(calls,1)
        for key in ('normalization_base','catalog'):
            self.paths[key].write_text('{"changed":true}')
            result,calls=self.prepare();self.assertFalse(result['input_reused']);self.assertEqual(calls,1)
        with patch.object(resume,'NAME_POLICY','new-policy'):
            result,calls=self.prepare();self.assertFalse(result['input_reused']);self.assertEqual(calls,1)

    def test_changed_pinned_file_invalidates_and_recovered_row_forces_refresh(self):
        self.prepare();Path(self.settings['input']['provenance']).write_text('changed provenance')
        result,calls=self.prepare();self.assertFalse(result['input_reused']);self.assertEqual(calls,1)
        photo=self.root/'image.jpeg';photo.write_bytes(b'fixture')
        with opened(self.paths['registration_database']) as db:
            import_batch(db,self.settings,{'csv':'old'},catalog(),self.root,[applicant(photo)])
        with patch.object(resume,'recover_browser_jobs',return_value={'recovered_name_formats':[{'job':1}],'applicant_posts':0}):
            result,calls=self.prepare();self.assertEqual(result['recovered_before_refresh'],1);self.assertEqual(calls,1)

    def test_large_terminal_history_does_not_enter_queue_or_load_payloads(self):
        count=20000
        with opened(self.paths['registration_database']) as db:
            portal=self.settings['portal'].split('#')[0].rstrip('/');campaign=self.settings['campaign']
            with db:
                db.execute('INSERT INTO batches VALUES (?,?,?,?,?,?)',('batch',self.settings['profile'],campaign,'{}','{}','snapshot'))
                db.execute('INSERT INTO profiles VALUES (?,?)',(self.settings['profile'],'batch'))
                db.executemany('''INSERT INTO jobs(portal,campaign,applicant_key,roll,payload,payload_hash,state,tracking)
                    VALUES (?,?,?,?,?,?,?,?)''',((portal,campaign,f'{n:013d}',f'{n:06d}','not parsed','hash','registered','TEST-'+str(n)) for n in range(count)))
                db.execute('INSERT INTO jobs(portal,campaign,applicant_key,roll,payload,state) VALUES (?,?,?,?,?,?)',
                    (portal,campaign,'9999999999999','999999','also not parsed','ready'))
                db.execute('INSERT INTO members SELECT ?,id FROM jobs',('batch',))
            queries=[];db.set_trace_callback(queries.append)
            refs=ready_job_refs(db,self.settings,1);known=known_registrations(db,self.settings)
            self.assertEqual(len(known),count);self.assertEqual([dict(r) for r in refs],[{'id':count+1,'roll':'999999'}])
            self.assertTrue(all('payload' not in sql.lower() and 'receipt' not in sql.lower() for sql in queries))
            plan=db.execute("EXPLAIN QUERY PLAN SELECT id,roll,applicant_key FROM jobs WHERE portal=? AND campaign=? AND state='ready' ORDER BY roll,id",(portal,campaign)).fetchall()
            self.assertIn('COVERING INDEX',' '.join(row['detail'] for row in plan))
