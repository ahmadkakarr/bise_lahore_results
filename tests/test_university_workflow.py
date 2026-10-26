import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import date
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from PIL import Image
from bise_normalize.university import generated_dob,apportion,SPORT_WEIGHTS
from youthgames_register.alternating import Alternating
from youthgames_register.photos import payload
from youthgames_register.http_pilot import prepare_form,multipart
from youthgames_register.store import opened,known_registrations,import_batch,begin
from tests.test_youthgames import applicant,config
from tests.test_youthgames_http import form_fixture
from bise_remote.registration_profiles import isolate_outputs
from youthgames_register.reports import output_folder


class ProfileOutputTests(unittest.TestCase):
    def test_legacy_pilot_to_full_run_preserves_receipts_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config').mkdir()
            legacy=output_folder(root/'registration_output','Cohort Pilot')
            receipt=legacy/'receipt.json';receipt.write_text('preserved receipt')
            for file,profile in (('register.json','Cohort'),('register-pilot.json','Cohort Pilot')):
                (root/'config'/file).write_text(json.dumps({'profile':profile,'state':{'output':str(legacy),'database':'same.db'}}))
            with self.assertRaisesRegex(ValueError,'another profile'):
                output_folder(legacy,'Cohort')
            self.assertEqual(len(isolate_outputs(root)),2)
            before={p:p.read_bytes() for p in (root/'config').iterdir()}
            self.assertEqual(isolate_outputs(root),[])
            self.assertEqual(before,{p:p.read_bytes() for p in (root/'config').iterdir()})
            for file in ('register.json','register-pilot.json'):
                config=json.loads((root/'config'/file).read_text())
                output_folder(config['state']['output'],config['profile'])
                self.assertEqual(config['state']['database'],'same.db')
            self.assertEqual(receipt.read_text(),'preserved receipt')
            self.assertEqual(json.loads((legacy/'Data/owner.json').read_text())['profile'],'Cohort Pilot')

    def test_unexpected_output_path_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config').mkdir();p=root/'config/register.json'
            p.write_text(json.dumps({'state':{'output':str(root/'unrelated')}}));before=p.read_bytes()
            with self.assertRaisesRegex(ValueError,'Unexpected'):isolate_outputs(root)
            self.assertEqual(p.read_bytes(),before)


class UniversityPolicyTests(unittest.TestCase):
    def test_randomized_dobs_are_stable_distributed_and_explicitly_provisional(self):
        policy={'birth_year':2006,'seed':'test-seed','operator_confirmed_admin_exception':True}
        dates=[generated_dob(str(i),policy) for i in range(1000)]
        self.assertGreater(len(set(dates)),300)
        self.assertTrue(all(date.fromisoformat(v).year==2006 for v in dates))
        self.assertEqual(dates[19],generated_dob('19',policy))
        policy['operator_confirmed_admin_exception']=False
        with self.assertRaises(ValueError):generated_dob('19',policy)

    def test_all_games_have_positive_weight_and_largest_remainder_preserves_total(self):
        self.assertEqual(len(SPORT_WEIGHTS),21);self.assertEqual(sum(SPORT_WEIGHTS.values()),100)
        self.assertEqual(sum(apportion(203,SPORT_WEIGHTS).values()),203)
        self.assertTrue(all(v>0 for v in apportion(203,SPORT_WEIGHTS).values()))
        female={k:v for k,v in SPORT_WEIGHTS.items() if 'boys only' not in k.casefold()}
        self.assertEqual(len(female),19);self.assertEqual(sum(apportion(14000,female).values()),14000)

    def test_universities_form_sends_empty_hidden_uc_and_current_category_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'photo.jpg';Image.new('RGB',(40,50),'blue').save(path)
            item=applicant(path);settings=config();settings['selection']['category']='Universities'
            settings['inspection'].update(observed_secondary_dob_min='2000-01-01',observed_secondary_dob_max='2009-12-31')
            item['data'].update(Category='Universities',DOB='2006-03-14',UC='')
            item['option_ids'].pop('UC');item['dob_rule']={'min':'2000-01-01','max':'2009-12-31'}
            html=form_fixture().replace('Schools (Secondary)','Universities').replace('2009-01-01','2000-01-01').replace('2016-12-31','2009-12-31')
            html=html.replace('"showUcByCategoryName": {"Universities": true}','"showUcByCategoryName": {"Universities": false}')
            fields,_=prepare_form(html,settings,item)
            self.assertEqual(fields['uc_id'],'');self.assertEqual(fields['date_of_birth'],'2006-03-14')

    def test_original_photo_is_bound_to_bform_roll_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'original.sqlite3';image=BytesIO();Image.new('RGB',(50,60),'green').save(image,format='JPEG');data=image.getvalue()
            digest=hashlib.sha256(data).hexdigest()
            with closing(sqlite3.connect(path)) as db:
                db.executescript('CREATE TABLE jobs(id INTEGER PRIMARY KEY,result_json TEXT);CREATE TABLE student_photos(job_id INTEGER,sha256 TEXT,payload BLOB);')
                record={'student_cnic':'0000001234562','roll':'500001','course':'HSSC','year':2024,'exam_type':'2'}
                db.execute('INSERT INTO jobs VALUES(1,?)',(json.dumps(record),));db.execute('INSERT INTO student_photos VALUES(1,?,?)',(digest,data));db.commit()
            ref={'kind':'original_sqlite_reference','source_database':str(path),'job_id':1,'roll':'500001','applicant_key':'0000001234562'}
            self.assertEqual(payload(ref,digest),data)
            with self.assertRaises(ValueError):payload({**ref,'applicant_key':'0000001234563'},digest)
            with self.assertRaises(ValueError):payload(ref,'0'*64)
            with self.assertRaisesRegex(ValueError,'Portrait owner'):
                multipart({'cnic':'00000-0123456-3'},ref,digest)

    def test_imported_terminal_identity_cannot_be_claimed_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);image=root/'pic.jpg';Image.new('RGB',(3,3),'blue').save(image)
            item=applicant(image);settings=config()
            with opened(root/'reg.sqlite3') as db:
                import_batch(db,settings,{}, {},root,[item])
                with db:db.execute('INSERT INTO imported_registrations VALUES(?,?,?,?,?,?,?,?)',
                    ('https://youthgames.punjab.gov.pk',settings['campaign'],item['data']['BForm'],'registered','TRACK-OLD','2026-10-25','source-hash','payload-hash'))
                self.assertIn(item['data']['BForm'],known_registrations(db,settings))
                job=db.execute('SELECT * FROM jobs').fetchone()
                with self.assertRaises(sqlite3.IntegrityError):begin(db,job)


class AlternatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_preparation_cannot_reorder_posts_and_turn_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=sqlite3.connect(Path(tmp)/'reg');self.addCleanup(db.close)
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,payload TEXT)')
            for ident,group in ((1,'Raiwind'),(2,'Raiwind'),(3,'Model Town'),(4,'Model Town')):
                db.execute('INSERT INTO jobs VALUES(?,?)',(ident,json.dumps({'data':{'Tehsil':group}})))
            db.commit();settings={'selection':{'dispatch_pattern':['Raiwind','Model Town'],'dispatch_key':'test'}}
            refs=[{'id':i,'roll':str(i)} for i in range(1,5)];order=Alternating(db,settings,refs)
            self.assertEqual([r['id'] for r in order.references],[1,3,2,4])
            stop=asyncio.Event();actual=[]
            async def ready(ident,delay):
                await asyncio.sleep(delay);await order.wait(ident,stop)
                with db:order.record(ident,'now')
                actual.append(ident);order.release(ident)
            await asyncio.gather(ready(1,.03),ready(3,0),ready(2,.01),ready(4,0))
            self.assertEqual(actual,[1,3,2,4])
            self.assertEqual(db.execute('SELECT next_group,sequence FROM alternating_turns').fetchone(),('Raiwind',4))
            again=Alternating(db,settings,refs)
            self.assertEqual(again.references[0]['id'],1)

    async def test_failed_preparation_releases_its_turn_without_blocking_other_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            with closing(sqlite3.connect(Path(tmp)/'reg')) as db:
                db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,payload TEXT)')
                for i,g in ((1,'Raiwind'),(2,'Model Town')):db.execute('INSERT INTO jobs VALUES(?,?)',(i,json.dumps({'data':{'Tehsil':g}})))
                db.commit();order=Alternating(db,{'selection':{'dispatch_pattern':['Raiwind','Model Town'],'dispatch_key':'key'}},[{'id':1},{'id':2}])
                order.release(1)
                await asyncio.wait_for(order.wait(2,asyncio.Event()),1)
