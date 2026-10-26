"""Private manual institute payload and bounded idempotent pilot regressions."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from playwright.async_api import async_playwright
from bise_allocate import private_pilot, private_pool
from youthgames_register import browser, http_pilot
from youthgames_register.config import sha, input_snapshot, load
from youthgames_register.institutes import institute_fields, private_manual_supported
from tests import test_private_pool as pool_fixtures
from tests.test_youthgames import applicant, config
from tests.test_youthgames_http import form_fixture


def private_fixture():
    html = form_fixture().replace('Open Category', 'Private').replace('value="12"', 'value="2"').replace('"id": "12"', '"id": "2"')
    return html.replace('<input type="hidden" name="institution_id" value=""><input type="hidden" name="institute_name" value="">',
        '<input type="hidden" id="institution-id" name="institution_id" value="">'
        '<input type="text" id="institute-name-text" name="institute_name" value="" style="display:none">'
        '<button id="use-manual-institute-btn" type="button" onclick="let e=document.getElementById(\'institute-name-text\');e.style.display=\'block\';e.required=true">Enter manually</button>')


class PrivateContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.photo=Path(self.temp.name)/'photo.jpeg';Image.new('RGB',(3,3)).save(self.photo)
        self.item=applicant(self.photo);self.item['data'].update(SubCategory='Private',Institute='ACTUAL SCHOOL (GIRLS), LAHORE')
        self.item['option_ids']['SubCategory']='2'
        self.config=config();self.config['selection']['subcategory']='Private'

    def test_private_exact_manual_school_and_no_invented_institution_id(self):
        fields,_=http_pilot.prepare_form(private_fixture(),self.config,self.item)
        self.assertEqual(fields['institute_name'],self.item['data']['Institute'])
        self.assertEqual(fields['institution_id'],'')
        body,ctype=http_pilot.multipart(fields,self.photo,sha(self.photo.read_bytes()))
        self.assertIn(self.item['data']['Institute'].encode(),body)

    def test_missing_manual_contract_stale_id_and_blank_school_block(self):
        for html,name in [(private_fixture().replace('id="use-manual-institute-btn"','id="changed"'),'Actual school'),
                          (private_fixture().replace('name="institution_id" value=""','name="institution_id" value="123"'),'Actual school'),
                          (private_fixture(),'')]:
            with self.subTest(name=name):
                item=deepcopy(self.item);item['data']['Institute']=name
                with self.assertRaises(ValueError):http_pilot.prepare_form(html,self.config,item)

    def test_contract_inside_form_only_and_supported_subcategories(self):
        self.assertTrue(private_manual_supported(private_fixture()))
        self.assertFalse(private_manual_supported(form_fixture()))
        self.assertFalse(private_manual_supported(private_fixture().replace('id="registration-form"','id="other"')))
        with self.assertRaises(ValueError):institute_fields({'SubCategory':'Public','Institute':'School'})
        self.assertEqual(institute_fields({'SubCategory':'Open Category'}),{'institution_id':'','institute_name':''})


class PrivateBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_fills_actual_manual_input_and_gate_blocks_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo=Path(tmp)/'photo.jpeg';Image.new('RGB',(3,3)).save(photo)
            item=applicant(photo);item['data'].update(SubCategory='Private',Institute='Actual School, LAHORE');item['option_ids']['SubCategory']='2'
            settings=config();settings['selection']['subcategory']='Private'
            async with async_playwright() as manager:
                instance=await manager.chromium.launch(headless=True)
                try:
                    page=await instance.new_page()
                    await page.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=private_fixture()))
                    _,expected,snapshot=await browser.fill(page,settings,item)
                    self.assertEqual(expected['institute_name'],'Actual School, LAHORE')
                    self.assertEqual(snapshot['invalid'],[])
                    self.assertEqual(expected['institution_id'],'')
                    # Test the outgoing gate using the actual multipart encoder.
                    fields,_=http_pilot.prepare_form(private_fixture(),settings,item)
                    gate=browser.PostGate();gate.armed=True;gate.fields={**expected,'disclaimer_accepted':'1','captcha':'5'}
                    gate.photo_hash=sha(photo.read_bytes());gate.verified_file={'sha256':gate.photo_hash,'name':'profile.jpeg'}
                    calls=[];gate.before_dispatch=lambda:calls.append('dispatch')
                    class Request: method='POST';url=http_pilot.BASE+'/applicant'
                    class Route:
                        request=Request()
                        async def continue_(self):calls.append('continue')
                        async def abort(self):calls.append('abort')
                    route=Route()
                    for tampered in (True,False):
                        outgoing={**fields,'institute_name':'Wrong School' if tampered else fields['institute_name']}
                        body,ctype=http_pilot.multipart(outgoing,photo,gate.photo_hash)
                        route.request.headers={'content-type':ctype};route.request.post_data_buffer=body
                        gate.error=None
                        await gate.route(route)
                        if tampered:self.assertEqual(calls,['abort']);self.assertFalse(gate.sent)
                        else:self.assertEqual(calls,['abort','dispatch','continue']);self.assertTrue(gate.sent)
                finally:await instance.close()


class PrivatePilotTests(unittest.TestCase):
    def setUp(self):
        f=pool_fixtures.PrivatePoolTests('test_either_command_first_freezes_one_disjoint_cohort_and_preserves_actual_institutes')
        f.setUp();self.addCleanup(f.doCleanups);f.prepare();self.f=f
        photo=f.root/'photo.jpeg';Image.new('RGB',(3,3)).save(photo)
        settings=json.loads((f.root/'reg.json').read_text());settings['photos'].update(shared_file=str(photo),expected_sha256=sha(photo.read_bytes()))
        (f.root/'reg.json').write_text(json.dumps(settings))
        regdir=f.root/'registration_profiles';regdir.mkdir();(regdir/'settings.json').write_text(json.dumps(settings))
        f.plan['paths']['registration_settings']=str(regdir/'settings.json');f.plan_path.write_text(json.dumps(f.plan))
        private_pool.run(f.plan_path,'raiwind');private_pool.run(f.plan_path,'model_town')
        self.profiles=[f.root/f.plan['groups'][g]['allocation_profile'] for g in ('raiwind','model_town')]

    def test_pins_three_per_group_disjoint_and_no_replacements_on_rerun(self):
        files=[private_pilot.prepare(p) for p in self.profiles]
        identities=[]
        for path in files:
            settings=load(path);before=path.read_bytes()
            self.assertEqual(settings['selection']['subcategory'],'Private')
            with input_snapshot(settings,path) as (rows,origins,*_):
                self.assertEqual(len(rows),3);identities.append({r['BForm'] for r in rows})
                self.assertEqual(len({r['UC'] for r in rows}),3)
            with patch.object(private_pilot.app,'sync',side_effect=AssertionError('Pinned pilots must not resync/reselect')):
                self.assertEqual(private_pilot.prepare(self.profiles[files.index(path)]),path)
            self.assertEqual(path.read_bytes(),before)
        self.assertFalse(identities[0]&identities[1])

    def test_modified_profile_fails_before_any_post(self):
        path=private_pilot.prepare(self.profiles[0]);path.write_text(path.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'integrity'):private_pilot.prepare(self.profiles[0])

    def test_unknown_held_and_confirmed_skips_without_transport(self):
        for passed,state in [(False,'uncertain'),(True,'registered'),(False,'ready')]:
            with patch.object(private_pilot,'prepare',return_value=Path('unused')),patch.object(private_pilot,'verify',return_value={'pilot_passed':passed,'checks':[{'state':state,'prior_dispatches':1}]}),patch('youthgames_register.http_async.run',side_effect=AssertionError('No replay allowed')):
                result=asyncio.run(private_pilot.run(self.profiles[0]))
                self.assertEqual(result['applicant_posts'],0)


class PrivateAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_private_pipeline_receipts_and_rerun_skip_before_get(self):
        import csv
        from tests import test_http_concurrent as concurrent
        from youthgames_register.store import opened,jobs,check_receipt
        f=concurrent.ConcurrentHttpTests('test_parallel_gets_and_overlapping_posts_have_separate_bounds_and_shared_rate')
        await f.asyncSetUp();self.addCleanup(f.doCleanups);f.write_input(3)
        folder=f.root/'input'
        origins=[json.loads(line) for line in (folder/'Data/provenance.jsonl').read_text().splitlines()]
        for i,origin in enumerate(origins):
            origin['data'].update(SubCategory='Private',Institute=f'Actual School {i}, LAHORE')
        with (folder/'Ready.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(origins[0]['data']));writer.writeheader();writer.writerows(o['data'] for o in origins)
        (folder/'Data/provenance.jsonl').write_text(''.join(json.dumps(o)+'\n' for o in origins))
        hashes={name:sha((folder/name).read_bytes()) for name in ('Ready.csv','Data/provenance.jsonl')}
        (folder/'Data/manifest.json').write_text(json.dumps({'files':hashes}))
        f.settings['selection']['subcategory']='Private';f.settings['input']['expected_csv_sha256']=hashes['Ready.csv'];f.settings['confirmation']['input_sha256']=hashes['Ready.csv']
        f.profile.write_text(json.dumps(f.settings))
        old=f.html
        # Keep the synthetic Raiwind location catalogs; use the verified manual
        # control shape and Private participation option from the form fixture.
        manual=private_fixture().split('<input type="hidden" id="institution-id"')[1].split('<select')[0]
        f.html=old.replace('Open Category','Private').replace('value="12"','value="2"').replace('"id": "12"','"id": "2"')
        f.html=f.html.replace('<input type="hidden" name="institution_id" value=""><input type="hidden" name="institute_name" value="">','<input type="hidden" id="institution-id"'+manual)
        result=await f.run_batch(2,limit=3)
        self.assertEqual(result['registered_now'],3)
        self.assertEqual(len(f.posts),3)
        self.assertEqual({p['institute_name'].decode() for p in f.posts},{o['data']['Institute'] for o in origins})
        self.assertTrue(all(p['institution_id']==b'' for p in f.posts))
        with opened(f.root/'jobs.sqlite3') as db:
            for job in jobs(db,'Lahore'):
                self.assertTrue(check_receipt(job['receipt']))
                proof=json.loads(Path(job['receipt']).with_name('http-request-proof.json').read_text())
                self.assertEqual(proof['institute_submission']['institute_name'],json.loads(job['payload'])['data']['Institute'])
        before=f.gets
        rerun=await f.run_batch(2,limit=3)
        self.assertEqual(rerun['registered_now'],0);self.assertEqual(f.gets,before);self.assertEqual(len(f.posts),3)
