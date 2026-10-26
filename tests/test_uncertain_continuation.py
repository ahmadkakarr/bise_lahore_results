"""Transport timeouts must not stop an explicitly opted-in unattended batch."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock,patch

import httpx
from rich.console import Console

from youthgames_register import http_async as single,connections as network
from youthgames_register.store import opened,jobs,check_receipt
from youthgames_register.terminal import Dashboard,print_summary
from tests import test_http_connections as connections_fixture
from tests import test_http_concurrent as fixture

ROUTES=connections_fixture.ROUTES


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=connections_fixture.MultiConnectionTests.asyncSetUp
    write_input=connections_fixture.MultiConnectionTests.write_input
    handler=connections_fixture.MultiConnectionTests.handler
    factory=connections_fixture.MultiConnectionTests.factory
    multi=connections_fixture.MultiConnectionTests.multi

    async def test_persistent_mobile_timeout_is_bounded_and_other_applicants_finish(self):
        self.write_input(8);self.fail_one=True
        delay=single.Control.delay
        with patch.object(single.Control,'delay',lambda c,s:delay(c,min(s,.002))):
            result=await self.multi(workers=12,retry_uncertain=True,uncertain_retries=2)
        self.assertNotIn('blocked',result);self.assertEqual(result['status'],'complete_with_issues')
        self.assertEqual(result['registered_now'],7)
        self.assertEqual(result['outcomes']['uncertain'],1)
        self.assertEqual(result['uncertain_retry_attempts'],2)
        self.assertEqual(result['workers_by_connection'],{'wifi':6,'usb':6})
        self.assertEqual(sum(fields['cnic']==b'00000-0000000-2' for fields in self.posts),3)
        self.assertTrue(all(p['interval_seconds']>=.025 for p in result['connection_pacing'].values()))
        with opened(self.root/'jobs.sqlite3') as db:
            uncertain=[r for r in jobs(db,'Lahore') if r['state']=='uncertain']
            self.assertEqual(len(uncertain),1);job=uncertain[0]
            attempts=db.execute('SELECT * FROM attempts WHERE job=? ORDER BY id',(job['id'],)).fetchall()
            self.assertEqual([r['outcome'] for r in attempts],['uncertain']*3)
            self.assertTrue(all(check_receipt(a['evidence']) for a in attempts))
            keys=db.execute('SELECT count(DISTINCT c.connection_key) FROM attempt_connections c JOIN attempts a ON a.id=c.attempt WHERE a.job=?',(job['id'],)).fetchone()[0]
            self.assertEqual(keys,1)
            for record in db.execute('SELECT original_job FROM uncertain_retries'):
                self.assertEqual(json.loads(record[0])['state'],'uncertain')

    async def test_unknown_then_duplicate_is_terminal_and_next_applicants_continue(self):
        self.write_input(6);self.fail_one=True;original=self.handler
        lost=False
        async def portal(request):
            nonlocal lost
            body=await request.aread()
            duplicate=lost and request.method=='POST' and b'00000-0000000-2' in body
            try:response=await original(request)
            except httpx.ReadTimeout:
                lost=True;self.fail_one=False;raise
            if duplicate:
                return httpx.Response(200,text=fixture.THROTTLED.replace(
                    'Too many registration attempts. Please wait 48 seconds and try again.',
                    'This B-Form / CNIC has already been registered.'))
            return response
        self.handler=portal;delay=single.Control.delay
        with patch.object(single.Control,'delay',lambda c,s:delay(c,min(s,.002))):
            result=await self.multi(workers=12,retry_uncertain=True,uncertain_retries=2)
        self.assertNotIn('blocked',result);self.assertEqual(result['registered_now'],5)
        self.assertEqual(result['already_registered_now'],1)
        before=len(self.posts)
        result=await self.multi(workers=12,retry_uncertain=True,uncertain_retries=2)
        self.assertEqual(result['skipped_registered'],6);self.assertEqual(len(self.posts),before)

    async def test_single_route_also_quarantines_timeout_and_keeps_processing(self):
        self.write_input(4);self.fail_one=True;self.sent_on=[]
        control=single.Control(submission_interval=.01)
        result=await single.run(self.profile,workers=4,client_factory=self.factory,control=control,
            install_signals=False,retry_uncertain=True,uncertain_retries=0)
        self.assertNotIn('blocked',result);self.assertEqual(result['status'],'complete_with_issues')
        self.assertEqual(result['registered_now'],3)

    async def test_partial_post_redirect_trace_survives_failed_get_without_replaying_post(self):
        calls=[]
        async def handler(request):
            calls.append(request.method)
            if request.method=='POST':return httpx.Response(302,headers={'location':'/','x-ratelimit-remaining':'4'})
            raise httpx.ReadTimeout('redirect GET lost',request=request)
        async with single.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.ReadTimeout):await client.post(b'body','text/plain')
            self.assertEqual(client.last_hops[0]['method'],'POST')
            self.assertEqual(client.last_hops[0]['status'],302)
            self.assertEqual(client.request_stage,{'method':'GET','path':'/','phase':'waiting_for_headers'})
            with self.assertRaisesRegex(ValueError,'already sent'):await client.post(b'body','text/plain')
        self.assertEqual(calls,['POST','GET'])

    async def test_uncertain_post_discards_pool_before_next_applicant_without_replaying(self):
        self.write_input(4);self.fail_one=True;calls=[];old_pools=[]
        handler=self.handler
        class TracedClient(single.Client):
            async def reset(client):
                calls.append('RESET');old_pools.append(client.client)
                await super().reset()
            async def get(client):
                self.assertFalse(client.needs_reset,'Failed POST pool must be replaced before a fresh GET')
                calls.append('GET');return await super().get()
            async def post(client,*args):
                calls.append('POST');return await super().post(*args)
        def factory(worker):return TracedClient(transport=httpx.MockTransport(handler))
        control=single.Control(submission_interval=.001)
        result=await single.run(self.profile,workers=1,client_factory=factory,control=control,
            install_signals=False,retry_uncertain=True,uncertain_retries=0)
        self.assertEqual(result['registered_now'],3)
        self.assertEqual(result['outcomes']['uncertain'],1)
        self.assertEqual(calls[:6],['GET','GET','POST','RESET','GET','POST'])
        self.assertEqual(len(self.posts),4)
        self.assertTrue(all(pool.is_closed for pool in old_pools))
        with opened(self.root/'jobs.sqlite3') as db:
            uncertain=[row for row in jobs(db,'Lahore') if row['state']=='uncertain']
            self.assertEqual(len(uncertain),1)
            self.assertTrue(check_receipt(uncertain[0]['receipt']))
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],4)

    async def test_slow_post_redirect_budget_is_180_seconds_while_form_get_remains_60(self):
        def handler(request):
            if request.method=='POST':return httpx.Response(302,headers={'location':'/'})
            return httpx.Response(200,text='synthetic',headers={'set-cookie':'session=synthetic; Path=/; Secure'})
        original=asyncio.timeout
        async with single.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(client.client.timeout.read,90)
            with patch.object(single.asyncio,'timeout',side_effect=original) as timer:
                await client.post(b'body','text/plain')
                self.assertEqual(timer.call_args_list[0].args,(180,))
            with patch.object(single.asyncio,'timeout',side_effect=original) as timer:
                await client.get()
                self.assertEqual(timer.call_args_list[0].args,(60,))


class PresentationTests(unittest.TestCase):
    def test_mobile_and_wifi_have_separate_saved_post_skip_and_worker_counts(self):
        output=io.StringIO();console=Console(file=output,width=180,height=70,force_terminal=False)
        ui=Dashboard('Synthetic',12,enabled=False,console=console)
        ui.event('connections',routes=[{**r.public(),'workers':6} for r in ROUTES])
        ui.event('queue',total=3)
        ui.event('submitting',connection='wifi',worker=1)
        ui.event('result',connection='wifi',worker=1,state='registered',roll='synthetic1')
        ui.event('submitting',connection='usb',worker=2)
        ui.event('result',connection='usb',worker=2,state='already_registered_external',roll='synthetic2')
        ui.event('uncertain_retry',connection='usb',worker=2,retry=1,maximum=2,roll='synthetic3')
        console.print(ui.render())
        rendered=output.getvalue()
        self.assertIn('Mobile USB',rendered);self.assertIn('Wi-Fi',rendered)
        self.assertIn('Connections · this run',rendered)
        self.assertNotIn('wlan-fixture',rendered);self.assertNotIn('usb-fixture',rendered)
        self.assertEqual(ui.connection_counts['wifi']['registered'],1)
        self.assertEqual(ui.connection_counts['usb']['already_registered_external'],1)
        self.assertEqual(ui.connection_counts['usb']['uncertain_retries'],1)
        self.assertEqual(ui.completed,2)
        print_summary({'status':'complete','connection_results':{'wifi':{'registered':1}},
            'connection_labels':{'wifi':'Wi-Fi'},'workers_by_connection':{'wifi':6}},console=console)
        self.assertIn('Already registered',output.getvalue())

    def test_six_workers_per_connection_becomes_twelve_total_before_prepare(self):
        from bise_allocate.__main__ import main
        with patch.object(sys,'argv',['bise_allocate','http-submit','--config','allocation.json',
                '--connections','connections.json','--workers-per-connection','6','--in-flight','6','--retry-uncertain','--json']), \
                patch('bise_allocate.app.load',return_value={'scheme':'Synthetic'}), \
                patch('bise_allocate.app.paths',return_value={'registration_profile':Path('/tmp/synthetic.json'),
                    'allocation_database':Path('/tmp/synthetic-allocations.sqlite3')}), \
                patch('bise_allocate.participation.current',return_value=None), \
                patch('bise_allocate.resume.prepare',return_value={}),patch('bise_allocate.app.progress',return_value={}), \
                patch.object(network,'load',return_value=ROUTES), \
                patch.object(single,'run',new_callable=AsyncMock,return_value={'status':'complete'}) as run,redirect_stdout(io.StringIO()):
            self.assertEqual(main(),0)
        self.assertEqual(run.call_args.kwargs['workers'],12)
        self.assertEqual(run.call_args.kwargs['uncertain_retries'],2)
        self.assertEqual(run.call_args.kwargs['max_in_flight'],6)
