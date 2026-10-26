"""Real HTTPX async workers against an intercepted synthetic portal; no live POSTs."""
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import csv
from email.parser import BytesParser
from email.policy import default
import io,json,re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,AsyncMock

import httpx
from PIL import Image
from rich.console import Console

from bise_normalize import COLUMNS
from youthgames_register import http_async as runner
from youthgames_register.config import sha
from youthgames_register.store import opened,jobs,check_receipt
from youthgames_register.domain import classify
from youthgames_register.http_recovery import recover_rate_limited
from youthgames_register.terminal import Dashboard,print_summary
from tests.test_youthgames import data,config,SUCCESS
from tests.test_youthgames_http import form_fixture

THROTTLED = '''<nav><ul><li>Schools (Middle)</li><li>Talk to CM</li></ul></nav>
<form id="registration-form" data-has-server-errors="1">
<div id="registration-errors" class="registration-error-banner" role="alert" tabindex="-1">
<ul><li>Too many registration attempts. Please wait 48 seconds and try again.</li></ul></div>
<div class="invalid-feedback">Category is required.</div>
<ul><li>I certify that all information is true.</li></ul></form>'''


class ServerValidationTests(unittest.TestCase):
    def test_actual_banner_layout_uses_only_explicit_server_errors(self):
        outcome=classify(THROTTLED,200)
        self.assertEqual(outcome['detail'],'Too many registration attempts. Please wait 48 seconds and try again.')
        self.assertTrue(outcome['rate_limited']);self.assertEqual(outcome['retry_after_seconds'],48)
        self.assertFalse(outcome['retryable_captcha'])
        # Current portal puts the message in a title div, without a list.
        title=THROTTLED.replace('<ul><li>Too many', '<div class="registration-error-banner__title">Too many').replace('try again.</li></ul></div>', 'try again.</div></div>')
        self.assertEqual(classify(title,200)['detail'],outcome['detail'])

    def test_hidden_feedback_and_menu_are_never_server_errors(self):
        outcome=classify(THROTTLED.replace('id="registration-errors"','id="unrelated"'),200)
        self.assertEqual(outcome['detail'],'Portal returned explicit form validation errors')
        self.assertFalse(outcome['rate_limited'])

    def test_mixed_validation_or_duplicate_or_tracking_never_retries_as_throttle(self):
        for extra in ['<li>Mobile phone is invalid.</li>','<li>This CNIC has already registered.</li>']:
            outcome=classify(THROTTLED.replace('seconds and try again.</li>','seconds and try again.</li>'+extra),200)
            self.assertFalse(outcome['rate_limited'])
        self.assertFalse(classify(THROTTLED+'<span id="registration-success-tracking-id">TEST-123</span>',200)['rate_limited'])

    def test_duplicate_and_captcha_inside_server_summary_keep_existing_classification(self):
        for text,state,retry in [('This CNIC has already registered.','already_registered_external',False),('Invalid captcha answer.','rejected',True)]:
            html=THROTTLED.replace('Too many registration attempts. Please wait 48 seconds and try again.',text)
            outcome=classify(html,200)
            self.assertEqual(outcome['state'],state);self.assertEqual(outcome['retryable_captcha'],retry)


class ConcurrentHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.settings=config();self.settings['state'].update(database=str(self.root/'jobs.sqlite3'),output=str(self.root/'output'),write_reports=False)
        image=self.root/'image.jpeg';Image.new('RGB',(3,3),'white').save(image)
        self.settings['photos'].update(shared_file=str(image),expected_sha256=sha(image.read_bytes()))
        self.profile=self.root/'profile.json';self.posts=[];self.gets=0;self.active=self.peak=0
        self.overlap=1;self.barrier=asyncio.Event();self.cancel_at_post=False;self.in_post=asyncio.Event()
        self.control=None;self.fail_one=False;self.pre_stop=False;self.throttle_once=False;self.fail_get=False
        self.clients=[];self.post_times=[];self.post_refusals=0;self.always_refuse=False
        self.get_delay=0;self.post_delay=.01;self.get_active=self.get_peak=0
        self.cooldown_seen=asyncio.Event();self.dispatch_stop_flags=[]
        self.write_input(40)

    def write_input(self,count):
        folder=self.root/'input';(folder/'Data').mkdir(parents=True,exist_ok=True)
        origins=[];rows=[]
        for index in range(count):
            row=data();row.update(FullName='Synthetic Student',BForm=f'{index:012d}2',UC=f'UC No. {255+index%20}, Test UC {255+index%20}',Tehsil='Raiwind')
            rows.append(row);origins.append({'roll':f'{index+1:06d}','data':row,'origins':{},
                'administrative_allocation':{'scheme':'Synthetic','uc_number':255+index%20,'residence_inferred':False}})
        with (folder/'Ready.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=COLUMNS);writer.writeheader();writer.writerows(rows)
        (folder/'Data/provenance.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in origins))
        hashes={name:sha((folder/name).read_bytes()) for name in ('Ready.csv','Data/provenance.jsonl')}
        (folder/'Data/manifest.json').write_text(json.dumps({'files':hashes}))
        self.settings['input'].update(csv=str(folder/'Ready.csv'),provenance=str(folder/'Data/provenance.jsonl'),manifest=str(folder/'Data/manifest.json'),expected_csv_sha256=hashes['Ready.csv'])
        self.settings['confirmation']['input_sha256']=hashes['Ready.csv'];self.profile.write_text(json.dumps(self.settings))
        html=form_fixture().replace('Shalimar','Raiwind').replace('"77"','"76"')
        options=[{'id':str(2330+n),'name':f'UC No. {255+n}, Test UC {255+n}'} for n in range(20)]
        self.html=re.sub(r'const ucsByDistrict = .*?;</script>','const ucsByDistrict = '+json.dumps({'21':options})+';</script>',html)

    async def handler(self,request):
        if request.method=='GET':
            self.gets+=1
            if self.gets>1 and self.fail_get:return httpx.Response(503,headers={'Retry-After':'0'})
            if self.gets>1 and self.throttle_once:
                self.throttle_once=False;return httpx.Response(429,headers={'Retry-After':'0'})
            if self.gets>1 and self.pre_stop:self.control.stop.set()
            token=f'synthetic-{self.gets}'
            if self.gets>1:
                self.get_active+=1;self.get_peak=max(self.get_peak,self.get_active)
                try:await asyncio.sleep(self.get_delay)
                finally:self.get_active-=1
            return httpx.Response(200,text=self.html.replace('synthetic-csrf',token),headers={'Set-Cookie':f'session={token}; Path=/; Secure'})
        body=await request.aread();message=BytesParser(policy=default).parsebytes(b'Content-Type: '+request.headers['content-type'].encode()+b'\r\n\r\n'+body)
        fields={p.get_param('name',header='content-disposition'):(p.get_payload(decode=True) or b'') for p in message.iter_parts()}
        self.assertEqual(request.headers['cookie'],'session='+fields['_token'].decode())
        self.posts.append(fields);self.post_times.append(asyncio.get_running_loop().time());self.active+=1;self.peak=max(self.peak,self.active);self.in_post.set()
        if self.active>=self.overlap:self.barrier.set()
        try:
            if self.cancel_at_post:await asyncio.Event().wait()
            await asyncio.wait_for(self.barrier.wait(),timeout=5)
            await asyncio.sleep(self.post_delay)
            if self.post_refusals or self.always_refuse:
                self.post_refusals=max(0,self.post_refusals-1)
                return httpx.Response(200,text=THROTTLED)
            if self.fail_one and fields['cnic']==b'00000-0000000-2':raise httpx.ReadTimeout('Lost response',request=request)
            tracking='TEST-HTTP-'+fields['cnic'].decode().replace('-','')
            return httpx.Response(200,text=SUCCESS.replace('TEST-YG-123',tracking))
        finally:self.active-=1

    def factory(self,worker):
        self.clients.append(worker)
        return runner.Client(transport=httpx.MockTransport(self.handler))

    async def run_batch(self,workers=18,limit=0,control=None,auto_recover=False):
        output=io.StringIO();console=Console(file=output,force_terminal=False,width=120)
        control=control or runner.Control(submission_interval=0,prepare_ahead=workers,max_in_flight=workers)
        with Dashboard('Synthetic',workers,enabled=False,console=console) as ui:
            event=ui.event
            def observe(phase,**values):
                if phase=='submitting':self.dispatch_stop_flags.append(control.stop.is_set())
                if phase=='cooldown':self.cooldown_seen.set()
                event(phase,**values)
            ui.event=observe
            result=await runner.run(self.profile,workers,limit,ui,self.factory,control,install_signals=False,auto_recover=auto_recover)
        return result

    async def test_parallel_gets_and_overlapping_posts_have_separate_bounds_and_shared_rate(self):
        self.write_input(8);self.get_delay=.03;self.post_delay=.14
        control=runner.Control(submission_interval=.04,prepare_ahead=4,max_in_flight=2)
        result=await self.run_batch(18,control=control)
        self.assertEqual(result['registered_now'],8)
        self.assertGreaterEqual(self.get_peak,3);self.assertLessEqual(self.get_peak,4)
        self.assertEqual(self.peak,2);self.assertEqual(result['peak_in_flight'],2)
        self.assertTrue(all(b-a>=.035 for a,b in zip(self.post_times,self.post_times[1:])))
        self.assertEqual(len({p['_token'] for p in self.posts}),8)
        self.assertEqual(len({p['cnic'] for p in self.posts}),8)

    async def test_preparation_continues_while_previous_post_is_awaiting_response(self):
        self.write_input(3);self.get_delay=.01;self.cancel_at_post=True
        control=runner.Control(submission_interval=.03,prepare_ahead=2,max_in_flight=1)
        task=asyncio.create_task(self.run_batch(18,control=control))
        await asyncio.wait_for(self.in_post.wait(),5)
        for _ in range(100):
            if self.gets>=4:break
            await asyncio.sleep(.002)
        self.assertGreaterEqual(self.gets,4)  # preflight + all three applicant GETs
        self.assertEqual(len(self.posts),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        with opened(self.root/'jobs.sqlite3') as db:
            states=[r['state'] for r in jobs(db,'Lahore')]
            self.assertEqual(states.count('uncertain'),1);self.assertEqual(states.count('ready'),2)

    async def test_prepared_form_age_is_checked_at_dispatch_and_refresh_keeps_one_attempt(self):
        self.write_input(2)
        control=runner.Control(submission_interval=.08,prepare_ahead=2,max_in_flight=2,form_max_age=.05)
        result=await self.run_batch(18,control=control)
        self.assertEqual(result['registered_now'],2);self.assertGreaterEqual(result['refreshed_forms'],1)
        self.assertEqual(len(self.posts),2);self.assertGreaterEqual(self.gets,4)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],2)
            for saved in jobs(db,'Lahore'):
                proof=json.loads((Path(saved['receipt']).parent/'http-request-proof.json').read_text())
                self.assertLess(proof['form_age_seconds'],.05)

    async def test_cooldown_invalidates_all_prepared_sessions_before_any_retry(self):
        self.write_input(3);self.post_refusals=1
        control=runner.Control(submission_interval=.05,prepare_ahead=3,max_in_flight=2)
        delay=runner.Control.delay
        def accelerated(instance,seconds):delay(instance,min(seconds,.025))
        with patch.object(runner.Control,'delay',accelerated):
            result=await self.run_batch(18,control=control)
        self.assertEqual(result['registered_now'],3);self.assertEqual(result['throttle_refusals'],1)
        self.assertGreaterEqual(result['refreshed_forms'],2)
        self.assertTrue(all(p['_token'] not in (b'synthetic-3',b'synthetic-4') for p in self.posts[1:]))

    async def test_cancellation_during_parallel_preparation_dispatches_no_posts(self):
        self.write_input(6);self.get_delay=10
        control=runner.Control(submission_interval=.05,prepare_ahead=3,max_in_flight=2)
        task=asyncio.create_task(self.run_batch(18,control=control))
        for _ in range(100):
            if self.get_active==3:break
            await asyncio.sleep(.002)
        self.assertEqual(self.get_active,3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertTrue(all(r['state']=='ready' for r in jobs(db,'Lahore')))
            self.assertEqual(db.execute('SELECT count(*) FROM attempts WHERE intent_at IS NOT NULL').fetchone()[0],0)
        self.assertFalse(self.posts)

    async def test_invalid_pipeline_limits_fail_before_preflight(self):
        for values in ({'prepare_ahead':0},{'prepare_ahead':9},{'prepare_ahead':True},{'max_in_flight':0},{'max_in_flight':37}):
            with self.assertRaises(ValueError):await runner.run(self.profile,client_factory=self.factory,**values)
        self.assertFalse(self.clients)
        self.assertFalse((self.root/'jobs.sqlite3').exists())

    async def test_catalog_hash_work_is_constant_for_large_input_preparation(self):
        from youthgames_register import http_pilot
        self.write_input(200)
        with patch.object(http_pilot,'signature',wraps=http_pilot.signature) as digest:
            result=await self.run_batch(1,limit=1)
        # One shared input hash, one first-row contract check, one fresh POST
        # form check. Scaling the input must not rehash the full UC catalog.
        catalog_calls=[call for call in digest.call_args_list if isinstance(call.args[0],dict) and 'ucs_by_district' in call.args[0]]
        self.assertLessEqual(len(catalog_calls),3)
        self.assertEqual(result['registered_now'],1)
        with opened(self.root/'jobs.sqlite3') as db:
            digests={json.loads(r['payload'])['catalog_hash'] for r in jobs(db,'Lahore')}
            self.assertEqual(len(digests),1)

    async def test_repeated_age_expiry_leaves_ready_without_any_submission_intent(self):
        self.write_input(1);self.get_delay=.025
        control=runner.Control(submission_interval=0,form_max_age=.01)
        result=await self.run_batch(1,control=control)
        self.assertEqual(result['status'],'stopped');self.assertEqual(result['outcomes'],{'ready':1})
        self.assertFalse(self.posts)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertIsNone(db.execute('SELECT intent_at FROM attempts').fetchone()[0])

    async def test_explicit_throttle_waits_shared_cooldown_and_retries_with_fresh_session(self):
        self.write_input(1);self.post_refusals=1
        # Accelerate the clock wait only; the recovery receipt still records the
        # server's actual deadline and remains hash verifiable.
        with patch.object(runner.Control,'delay',autospec=True) as delay:
            result=await self.run_batch(18)
        self.assertEqual(result['registered_now'],1);self.assertEqual(result['processed'],1)
        self.assertEqual(result['throttle_refusals'],1);self.assertEqual(len(self.posts),2)
        self.assertNotEqual(self.posts[0]['_token'],self.posts[1]['_token'])
        self.assertGreaterEqual(max(call.args[1] for call in delay.call_args_list),49)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual([r[0] for r in db.execute('SELECT outcome FROM attempts ORDER BY id')],['rejected','registered'])
            recovery=db.execute('SELECT * FROM submission_recoveries').fetchone()
            self.assertTrue(check_receipt(recovery['evidence']))
            self.assertIn('Too many registration attempts',json.loads(Path(recovery['evidence']).read_text())['outcome']['detail'])
            self.assertEqual(recover_rate_limited(db,self.settings),[])
        before=(self.gets,len(self.posts))
        await self.run_batch(18)
        self.assertEqual((self.gets,len(self.posts)),before)

    async def test_local_recovery_error_never_overwrites_committed_server_receipt(self):
        self.write_input(1);self.post_refusals=1
        original=runner.recover_rate_limited
        def fail_after_commit(db,config,job_ids=None):
            if job_ids:raise OSError('Synthetic audit failure')
            return original(db,config)
        with patch.object(runner,'recover_rate_limited',side_effect=fail_after_commit):
            result=await self.run_batch(1)
        self.assertIn('Local post-processing',result['blocked'])
        with opened(self.root/'jobs.sqlite3') as db:
            saved=jobs(db,'Lahore')[0]
            self.assertEqual(saved['state'],'rejected');self.assertTrue(check_receipt(saved['receipt']))
            receipt=json.loads(Path(saved['receipt']).read_text())
            self.assertTrue(receipt['outcome']['rate_limited']);self.assertEqual(receipt['http_status'],200)

    async def test_other_validation_errors_stop_without_retry_or_recovery(self):
        self.write_input(3);self.post_refusals=1
        rejection=THROTTLED.replace('Too many registration attempts. Please wait 48 seconds and try again.','Mobile phone is invalid.')
        with patch(__name__+'.THROTTLED',rejection):
            result=await self.run_batch(18,control=runner.Control(submission_interval=.05))
        self.assertEqual(len(self.posts),1);self.assertEqual(result['outcomes']['rejected'],1)
        self.assertEqual(result['status'],'stopped');self.assertIn('rejected',result['blocked'])
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM submission_recoveries').fetchone()[0],0)

    async def test_auto_recovery_quarantines_one_explicit_rejection_and_continues_rest(self):
        self.write_input(4);self.post_refusals=1
        rejection=THROTTLED.replace('Too many registration attempts. Please wait 48 seconds and try again.','Mobile phone is invalid.')
        with patch(__name__+'.THROTTLED',rejection):
            result=await self.run_batch(1,auto_recover=True)
        self.assertEqual(result['status'],'complete_with_issues');self.assertNotIn('blocked',result)
        self.assertEqual(result['registered_now'],3);self.assertEqual(result['outcomes']['rejected'],1)
        self.assertEqual(len(self.posts),4);self.assertEqual(result['needs_attention'],1)
        with Path(result['attention_report']).open(encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream))
        self.assertEqual(len(rows),1);self.assertIn('Mobile phone is invalid.',rows[0]['reason'])
        before=len(self.posts)
        second=await self.run_batch(1,auto_recover=True)
        self.assertEqual(len(self.posts),before);self.assertEqual(second['skipped_registered'],3)
        self.assertEqual(second['needs_attention'],1)

    async def test_bad_local_name_is_quarantined_without_attempt_and_valid_rows_continue(self):
        original=data
        def named():
            row=original();row['FatherName']='Father 123' if not getattr(named,'called',False) else 'Synthetic Father'
            named.called=True;return row
        with patch(__name__+'.data',side_effect=named):self.write_input(3)
        result=await self.run_batch(1,auto_recover=True)
        self.assertEqual(result['registered_now'],2);self.assertEqual(result['needs_attention'],1)
        self.assertEqual(len(self.posts),2)
        with opened(self.root/'jobs.sqlite3') as db:
            bad=db.execute("SELECT id FROM jobs WHERE state='blocked_input'").fetchone()
            self.assertEqual(db.execute('SELECT count(*) FROM attempts WHERE job=?',(bad['id'],)).fetchone()[0],0)

    async def test_separator_fix_is_sent_and_audited_without_changing_pinned_source(self):
        original=data
        def named():
            row=original();row['FatherName']='AHSAN -UL-HAQ';return row
        with patch(__name__+'.data',side_effect=named):self.write_input(1)
        result=await self.run_batch(1,auto_recover=True)
        self.assertEqual(result['registered_now'],1)
        self.assertEqual(self.posts[0]['last_name'],b'AHSAN-UL-HAQ')
        with opened(self.root/'jobs.sqlite3') as db:
            job=jobs(db,'Lahore')[0]
            self.assertEqual(json.loads(job['payload'])['data']['FatherName'],'AHSAN -UL-HAQ')
            proof=json.loads(Path(job['receipt']).with_name('http-request-proof.json').read_text())
            self.assertEqual(proof['name_formatting']['FatherName']['to'],'AHSAN-UL-HAQ')

    async def test_all_workers_pause_on_throttle_and_cooldown_is_durable(self):
        self.write_input(3);self.post_refusals=1;control=runner.Control(submission_interval=.05,max_in_flight=1)
        task=asyncio.create_task(self.run_batch(18,control=control))
        await asyncio.wait_for(self.cooldown_seen.wait(),5)
        await asyncio.sleep(.02)
        self.assertEqual(len(self.posts),1)
        self.assertGreater(control.deadline-asyncio.get_running_loop().time(),49)
        control.stop.set();await asyncio.wait_for(task,5)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertGreater(runner.cooldown_remaining(db,runner.BASE,self.settings['campaign']),49)
            self.assertTrue(all(r['state']=='ready' for r in jobs(db,'Lahore')))

    async def test_repeated_throttle_is_bounded_and_keeps_pending_student_ready(self):
        self.write_input(1);self.always_refuse=True
        with patch.object(runner.Control,'delay'):
            result=await self.run_batch(18)
        self.assertEqual(len(self.posts),3);self.assertEqual(result['registered_now'],0)
        self.assertEqual(result['status'],'stopped');self.assertIn('throttling',result['blocked'])
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual(jobs(db,'Lahore')[0]['state'],'ready')
            self.assertEqual(db.execute('SELECT count(*) FROM submission_recoveries').fetchone()[0],3)

    async def test_auto_recovery_survives_more_than_two_throttle_refusals_without_replaying_success(self):
        self.write_input(1);self.post_refusals=4
        with patch.object(runner.Control,'wait',new_callable=AsyncMock,return_value=True),patch.object(runner.Control,'delay'):
            result=await self.run_batch(1,auto_recover=True)
        self.assertEqual(result['status'],'complete');self.assertTrue(result['auto_recover'])
        self.assertEqual(result['throttle_refusals'],4);self.assertEqual(result['registered_now'],1)
        self.assertEqual(len(self.posts),5);self.assertEqual(len({p['_token'] for p in self.posts}),5)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual([r[0] for r in db.execute('SELECT outcome FROM attempts ORDER BY id')],['rejected']*4+['registered'])
            self.assertEqual(db.execute('SELECT count(*) FROM submission_recoveries').fetchone()[0],4)
            self.assertGreaterEqual(db.execute('SELECT dispatch_interval FROM portal_pacing').fetchone()[0],7.5)
            self.assertTrue(check_receipt(jobs(db,'Lahore')[0]['receipt']))
        before=(self.gets,len(self.posts))
        repeated=await self.run_batch(1,auto_recover=True)
        self.assertEqual(repeated['processed'],0);self.assertEqual((self.gets,len(self.posts)),before)

    async def test_temporary_worker_get_outage_requeues_only_an_undispatched_applicant(self):
        self.write_input(1)
        original=self.handler
        async def outage(request):
            self.fail_get=1 <= self.gets <= 3
            return await original(request)
        self.handler=outage
        with patch.object(runner.Control,'wait',new_callable=AsyncMock,return_value=True),patch.object(runner.Control,'delay'):
            result=await self.run_batch(1,auto_recover=True)
        self.assertEqual(result['status'],'complete');self.assertEqual(result['registered_now'],1)
        self.assertEqual(result['safe_form_retries'],1);self.assertEqual(len(self.posts),1)
        with opened(self.root/'jobs.sqlite3') as db:
            attempts=db.execute('SELECT * FROM attempts ORDER BY id').fetchall()
            self.assertEqual(len(attempts),2);self.assertEqual(attempts[0]['outcome'],'ready')
            self.assertIsNone(attempts[0]['intent_at']);self.assertIsNone(attempts[0]['dispatched_at'])
            self.assertEqual(attempts[1]['outcome'],'registered')

    async def test_preflight_get_outage_keeps_retrying_without_claiming_applicants(self):
        self.write_input(1)
        original=self.handler
        async def outage(request):
            if request.method=='GET' and self.gets < 5:
                self.gets+=1
                return httpx.Response(503)
            return await original(request)
        self.handler=outage
        with patch.object(runner.Control,'wait',new_callable=AsyncMock,return_value=True),patch.object(runner.Control,'delay'):
            result=await self.run_batch(1,auto_recover=True)
        self.assertEqual(result['status'],'complete');self.assertEqual(len(self.posts),1)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],1)

    async def test_ambiguous_post_still_stops_in_auto_recovery_mode(self):
        self.write_input(1);self.fail_one=True
        result=await self.run_batch(1,auto_recover=True)
        self.assertEqual(result['outcomes']['uncertain'],1);self.assertEqual(len(self.posts),1)
        self.assertEqual(result['status'],'stopped')
        again=await self.run_batch(1,auto_recover=True)
        self.assertEqual(again['blocked'],'uncertain_submission_requires_reconciliation')
        self.assertEqual(len(self.posts),1)

    async def test_ctrl_c_during_automatic_backoff_keeps_rejected_applicant_ready(self):
        self.write_input(1);self.always_refuse=True;control=runner.Control(submission_interval=.05)
        task=asyncio.create_task(self.run_batch(1,control=control,auto_recover=True))
        await asyncio.wait_for(self.cooldown_seen.wait(),5)
        control.stop.set()
        result=await asyncio.wait_for(task,5)
        self.assertEqual(result['status'],'stopped');self.assertEqual(len(self.posts),1)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual(jobs(db,'Lahore')[0]['state'],'ready')
            self.assertTrue(db.execute('SELECT not_before FROM portal_pacing').fetchone()[0])

    async def test_stopping_preflight_backoff_writes_summary_without_any_claim(self):
        self.write_input(1);control=runner.Control(submission_interval=.05)
        async def unavailable(request):
            return httpx.Response(503)
        self.handler=unavailable
        task=asyncio.create_task(self.run_batch(1,control=control,auto_recover=True))
        await asyncio.wait_for(self.cooldown_seen.wait(),5)
        control.stop.set()
        result=await asyncio.wait_for(task,5)
        self.assertEqual(result['blocked'],'operator_stopped_before_submission')
        self.assertTrue(Path(result['report']).is_file());self.assertFalse(self.posts)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)

    async def test_shared_spacing_applies_to_all_workers_and_survives_rerun(self):
        self.write_input(4)
        await self.run_batch(18,limit=3,control=runner.Control(submission_interval=.07))
        await self.run_batch(18,limit=1,control=runner.Control(submission_interval=.07))
        self.assertEqual(len(self.post_times),4)
        self.assertTrue(all(b-a>=.065 for a,b in zip(self.post_times,self.post_times[1:])))

    async def test_waiting_workers_cancel_before_post_without_replaying_saved_success(self):
        self.write_input(3);control=runner.Control(submission_interval=60)
        task=asyncio.create_task(self.run_batch(18,control=control))
        await asyncio.wait_for(self.in_post.wait(),5)
        await asyncio.sleep(.04);control.stop.set()
        result=await asyncio.wait_for(task,5)
        self.assertEqual(len(self.posts),1);self.assertEqual(result['registered_now'],1)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual([r['state'] for r in jobs(db,'Lahore')],['registered','ready','ready'])

    async def test_saved_throttle_refusal_is_recoverable_idempotently_without_post(self):
        self.write_input(1);self.always_refuse=True
        with patch.object(runner.Control,'delay'):
            await self.run_batch(1)
        with opened(self.root/'jobs.sqlite3') as db:
            # Simulate the previous classifier's rejected state using its original
            # receipt. Only that same dispatched request can be reconciled.
            prior=db.execute('SELECT * FROM attempts ORDER BY id DESC LIMIT 1').fetchone()
            db.execute('DELETE FROM submission_recoveries WHERE attempt=?',(prior['id'],))
            db.execute("UPDATE jobs SET state='rejected',receipt=?",(prior['evidence'],));db.commit()
            before=len(self.posts)
            self.assertEqual(len(recover_rate_limited(db,self.settings)),1)
            self.assertEqual(recover_rate_limited(db,self.settings),[])
            self.assertEqual(len(self.posts),before)
            self.assertTrue(check_receipt(jobs(db,'Lahore')[0]['receipt']))

    async def test_damaged_or_mismatched_throttle_receipts_never_reset_jobs(self):
        self.write_input(1);self.always_refuse=True
        with patch.object(runner.Control,'delay'):
            await self.run_batch(1)
        with opened(self.root/'jobs.sqlite3') as db:
            prior=db.execute('SELECT * FROM attempts ORDER BY id DESC LIMIT 1').fetchone()
            db.execute('DELETE FROM submission_recoveries WHERE attempt=?',(prior['id'],))
            db.execute("UPDATE jobs SET state='rejected',receipt=?",(prior['evidence'],));db.commit()
            original=Path(prior['evidence']);saved=json.loads(original.read_text())
            saved['job']=999;original.write_text(json.dumps(saved))
            self.assertEqual(recover_rate_limited(db,self.settings),[])
            saved['job']=1;original.write_text(json.dumps(saved))
            Path(saved['files']['response.html']['path']).write_text('tampered')
            self.assertEqual(recover_rate_limited(db,self.settings),[])
            self.assertEqual(jobs(db,'Lahore')[0]['state'],'rejected')

    async def test_eighteen_and_thirty_six_real_httpx_workers_overlap_with_one_post_each(self):
        for count in (18,36):
            with self.subTest(workers=count):
                # Separate cases retain successful identities and select new rolls.
                self.overlap=count;self.barrier=asyncio.Event();before=len(self.posts)
                if count==36:self.write_input(60)
                result=await self.run_batch(count,limit=count)
                self.assertEqual(result['registered_now'],count)
                self.assertGreaterEqual(result['peak_active'],count)
                self.assertEqual(len(self.posts)-before,count)
                self.assertEqual(len({p['cnic'] for p in self.posts}),len(self.posts))
        with opened(self.root/'jobs.sqlite3') as db:
            saved=db.execute("SELECT * FROM jobs WHERE state='registered'").fetchall()
            self.assertEqual(len(saved),54)
            self.assertTrue(all(row['tracking'] and check_receipt(row['receipt']) for row in saved))
            self.assertEqual(db.execute('SELECT count(DISTINCT job),count(*) FROM attempts').fetchone()[:],(54,54))

    async def test_global_limit_and_rerun_skip_all_saved_without_network(self):
        self.write_input(18);self.overlap=18
        first=await self.run_batch(18,limit=18);before=(self.gets,len(self.posts))
        repeated=await self.run_batch(18)
        self.assertEqual(first['processed'],18);self.assertEqual(repeated['processed'],0)
        self.assertEqual((self.gets,len(self.posts)),before)

    async def test_lost_post_response_stops_new_claims_keeps_inflight_receipts_and_blocks_rerun(self):
        self.overlap=18;self.fail_one=True
        result=await self.run_batch(18)
        self.assertEqual(result['outcomes']['uncertain'],1)
        # Responses can finish in a different order under load. Work dispatched
        # before the lost-response failure is observed is legitimately in flight.
        self.assertEqual(result['outcomes']['registered'],len(self.posts)-1)
        self.assertTrue(set(result['outcomes']) <= {'uncertain','registered','ready'})
        dispatched=len(self.posts)
        self.assertGreaterEqual(dispatched,18)
        self.assertFalse(any(self.dispatch_stop_flags))
        again=await self.run_batch(18)
        self.assertEqual(again['blocked'],'uncertain_submission_requires_reconciliation')
        self.assertEqual(len(self.posts),dispatched)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE state='ready'").fetchone()[0],40-dispatched)
            self.assertTrue(check_receipt(jobs(db,'Lahore',('uncertain',))[0]['receipt']))

    async def test_stop_before_intent_keeps_ready_and_dispatches_nothing(self):
        self.control=runner.Control();self.pre_stop=True
        result=await self.run_batch(1,control=self.control)
        self.assertEqual(result['outcomes'],{'ready':1});self.assertFalse(self.posts)
        with opened(self.root/'jobs.sqlite3') as db:
            attempt=db.execute('SELECT * FROM attempts').fetchone()
            self.assertIsNone(attempt['intent_at']);self.assertIsNone(attempt['dispatched_at'])

    async def test_cancellation_after_dispatch_preserves_uncertain_before_sqlite_closes(self):
        self.cancel_at_post=True
        task=asyncio.create_task(self.run_batch(1,limit=1))
        await asyncio.wait_for(self.in_post.wait(),timeout=5);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        with opened(self.root/'jobs.sqlite3') as db:
            row=jobs(db,'Lahore',('uncertain',))[0]
            self.assertTrue(check_receipt(row['receipt']))
            self.assertEqual(db.execute('SELECT count(*) FROM attempts WHERE dispatched_at IS NOT NULL').fetchone()[0],1)

    async def test_get_throttling_retries_only_safe_get_and_uses_a_fresh_cookie(self):
        self.throttle_once=True
        result=await self.run_batch(1,limit=1)
        self.assertEqual(result['registered_now'],1);self.assertEqual(len(self.posts),1)
        # A transport recovery invalidates the old preparation generation, so a
        # final fresh cookie/form GET follows the explicit safe GET retry.
        self.assertEqual(self.gets,4)

    async def test_exhausted_get_retries_are_resumable_without_a_post(self):
        self.fail_get=True
        result=await self.run_batch(1,limit=1)
        self.assertEqual(result['outcomes'],{'ready':1});self.assertFalse(self.posts)
        self.assertEqual(self.gets,4)

    async def test_low_disk_stops_before_claiming_any_student(self):
        with patch('youthgames_register.http_async.shutil.disk_usage',return_value=type('Usage',(),{'free':0})()):
            result=await self.run_batch(18)
        self.assertEqual(result['blocked'],'low_disk_space');self.assertEqual(result['processed'],0);self.assertFalse(self.posts)
        with opened(self.root/'jobs.sqlite3') as db:self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)

    async def test_invalid_worker_counts_fail_before_network_or_database_changes(self):
        for count in (0,37,True):
            with self.assertRaises(ValueError):await self.run_batch(count)
        self.assertFalse(self.clients);self.assertFalse((self.root/'jobs.sqlite3').exists())


class TransportAndTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_dashboard_refreshes_without_network_events(self):
        out=io.StringIO();console=Console(file=out,force_terminal=True,width=160,height=40)
        ui=Dashboard('Synthetic live',2,enabled=True,console=console)
        with patch.object(ui,'render',wraps=ui.render) as render:
            with ui:
                ui.event('queue',total=2)
                ui.event('cooldown',seconds=.05,message='Synthetic cooldown')
                baseline=render.call_count
                await asyncio.sleep(.3)
                self.assertGreater(render.call_count,baseline)
                self.assertLess(ui.cooldown_until,asyncio.get_running_loop().time())
        self.assertIn('Synthetic live',out.getvalue())

    async def test_rate_headers_survive_post_redirect_without_retaining_cookie_headers(self):
        async def route(request):
            if request.method=='POST':
                return httpx.Response(302,headers={'location':'/','X-RateLimit-Limit':'20',
                    'X-RateLimit-Remaining':'19','Retry-After':'7','Set-Cookie':'private=value'})
            return httpx.Response(200,text='OK')
        async with runner.Client(transport=httpx.MockTransport(route)) as client:
            response=await client.post(b'body','multipart/form-data')
        self.assertEqual(response.retry_after,'7')
        self.assertEqual(response.rate_headers,{'x-ratelimit-limit':'20','x-ratelimit-remaining':'19','retry-after':'7'})

    async def test_dashboard_reports_pipeline_activity_and_rate_excluding_startup(self):
        clock=[1000.0];out=io.StringIO();console=Console(file=out,width=220,force_terminal=False)
        with patch('youthgames_register.terminal.time.monotonic',side_effect=lambda:clock[0]):
            ui=Dashboard('Synthetic',18,enabled=False,console=console)
            ui.event('pipeline',prepare_ahead=4,max_in_flight=4,submission_interval_seconds=5,form_max_age_seconds=30)
            clock[0]=1120;ui.event('queue',total=100)
            for instant in (1121,1126,1131):
                clock[0]=instant;ui.event('result',state='registered',worker=1,tracking='TEST')
            ui.event('fetching_form',worker=2);ui.event('prepared',worker=3);ui.event('awaiting_response',worker=4)
            clock[0]=1135
            self.assertAlmostEqual(ui.recent_rate(),12)
            console.print(ui.render())
            text=out.getvalue()
            self.assertIn('Preparing 1',text);self.assertIn('Prepared 1',text);self.assertIn('Awaiting response 1',text)
            self.assertIn('Startup 120s',text);self.assertIn('12.0 saved/min',text)
            clock[0]=1192
            self.assertEqual(ui.recent_rate(),0)

    async def test_terminal_exposes_cooldown_reason_and_keeps_deferrals_out_of_failure_progress(self):
        out=io.StringIO();console=Console(file=out,force_terminal=False,width=180)
        with Dashboard('Synthetic',18,enabled=False,console=console) as ui:
            ui.event('queue',total=3)
            ui.event('cooldown',seconds=50,message='Too many registration attempts. Please wait 48 seconds and try again.')
            ui.event('throttle_retry',worker=1,uc=255,roll='000001',state='rejected',detail='Too many registration attempts. Please wait 48 seconds and try again.')
            ui.event('result',worker=2,uc=256,roll='000002',state='ready')
            console.print(ui.render())
            self.assertEqual(ui.completed,0);self.assertEqual(ui.throttles,1)
        text=out.getvalue()
        self.assertIn('Too many registration attempts',text);self.assertIn('WAIT 50s',text)
        self.assertIn('0 need attention',text);self.assertIn('1 deferred',text)
        print_summary({'status':'stopped','blocked':'Repeated explicit server throttling'},console)
        self.assertIn('Repeated explicit server throttling',out.getvalue())

    async def test_post_preserving_and_cross_origin_redirects_never_replay(self):
        for code,url in [(307,runner.BASE+'/applicant'),(302,'https://example.com/')]:
            requests=[]
            async def route(request):
                requests.append(request)
                return httpx.Response(code,headers={'location':url})
            async with runner.Client(transport=httpx.MockTransport(route)) as client:
                with self.assertRaises(ValueError):await client.post(b'body','multipart/form-data')
            self.assertEqual(len(requests),1)

    async def test_post_302_follows_with_a_get_and_keeps_session(self):
        methods=[]
        async def route(request):
            methods.append(request.method)
            if request.method=='POST':return httpx.Response(302,headers={'location':'/'})
            return httpx.Response(200,text='Success')
        async with runner.Client(transport=httpx.MockTransport(route)) as client:
            response=await client.post(b'body','multipart/form-data')
        self.assertEqual(methods,['POST','GET']);self.assertEqual(response.status,200)

    async def test_rich_dashboard_and_private_events_are_readable_without_raw_json_dump(self):
        with tempfile.TemporaryDirectory() as temp:
            out=io.StringIO();console=Console(file=out,force_terminal=False,width=130,record=True)
            with Dashboard('Lahore · Raiwind',18,enabled=True,console=console) as ui:
                ui.attach(temp);ui.event('queue',total=100)
                ui.event('submitting',worker=1,uc=255,roll='400200')
                ui.event('result',worker=1,uc=255,roll='400200',state='registered',tracking='TEST-TRACK')
                console.print(ui.render())
            print_summary({'status':'complete','processed':1,'registered_now':1,'workers':18},console)
            text=out.getvalue();self.assertIn('Worker activity',text);self.assertIn('TEST-TRACK',text);self.assertNotIn('"pilot_checks"',text)
            events=[json.loads(line) for line in (Path(temp)/'events.jsonl').read_text().splitlines()]
            self.assertEqual(len(events),3)
            self.assertTrue(all(not ({'cnic','DOB','first_name','_token'}&set(event)) for event in events))

class CliTests(unittest.TestCase):
    def test_bulk_cli_passes_worker_limit_and_keeps_json_explicit(self):
        import sys
        from unittest.mock import AsyncMock
        from bise_allocate.__main__ import main
        result={'status':'complete','processed':0,'registered_now':0,'workers':36}
        with patch.object(sys,'argv',['bise_allocate','http-submit','--config','allocation.json','--workers','36','--limit','100','--dispatch-interval','6','--json']), \
             patch('bise_allocate.app.load',return_value={'scheme':'Synthetic'}), \
             patch('bise_allocate.app.paths',return_value={'registration_profile':Path('/tmp/synthetic.json'),
                 'allocation_database':Path('/tmp/synthetic-allocations.sqlite3')}), \
             patch('bise_allocate.participation.current',return_value=None), \
             patch('bise_allocate.resume.prepare',return_value={'input_reused':True}) as prepare, \
             patch('bise_allocate.app.progress',return_value={'registered':40,'ready_remaining':100,'normalization_blocked':6}), \
             patch('youthgames_register.http_async.run',new_callable=AsyncMock,return_value=result) as run,redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(),0)
        self.assertEqual(run.call_args.kwargs['workers'],36)
        self.assertEqual(run.call_args.kwargs['limit'],100)
        self.assertEqual(run.call_args.kwargs['prepare_ahead'],4)
        self.assertEqual(run.call_args.kwargs['max_in_flight'],4)
        self.assertTrue(run.call_args.kwargs['auto_recover'])
        self.assertEqual(run.call_args.kwargs['dispatch_interval'],6)
        self.assertEqual(json.loads(output.getvalue())['registered'],40)
        prepare.assert_called_once()
        self.assertFalse(prepare.call_args.kwargs['refresh'])

    def test_completed_pilot_default_output_is_compact_and_has_no_payload_dump(self):
        import sys
        from bise_allocate.__main__ import main
        result={'registered':40,'ready_remaining':18879,'normalization_blocked':6,'attempted_now':0,'pilot_passed':True,
                'pilot_checks':[{'private_detail':'synthetic'} for _ in range(20)]}
        with patch.object(sys,'argv',['bise_allocate','http-pilot','--config','allocation.json']), \
             patch('bise_allocate.http.run',return_value=result),redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(),0)
        text=output.getvalue();self.assertIn('Verified',text);self.assertNotIn('pilot_checks',text);self.assertNotIn('private_detail',text)
        self.assertLess(len(text.splitlines()),25)
