"""Synthetic allocation, normalization, durable identity and tracking regressions."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bise_allocate import app
from bise_allocate.store import opened as allocation_store
from bise_normalize.app import execute
from bise_normalize.config import load as load_normal
from bise_normalize.sources import table
from bise_results.parser import parse_result
from youthgames_register.store import opened as registration_store
from youthgames_register.reports import export
from youthgames_register.config import sha
from tests.test_youthgames import config as register_config
from tests.test_identity_fields import FIXTURE


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'results.sqlite3';self.reg=self.root/'youthgames.sqlite3';self.alloc=self.root/'allocations.sqlite3'
        with registration_store(self.reg):pass
        catalog=json.loads(Path('data/youthgames_catalog.json').read_text())
        catalog['fetched_at']=datetime.now(timezone.utc).isoformat()
        (self.root/'catalog.json').write_text(json.dumps(catalog))
        base=load_normal(Path('normalization_profiles/9th_combined.json'))
        base['target']['template']=None
        base['validation']['catalog']=str(self.root/'catalog.json')
        base['portal'].update(normalization_database=str(self.root/'normalization.sqlite3'),registration_database=str(self.reg))
        (self.root/'base.json').write_text(json.dumps(base))
        reg=register_config();reg['state'].update(database=str(self.reg),output=str(self.root/'registered'),write_reports=False)
        (self.root/'reg.json').write_text(json.dumps(reg))
        definition=json.loads(Path('allocation_profiles/lahore_9th_raiwind.json').read_text())
        definition.update(scheme='Synthetic allocation',expected_records=43)
        definition['paths']={
            'source_database':str(self.source),'allocation_database':str(self.alloc),'registration_database':str(self.reg),
            'catalog':str(self.root/'catalog.json'),'normalization_base':str(self.root/'base.json'),
            'normalization_profile':str(self.root/'normalized-profile.json'),'registration_settings':str(self.root/'reg.json'),
            'registration_profile':str(self.root/'register-profile.json'),'output':str(self.root/'normalized')}
        self.profile=self.root/'allocation.json';self.profile.write_text(json.dumps(definition));self.definition=definition
        self.original=parse_result(FIXTURE.with_name('ssc-part1.html').read_text(),'001234',exam_type='1')
        self.rows=[]
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('CREATE TABLE jobs(roll TEXT,year INTEGER,course TEXT,exam_type TEXT,state TEXT,result_json TEXT)')
            for index in range(43):
                row={**self.original,'roll':f'{index+1:06d}','institution':'LAHORE','name':'Synthetic Student',
                     'student_cnic':f'{index:012d}2','date_of_birth':'09/05/2010'}
                self.rows.append(row)
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(row['roll'],2026,'SSC','1','verified',json.dumps(row)))
            for roll,institution,birth,part,state in [('900001','Named School Lahore','09/05/2010','1','verified'),('900002','LAHORE','09/05/2008','1','verified'),('900003','LAHORE','09/05/2010','2','verified'),('900004','LAHORE','09/05/2010','1','pending')]:
                row={**self.original,'roll':roll,'institution':institution,'date_of_birth':birth,'exam_type':part}
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(roll,2026,'SSC',part,state,json.dumps(row)))

    def insert_registered(self,roll,uc,tehsil='Raiwind'):
        with allocation_store(self.alloc,readonly=True) as db:
            row=db.execute('SELECT applicant_key FROM allocations WHERE roll=?',(roll,)).fetchone()
        with registration_store(self.reg) as db:
            db.execute('INSERT INTO jobs(portal,campaign,applicant_key,roll,payload,payload_hash,state,tracking,receipt) VALUES(?,?,?,?,?,?,?,?,?)',
                ('https://youthgames.punjab.gov.pk','punjab-youthgames-2026',row[0],roll,
                 json.dumps({'data':{'Tehsil':tehsil,'UC':uc}}),'synthetic','registered','TEST-TRACK-'+roll,'synthetic-receipt'))
            db.commit()

    def test_exact_portal_ids_balancing_freezing_and_source_preservation(self):
        before=self.source.read_bytes()
        self.assertEqual(app.apply(self.profile)['assigned'],43)
        with allocation_store(self.alloc,readonly=True) as db:
            assignments=[tuple(row) for row in db.execute('SELECT roll,uc_number,assigned_at FROM allocations ORDER BY roll')]
            counts=[r[0] for r in db.execute('SELECT count(*) FROM allocations GROUP BY uc_number')]
            self.assertEqual(sorted(counts),[2]*17+[3]*3)
            self.assertEqual(db.execute('SELECT uc,uc_id,tehsil_id FROM allocation_targets WHERE uc_number=255').fetchone()[:],('UC No. 255, Satto Katia','2330','76'))
        self.assertEqual(app.apply(self.profile)['status'],'already_allocated')
        with allocation_store(self.alloc,readonly=True) as db:
            self.assertEqual(assignments,[tuple(row) for row in db.execute('SELECT roll,uc_number,assigned_at FROM allocations ORDER BY roll')])
        self.assertEqual(before,self.source.read_bytes())

    def test_changed_scope_or_unconfirmed_allocation_cannot_reassign(self):
        app.apply(self.profile)
        c=deepcopy(self.definition);c['tehsil']='Shalimar';self.profile.write_text(json.dumps(c))
        with self.assertRaisesRegex(ValueError,'definition changed'):app.apply(self.profile)
        c['administrative_allocation_confirmed']=False;self.profile.write_text(json.dumps(c))
        with self.assertRaisesRegex(ValueError,'confirm'):app.load(self.profile)

    def test_wrong_count_fails_without_partial_allocation(self):
        c=deepcopy(self.definition);c['expected_records']=44;self.profile.write_text(json.dumps(c))
        with self.assertRaisesRegex(ValueError,'Expected 44'):app.apply(self.profile)
        with allocation_store(self.alloc,readonly=True) as db:self.assertEqual(db.execute('SELECT count(*) FROM allocations').fetchone()[0],0)

    def test_normalization_uses_allocated_uc_and_records_origin_without_excel(self):
        app.apply(self.profile);result=app.normalize(self.profile)
        self.assertEqual(result['summary']['normalized'],43)
        self.assertEqual(result['summary']['ready_to_register'],43)
        self.assertFalse(list((self.root/'normalized').glob('*.xlsx')))
        origin=json.loads((self.root/'normalized/Data/provenance.jsonl').read_text().splitlines()[0])
        self.assertEqual(origin['data']['Tehsil'],'Raiwind')
        self.assertEqual(origin['data']['UC'],'UC No. 255, Satto Katia')
        self.assertEqual(origin['raw']['Institution / District'],'LAHORE')
        self.assertFalse(origin['administrative_allocation']['residence_inferred'])
        self.assertIn('administrative_allocation',origin['origins']['UC'])
        status=app.progress(self.profile)
        self.assertEqual(status['ready_remaining'],43)
        self.assertEqual(status['normalization_blocked'],0)
        before=(self.root/'normalized/Data/manifest.json').read_bytes()
        self.assertEqual(app.normalize(self.profile)['status'],'already_up_to_date')
        self.assertEqual(before,(self.root/'normalized/Data/manifest.json').read_bytes())

    def test_duplicate_identity_across_different_ucs_stays_blocked(self):
        duplicate={**self.rows[1],'student_cnic':self.rows[0]['student_cnic']}
        with closing(sqlite3.connect(self.source)) as db, db:db.execute('UPDATE jobs SET result_json=? WHERE roll=?',(json.dumps(duplicate),duplicate['roll']))
        app.apply(self.profile);r=app.normalize(self.profile)
        self.assertEqual(r['summary']['normalized'],43)
        self.assertEqual(r['summary']['ready_to_register'],41)
        self.assertEqual(r['summary']['missing_details'],2)
        status=app.progress(self.profile)
        self.assertEqual(status['normalization_blocked'],2)
        self.assertEqual(status['ready_remaining'],41)

    def test_allocated_database_requires_explicit_scheme_and_does_not_mix_schemes(self):
        app.apply(self.profile)
        profile=app.normalization_profile(self.profile)
        settings=load_normal(profile)['source']
        settings['allocation_scheme']=None
        with self.assertRaisesRegex(ValueError,'require source.allocation_scheme'):
            list(table(self.alloc,settings))
        settings['allocation_scheme']='Missing scheme'
        self.assertEqual(len(list(table(self.alloc,settings))),1)  # Header only, no other cohort fallback.

    def test_registration_status_joins_tracking_without_changing_original_assignment(self):
        app.apply(self.profile)
        self.insert_registered('000001','UC No. 255, Satto Katia')
        self.insert_registered('000002','UC No. 178, Manawan Ram Pur Jageer','Shalimar')
        status=app.progress(self.profile)
        self.assertEqual(status['registered'],2);self.assertEqual(status['tracking_ids_saved'],2)
        target=next(x for x in status['ucs'] if x['uc_number']==256)
        self.assertEqual(target['registered_elsewhere'],1)
        row=app.progress(self.profile,'000001')['records'][0]
        self.assertEqual(row['tracking'],'TEST-TRACK-000001')
        self.assertEqual(row['submitted_uc'],row['uc'])

    def test_pilot_is_one_per_uc_and_durable_after_registration(self):
        app.apply(self.profile);app.normalize(self.profile)
        selected=app.select_pilots(self.profile)
        self.assertEqual(len(selected),20);self.assertEqual(len({p['uc_number'] for p in selected}),20)
        self.insert_registered(selected[0]['roll'],'UC No. 255, Satto Katia')
        self.assertEqual(selected,app.select_pilots(self.profile))

    def test_http_pins_are_separate_stable_and_exclude_browser_pins(self):
        app.apply(self.profile);app.normalize(self.profile)
        browser=app.select_pilots(self.profile)
        direct=app.select_pilots(self.profile,transport='http')
        self.assertEqual(len(direct),20)
        self.assertFalse({x['roll'] for x in browser}&{x['roll'] for x in direct})
        self.assertEqual(direct,app.select_pilots(self.profile,transport='http'))

    def test_twenty_uc_http_pilot_uses_no_browser_and_rerun_sends_nothing(self):
        from contextlib import redirect_stdout
        from email.parser import BytesParser
        from email.policy import default
        import io,re
        from unittest.mock import patch
        from PIL import Image
        from bise_allocate import http as direct
        from youthgames_register import browser,http_pilot
        from tests.test_youthgames_http import form_fixture
        from tests.test_youthgames import SUCCESS
        image=self.root/'image.jpeg';Image.new('RGB',(3,3),'white').save(image)
        settings=json.loads((self.root/'reg.json').read_text())
        settings['photos'].update(shared_file=str(image),expected_sha256=sha(image.read_bytes()))
        (self.root/'reg.json').write_text(json.dumps(settings))
        definition=deepcopy(self.definition);definition['paths']['output']=str(self.root/'normalized_output/Synthetic')
        self.profile.write_text(json.dumps(definition))
        app.apply(self.profile);app.normalize(self.profile)
        old=app.select_pilots(self.profile)
        for selected in old:
            target=app.targets(definition,json.loads((self.root/'catalog.json').read_text()))[selected['uc_number']-255]
            self.insert_registered(selected['roll'],target['UC'])
        targets=app.targets(definition,json.loads((self.root/'catalog.json').read_text()))
        html=form_fixture().replace('Shalimar','Raiwind').replace('"77"','"76"')
        html=re.sub(r'const ucsByDistrict = .*?;</script>',
            'const ucsByDistrict = '+json.dumps({'21':[{'id':x['uc_id'],'name':x['UC']} for x in targets]})+';</script>',html)
        sent=[];created=[]
        class Client:
            def __init__(self):self.posts=0;created.append(self)
            def get(self):return http_pilot.Response(200,html)
            def post(self,body,content_type):
                self.posts+=1
                if self.posts>1:raise AssertionError('Replay')
                message=BytesParser(policy=default).parsebytes(b'Content-Type: '+content_type.encode()+b'\r\n\r\n'+body)
                fields={p.get_param('name',header='content-disposition'):(p.get_payload(decode=True) or b'') for p in message.iter_parts()}
                sent.append(fields['uc_id'].decode())
                return http_pilot.Response(200,SUCCESS.replace('TEST-YG-123','TEST-HTTP-'+sent[-1]))
        with patch.object(browser,'visible_browser',side_effect=AssertionError('Browser must not launch')),redirect_stdout(io.StringIO()):
            report=direct.run(self.profile,client_factory=Client,sleep=lambda _:None)
            self.assertTrue(report['pilot_passed']);self.assertEqual(report['attempted_now'],20)
            self.assertEqual(set(sent),{x['uc_id'] for x in targets})
            count=len(created)
            repeated=direct.run(self.profile,client_factory=Client,sleep=lambda _:None)
            self.assertEqual(repeated['attempted_now'],0);self.assertEqual(len(created),count)
        self.assertEqual(len(sent),20)
        self.assertEqual(report['registered'],40)

    def test_database_only_registration_summary_does_not_write_workbooks(self):
        app.apply(self.profile)
        c=register_config();c['state']['write_reports']=False
        with registration_store(self.reg) as db:
            db.execute("INSERT INTO batches(id,profile) VALUES('test',?)",(c['profile'],))
            db.execute("INSERT INTO profiles VALUES(?,'test')",(c['profile'],));db.commit()
            counts=export(db,c,self.root/'reports')
            self.assertEqual(counts['total'],0)
            self.assertFalse(list((self.root/'reports').glob('*.xlsx')))

    def test_pilot_load_recovery_requires_receipt_and_no_intent_or_dispatch(self):
        app.apply(self.profile)
        job_ids=[]
        with allocation_store(self.alloc) as db:
            for index,row in enumerate(self.rows[:6]):
                db.execute('INSERT INTO allocation_pilots VALUES(?,?,?,?,?)',
                    ('Synthetic allocation',255+index,row['roll'],row['student_cnic'],'synthetic'))
        with registration_store(self.reg) as db, db:
            for index,row in enumerate(self.rows[:6]):
                state='registered' if index==3 else 'blocked_input'
                detail='Invalid field' if index==2 else 'TimeoutError: Page.goto: Timeout 60000ms exceeded.'
                job=db.execute('INSERT INTO jobs(portal,campaign,applicant_key,roll,payload,payload_hash,state,detail) VALUES(?,?,?,?,?,?,?,?)',
                    ('https://youthgames.punjab.gov.pk','punjab-youthgames-2026',row['student_cnic'],row['roll'],'{}','hash','blocked_input',detail)).lastrowid
                job_ids.append(job)
                attempt=db.execute('INSERT INTO attempts(job,started_at,intent_at,dispatched_at,outcome) VALUES(?,?,?,?,?)',
                    (job,'synthetic','intent' if index==1 else None,'dispatch' if index==1 else None,'blocked_input')).lastrowid
                body=self.root/f'body-{index}.html';body.write_text('Synthetic form')
                receipt=self.root/f'receipt-{index}.json'
                receipt.write_text(json.dumps({'attempt':attempt,'payload_hash':'hash','post_dispatched':index==5,
                    'files':{'response.html':{'path':str(body),'sha256':'damaged' if index==4 else sha(body.read_bytes())}}}))
                db.execute('UPDATE jobs SET receipt=? WHERE id=?',(str(receipt),job))
                # Finalize only after constructing its attempt/receipt; the
                # permanent registered ledger correctly blocks later edits.
                if state=='registered':db.execute("UPDATE jobs SET state='registered' WHERE id=?",(job,))
        self.assertEqual(app.recover_pilot_loads(self.profile),[job_ids[0]])
        with registration_store(self.reg) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT state FROM jobs ORDER BY id')],
                ['ready','blocked_input','blocked_input','registered','blocked_input','blocked_input'])
            self.assertEqual(db.execute('SELECT outcome FROM attempts WHERE job=?',(job_ids[0],)).fetchone()[0],'blocked_input')
