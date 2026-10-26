"""HTTP pilot fixtures: no live network requests or applicant registrations."""
from contextlib import contextmanager
from copy import deepcopy
from email.parser import BytesParser
from email.policy import default
import io,json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from youthgames_register import http_pilot as http
from youthgames_register.config import sha
from youthgames_register.store import opened,import_batch,jobs,check_receipt
from tests.test_youthgames import config,catalog,applicant,fixture,SUCCESS,REJECTED


def form_fixture():
    cat=catalog(); options=cat['options']; conv=lambda v:[{'id':o['value'],'name':o['text']} for o in v]
    part={'subCategories':conv(options['SubCategory']),'sports':conv(options['Sport']),
        'dobEligibilityRulesByCategoryName':{'Schools (Secondary)':{'min_dob':'2009-01-01','max_dob':'2016-12-31'}},
        'subCategoriesByCategoryName':{'Schools (Secondary)':['Open Category']},
        'sportsByCategoryName':{'Schools (Secondary)':['Athletics']},'showUcByCategoryName':{'Schools (Secondary)':True}}
    return fixture()+f'<script>window.participationCatalog = {json.dumps(part)};const districtsByDivision = {json.dumps({"6":conv(options["District"])})};const tehsilsByDistrict = {json.dumps({"21":conv(options["Tehsil"])})};const ucsByDistrict = {json.dumps({"21":conv(options["UC"])})};</script>'


class LiteralTests(unittest.TestCase):
    def test_data_only_js_literals_and_escaped_slashes(self):
        value=http.Literal(r'''{name:'Schools', nested:[true, false, null,], path:"A\/B", number:12,}''').value()
        self.assertEqual(value,{'name':'Schools','nested':[True,False,None],'path':'A/B','number':12})
    def test_executable_expressions_and_duplicate_keys_are_rejected(self):
        for value in ['{x: fetch("secret")}', '[1+2]', '{a:1,a:2}', '{x:undefined}', '{x:function(){}}']:
            with self.subTest(value=value),self.assertRaises(ValueError):http.Literal(value).value()
    def test_post_preserving_and_cross_origin_redirects_are_blocked(self):
        from urllib.request import Request
        request=Request(http.BASE+'/applicant',data=b'test',method='POST')
        for status,url in [(307,http.BASE+'/applicant'),(308,http.BASE+'/applicant'),(302,'https://example.com/')]:
            with self.subTest(status=status,url=url),self.assertRaises(ValueError):http.Redirects().redirect_request(request,None,status,'',{},url)
        result=http.Redirects().redirect_request(request,None,302,'',{},http.BASE+'/')
        self.assertEqual(result.method,'GET');self.assertIsNone(result.data)


class HttpPilotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.photo=self.root/'photo.jpeg';Image.new('RGB',(3,3),'white').save(self.photo)
        self.settings=config();self.settings['photos']['expected_sha256']=sha(self.photo.read_bytes())
        self.settings['state']={'database':str(self.root/'jobs.sqlite3'),'output':str(self.root/'reports')}
        self.item=applicant(self.photo);self.item['operator_confirmation']=self.settings['confirmation']
        with opened(self.root/'jobs.sqlite3') as db:import_batch(db,self.settings,{'csv':'fake'},catalog(),self.root,[self.item])
    def client(self,response=SUCCESS,fail_get=False,fail_post=False):
        owner=self
        class Client:
            posts=0
            def get(self):
                if fail_get:raise ConnectionError('GET failed')
                return http.Response(200,form_fixture())
            def post(self,body,content_type):
                self.posts+=1
                if self.posts>1:raise AssertionError('Must not replay POST')
                message=BytesParser(policy=default).parsebytes(b'Content-Type: '+content_type.encode()+b'\r\n\r\n'+body)
                fields={p.get_param('name',header='content-disposition'):p.get_payload(decode=True) for p in message.iter_parts()}
                owner.assertEqual(fields['disclaimer_accepted'],b'1');owner.assertEqual(fields['captcha'],b'5')
                owner.assertEqual(sha(fields['profile_photo']),owner.item['photo']['sha256'])
                if fail_post:raise TimeoutError('Unknown remote outcome')
                return http.Response(200,response)
        return Client()
    def run_pilot(self,client):
        owner=self
        @contextmanager
        def snapshot(*args,**kwargs):yield [owner.item['data']],[],{},{}
        with patch.object(http,'load',return_value=self.settings),patch.object(http,'input_snapshot',snapshot):
            return http.run(self.root/'profile.json',client)
    def test_success_commits_receipt_and_rerun_skips_without_post(self):
        client=self.client();result=self.run_pilot(client)
        self.assertEqual(result['state'],'registered');self.assertEqual(client.posts,1)
        with opened(self.root/'jobs.sqlite3') as db:
            job=jobs(db,'Lahore')[0];self.assertTrue(check_receipt(job['receipt']))
            attempt=db.execute('SELECT * FROM attempts').fetchone();self.assertTrue(attempt['intent_at']);self.assertTrue(attempt['dispatched_at'])
            self.assertNotIn('synthetic-csrf',Path(job['receipt']).with_name('response.html').read_text())
        self.assertEqual(self.run_pilot(client)['status'],'nothing_ready');self.assertEqual(client.posts,1)
    def test_unknown_response_is_uncertain_and_blocks_rerun(self):
        client=self.client(fail_post=True);result=self.run_pilot(client)
        self.assertEqual(result['state'],'uncertain');self.assertEqual(client.posts,1)
        with self.assertRaisesRegex(ValueError,'uncertain'):self.run_pilot(client)
        self.assertEqual(client.posts,1)
    def test_explicit_captcha_rejection_is_saved_without_retry(self):
        client=self.client(response=REJECTED);result=self.run_pilot(client)
        self.assertEqual(result['state'],'rejected');self.assertEqual(client.posts,1)
    def test_failed_get_keeps_ready_and_creates_no_attempt(self):
        client=self.client(fail_get=True)
        with self.assertRaises(ConnectionError):self.run_pilot(client)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual(jobs(db,'Lahore')[0]['state'],'ready');self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
        self.assertEqual(client.posts,0)
    def test_tampered_options_photo_and_unknown_fields_prevent_post(self):
        for kind in ['photo','options','fields','executable_catalog']:
            with self.subTest(kind=kind):
                item=deepcopy(self.item);html=form_fixture()
                if kind=='options':item['option_ids']['UC']='wrong'
                if kind=='photo':
                    with self.assertRaisesRegex(ValueError,'Photo'):http.multipart({'captcha':'5'},self.photo,'wrong')
                    continue
                if kind=='fields':html=html.replace('<input id="captcha"','<input name="unexpected" value="yes"><input id="captcha"')
                if kind=='executable_catalog':html=html.replace('window.participationCatalog = {','window.participationCatalog = fetch("unsafe") || {')
                with self.assertRaises(ValueError):http.prepare_form(html,self.settings,item)
    def test_419_response_cannot_become_success(self):
        class Client:
            posts=0
            def get(self):return http.Response(200,form_fixture())
            def post(self,*args):self.posts+=1;return http.Response(419,'Page expired')
        result=self.run_pilot(Client());self.assertEqual(result['state'],'uncertain')

    def test_explicit_roll_never_falls_back_to_a_different_ready_student(self):
        second=deepcopy(self.item);second['roll']='001235';second['data']['BForm']='0000001234572'
        with opened(self.root/'jobs.sqlite3') as db:
            import_batch(db,self.settings,{'csv':'two'},catalog(),self.root,[self.item,second])
        @contextmanager
        def snapshot(*args,**kwargs):yield [self.item['data'],second['data']],[],{},{}
        client=self.client()
        with patch.object(http,'load',return_value=self.settings),patch.object(http,'input_snapshot',snapshot):
            result=http.run(self.root/'profile.json',client,roll='001235')
            self.assertEqual(result['state'],'registered')
            self.assertEqual(http.run(self.root/'profile.json',client,roll='001235')['post_attempts'],0)
            self.assertEqual(http.run(self.root/'profile.json',client,roll='missing')['post_attempts'],0)
        self.assertEqual(client.posts,1)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual([(r['roll'],r['state']) for r in jobs(db,'Lahore')],[('001234','ready'),('001235','registered')])
