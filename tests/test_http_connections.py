"""No live network: fixed interfaces, isolated quotas and shared durable identity."""

from contextlib import redirect_stdout
from dataclasses import replace
import asyncio
import io
import json
import sqlite3
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

import httpx

from youthgames_register import connections as network,http_async as single,http_multi
from youthgames_register.store import opened,jobs,retain_connection_history
from tests import test_http_concurrent as fixture


ROUTES=[network.Connection('wifi','wlan-fixture','192.0.2.1','1.1.1.1',6),
        network.Connection('usb','usb-fixture','192.0.2.2','8.8.8.8',6)]


class ServerDuplicateContractTests(unittest.TestCase):
    def test_captured_live_bform_message_is_terminal_duplicate_only_with_server_error_contract(self):
        message='This B-Form / CNIC has already been registered.'
        html=f'''<form id="registration-form" data-has-server-errors="1">
        <div id="registration-errors"><ul><li>{message}</li></ul></div></form>'''
        outcome=single.classify(html,200)
        self.assertEqual(outcome['state'],'already_registered_external')
        self.assertEqual(outcome['detail'],message)
        self.assertFalse(outcome['rate_limited']);self.assertFalse(outcome['retryable_captcha'])
        self.assertEqual(single.classify(html.replace('data-has-server-errors="1"',''),200)['state'],'uncertain')
        self.assertEqual(single.classify(html,503)['state'],'uncertain')
        conflict=html+'<span id="registration-success-tracking-id">TEST-CONFLICT</span>'
        self.assertEqual(single.classify(conflict,200)['state'],'uncertain')


class RouteValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_usb_operstate_with_real_carrier_is_ready_but_missing_carrier_is_not(self):
        addresses=[{'ifname':name,'operstate':'UNKNOWN','flags':flags,
            'addr_info':[{'family':'inet','scope':'global','local':address}]} for name,flags,address in
            [('usb0',['BROADCAST','MULTICAST','UP','LOWER_UP'],'172.31.203.233'),
             ('usb1',['UP'],'192.0.2.2'),('usb2',['LOWER_UP'],'192.0.2.3')]]
        routes=[{'dst':'default','dev':row['ifname'],'table':20040} for row in addresses]
        with patch.object(network,'ip_info',side_effect=lambda *args:addresses if args[0]=='address' else routes) as info:
            self.assertEqual(network.inventory(),{'usb0':['172.31.203.233']})
            self.assertIn(unittest.mock.call('-4','route','show','table','all'),info.call_args_list)

    async def test_discovery_includes_usb_default_in_isolated_policy_table(self):
        addresses=[{'ifname':name,'operstate':'UP','addr_info':[{'family':'inet','scope':'global','local':address}]}
            for name,address in [('wlan0','192.168.18.6'),('usb0','172.31.203.19')]]
        routes=[{'dst':'default','dev':'wlan0','table':'main'},
            {'dst':'default','dev':'usb0','table':20040},
            {'dst':'default','dev':'usb0','table':20040}]
        def info(*args):return addresses if args[0]=='address' else routes
        with patch.object(network,'ip_info',side_effect=info):
            self.assertEqual(network.inventory(),{'wlan0':['192.168.18.6'],'usb0':['172.31.203.19']})

    async def test_discovery_ignores_down_unreachable_or_ipv6_only_routes(self):
        addresses=[{'ifname':name,'operstate':state,'addr_info':[{'family':family,'scope':'global','local':'fixture'}]}
            for name,state,family in [('wlan0','UP','inet'),('usb0','DOWN','inet'),('bad','UP','inet'),('v6','UP','inet6')]]
        routes=[{'dst':'default','dev':name,'type':'unreachable' if name=='bad' else 'unicast'}
            for name in ('wlan0','usb0','bad','v6')]
        with patch.object(network,'ip_info',side_effect=lambda *args:addresses if args[0]=='address' else routes):
            self.assertEqual(network.inventory(),{'wlan0':['fixture']})

    async def test_duplicate_public_ip_or_changed_ip_fails_before_submissions(self):
        def factory(route):
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'ip':'1.1.1.1'})))
        with self.assertRaisesRegex(ValueError,'public IP changed'):await network.verify(ROUTES,client_factory=factory)
        with self.assertRaisesRegex(ValueError,'same public IP'):await network.verify(ROUTES,pinned=False,client_factory=factory)

    async def test_egress_checks_are_get_only_and_do_not_send_applicant_data(self):
        requests=[]
        def factory(route):
            def response(request):
                requests.append(request);return httpx.Response(200,json={'ip':route.public_ip})
            return httpx.AsyncClient(transport=httpx.MockTransport(response))
        self.assertEqual(await network.verify(ROUTES,client_factory=factory),['1.1.1.1','8.8.8.8'])
        self.assertTrue(all(r.method=='GET' and not r.content and r.url.host=='api.ipify.org' for r in requests))

    async def test_no_interface_fallback_or_environment_proxy_is_used(self):
        with patch('youthgames_register.bound_transport.InterfaceTransport') as transport:ROUTES[0].transport()
        transport.assert_called_once_with('wlan-fixture','192.0.2.1')

    async def test_bad_config_and_missing_usb_fail_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'connections.json'
            config={'schema_version':1,'connections':[{'name':r.name,'interface':r.interface,'source_ip':r.source_ip,
                'expected_public_ip':r.public_ip,'dispatch_interval_seconds':6} for r in ROUTES]}
            path.write_text(json.dumps(config))
            with patch.object(network,'inventory',return_value={'wlan-fixture':['192.0.2.1']}),patch.object(network.socket,'socket'):
                with self.assertRaisesRegex(ValueError,'missing, down'):network.load(path)
            self.assertEqual(json.loads(path.read_text()),config)

    async def test_discovery_is_idempotent_and_preserves_operator_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'connections.json'
            with patch.object(network,'inventory',return_value={'wlan-fixture':['192.0.2.1'],'usb-fixture':['192.0.2.2']}):
                self.assertEqual(network.discover(path)['connections'],2)
                original=path.read_bytes()
                with patch.object(network,'load',return_value=ROUTES):self.assertEqual(network.discover(path)['status'],'existing_config_preserved')
                self.assertEqual(path.read_bytes(),original)

    async def test_configured_source_interface_missing_and_invalid_interval_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'connections.json'
            rows=[{'name':r.name,'interface':r.interface,'source_ip':r.source_ip,'expected_public_ip':r.public_ip,
                'dispatch_interval_seconds':6} for r in ROUTES]
            available={r.interface:[r.source_ip] for r in ROUTES}
            with patch.object(network,'inventory',return_value=available),patch.object(network.socket,'socket'):
                for interval in (True,0,4,float('inf'),float('nan')):
                    rows[0]['dispatch_interval_seconds']=interval
                    path.write_text(json.dumps({'schema_version':1,'connections':rows}))
                    with self.assertRaises(ValueError):network.load(path)
                rows[0]['dispatch_interval_seconds']=6;rows[0]['source_ip']='192.0.2.99'
                path.write_text(json.dumps({'schema_version':1,'connections':rows}))
                with self.assertRaisesRegex(ValueError,'not assigned'):network.load(path)

    async def test_post_redirect_headers_are_kept_separate_from_final_get(self):
        def handler(request):
            if request.method=='POST':return httpx.Response(302,headers={'location':'/','x-ratelimit-limit':'5','x-ratelimit-remaining':'0'})
            return httpx.Response(200,text='OK',headers={'x-ratelimit-limit':'100','x-ratelimit-remaining':'99'})
        async with single.Client(transport=httpx.MockTransport(handler)) as client:response=await client.post(b'body','text/plain')
        self.assertEqual([hop['method'] for hop in response.hops],['POST','GET'])
        self.assertEqual(response.hops[0]['rate_headers']['x-ratelimit-limit'],'5')
        self.assertEqual(response.hops[1]['rate_headers']['x-ratelimit-limit'],'100')


class MultiConnectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.ConcurrentHttpTests.asyncSetUp
    write_input=fixture.ConcurrentHttpTests.write_input
    handler=fixture.ConcurrentHttpTests.handler

    def factory(self,worker):
        self.clients.append(worker)
        async def handler(request):
            if request.method=='POST':self.sent_on.append((worker,await request.aread()))
            return await self.handler(request)
        return single.Client(transport=httpx.MockTransport(handler))

    async def multi(self,workers=6,limit=0,retry_uncertain=False,observed_ips=None,uncertain_retries=0,inventory=None,ui=None):
        self.sent_on=[]
        original=single.Control
        def fast(**kwargs):
            kwargs['submission_interval']=.025
            control=original(**kwargs)
            transport_delay=control.transport_delay
            # Timing policy has separate unit tests; integration uses a short
            # synthetic network retry rather than sleeping real 5–30 seconds.
            control.transport_delay=lambda seconds:transport_delay(min(seconds,.003))
            return control
        observed=dict(zip((r.name for r in ROUTES),observed_ips or [r.public_ip for r in ROUTES]))
        from youthgames_register.route_health import RouteHealth
        def quick_health(*args,**kwargs):return RouteHealth(*args,**kwargs,poll_seconds=.005,retry_seconds=.005)
        with patch.object(network,'load',return_value=ROUTES),patch.object(network,'inventory',
                side_effect=inventory or (lambda:{r.interface:[r.source_ip] for r in ROUTES})), \
                patch.object(network,'verify',new_callable=AsyncMock,
                    side_effect=lambda candidates,pinned=False:[observed[r.name] for r in candidates]),patch.object(single,'Control',side_effect=fast), \
                patch('youthgames_register.http_multi.RouteHealth',side_effect=quick_health):
            return await http_multi.run(self.profile,'synthetic-connections.json',workers=workers,limit=limit,
                client_factory=self.factory,install_signals=False,prepare_ahead=2,max_in_flight=2,retry_uncertain=retry_uncertain,
                uncertain_retries=uncertain_retries,ui=ui)

    async def test_parallel_fixed_routes_register_once_and_rerun_skips_completed(self):
        self.write_input(8);self.post_delay=.045
        result=await self.multi()
        self.assertEqual(result['registered_now'],8);self.assertEqual(result['connections'],2)
        self.assertGreaterEqual(self.peak,2)
        self.assertGreater(result['connection_results']['wifi']['registered'],0)
        self.assertGreater(result['connection_results']['usb']['registered'],0)
        self.assertEqual(len({fields['cnic'] for fields in self.posts}),8)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempt_connections').fetchone()[0],8)
            self.assertEqual(db.execute('SELECT count(DISTINCT connection_key) FROM connection_dispatch_clock').fetchone()[0],2)
            for row in jobs(db,'Lahore'):
                proof=json.loads(Path(row['receipt']).with_name('http-request-proof.json').read_text())
                self.assertIn(proof['connection'],('wifi','usb'))
        before=len(self.posts);gets=self.gets
        again=await self.multi()
        self.assertEqual(again['skipped_registered'],8);self.assertEqual(len(self.posts),before);self.assertEqual(self.gets,gets)

    async def test_changed_nat_addresses_run_and_restart_without_resubmitting_completed(self):
        self.write_input(2)
        result=await self.multi(workers=2,observed_ips=['9.9.9.9','4.2.2.2'])
        self.assertEqual(result['registered_now'],2)
        before=len(self.posts)
        result=await self.multi(workers=2,observed_ips=['1.0.0.1','4.2.2.3'])
        self.assertEqual(result['skipped_registered'],2);self.assertEqual(len(self.posts),before)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM connection_slots').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM connection_observations').fetchone()[0],4)

    async def test_stop_during_initial_network_wait_writes_summary_without_opening_registration_db(self):
        async def stopped(health):
            health.stop.set();return None
        with patch.object(network,'load',return_value=ROUTES),patch('youthgames_register.route_health.RouteHealth.initial',stopped):
            result=await http_multi.run(self.profile,'synthetic.json',workers=2,install_signals=False)
        self.assertEqual(result['blocked'],'operator_stopped_before_submission')
        self.assertTrue(Path(result['report']).is_file())
        self.assertFalse((self.root/'jobs.sqlite3').exists());self.assertFalse(self.posts)

    async def test_one_route_throttle_does_not_move_retry_to_other_connection(self):
        self.write_input(4);self.post_refusals=1
        original_delay=single.Control.delay
        def accelerated(control,seconds):return original_delay(control,min(seconds,.015))
        with patch.object(single.Control,'delay',accelerated):result=await self.multi()
        self.assertEqual(result['registered_now'],4);self.assertEqual(result['throttle_refusals'],1)
        with opened(self.root/'jobs.sqlite3') as db:
            retried=db.execute('SELECT job FROM attempts GROUP BY job HAVING count(*)=2').fetchone()[0]
            self.assertEqual(db.execute('SELECT count(DISTINCT c.connection_key) FROM attempt_connections c JOIN attempts a ON a.id=c.attempt WHERE a.job=?',(retried,)).fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM connection_pacing').fetchone()[0],1)

    async def test_unknown_post_stops_all_routes_and_rerun_never_resubmits(self):
        self.write_input(6);self.fail_one=True
        result=await self.multi(workers=2)
        self.assertEqual(result['blocked'],'uncertain_submission_requires_reconciliation')
        before=len(self.posts)
        again=await self.multi(workers=2)
        self.assertEqual(again['blocked'],'uncertain_submission_requires_reconciliation');self.assertEqual(len(self.posts),before)

    async def test_opt_in_uncertain_retry_skips_actual_duplicate_and_continues_preserving_history(self):
        self.write_input(4);self.fail_one=True
        await self.multi(workers=2)
        with opened(self.root/'jobs.sqlite3') as db:
            old=[dict(row) for row in db.execute("SELECT * FROM attempts WHERE outcome='uncertain'")]
            self.assertTrue(old)
            saved_files={row['evidence']:Path(row['evidence']).read_bytes() for row in old}
            completed={row['id']:dict(row) for row in jobs(db,'Lahore') if row['state']=='registered'}
        previous_cnics={fields['cnic'] for fields in self.posts};original=self.handler
        async def duplicate_portal(request):
            body=await request.aread()
            duplicate=request.method=='POST' and any(cnic in body for cnic in previous_cnics)
            result=await original(request)
            if duplicate:
                return httpx.Response(200,text=fixture.THROTTLED.replace(
                    'Too many registration attempts. Please wait 48 seconds and try again.',
                    'This B-Form / CNIC has already been registered.'))
            return result
        self.handler=duplicate_portal;self.fail_one=False
        result=await self.multi(workers=2,retry_uncertain=True)
        self.assertEqual(result['already_registered_now'],len(old))
        self.assertEqual(result['uncertain_requeued'],len(old))
        self.assertNotIn('blocked',result)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertTrue(all(row['state'] in ('registered','already_registered_external') for row in jobs(db,'Lahore')))
            self.assertEqual([dict(row) for row in db.execute("SELECT * FROM attempts WHERE outcome='uncertain'")],old)
            for ident,job in completed.items():self.assertEqual(dict(db.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()),job)
            self.assertEqual(db.execute('SELECT count(*) FROM uncertain_retries').fetchone()[0],len(old))
        for path,content in saved_files.items():self.assertEqual(Path(path).read_bytes(),content)
        before=len(self.posts);again=await self.multi(workers=2,retry_uncertain=True)
        self.assertEqual(len(self.posts),before);self.assertEqual(again['uncertain_requeued'],0)

    async def test_opt_in_retry_can_succeed_but_a_new_unknown_is_quarantined_without_looping(self):
        self.write_input(2);self.fail_one=True
        await self.multi(workers=2)
        before=len(self.posts)
        result=await self.multi(workers=2,retry_uncertain=True)
        self.assertEqual(len(self.posts),before+1)
        self.assertNotIn('blocked',result);self.assertEqual(result['status'],'complete_with_issues')
        self.fail_one=False
        result=await self.multi(workers=2,retry_uncertain=True)
        self.assertEqual(result['registered_now'],1)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM uncertain_retries').fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE outcome='uncertain'").fetchone()[0],2)

    async def test_single_connection_runner_also_requires_opt_in_and_preserves_old_attempt(self):
        self.write_input(2);self.fail_one=True
        await self.multi(workers=2)
        before=len(self.posts)
        blocked=await single.run(self.profile,workers=2,client_factory=self.factory,install_signals=False)
        self.assertEqual(blocked['blocked'],'uncertain_submission_requires_reconciliation')
        self.assertEqual(len(self.posts),before)
        self.fail_one=False
        result=await single.run(self.profile,workers=2,client_factory=self.factory,
            control=single.Control(submission_interval=.01),install_signals=False,retry_uncertain=True)
        self.assertEqual(result['uncertain_requeued'],1);self.assertEqual(result['registered_now'],1)
        before=len(self.posts)
        result=await single.run(self.profile,workers=2,client_factory=self.factory,install_signals=False,retry_uncertain=True)
        self.assertEqual(result['skipped_registered'],2);self.assertEqual(len(self.posts),before)

    async def test_cancel_keeps_dispatched_outcomes_uncertain_before_database_closes(self):
        self.write_input(4);self.cancel_at_post=True
        task=asyncio.create_task(self.multi(workers=2))
        await asyncio.wait_for(self.in_post.wait(),5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertGreater(db.execute("SELECT count(*) FROM jobs WHERE state='uncertain'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE state='submitting'").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM attempts WHERE outcome IS NULL').fetchone()[0],0)

    async def test_route_association_is_immutable_after_dispatch(self):
        self.write_input(2);await self.multi(workers=2)
        with opened(self.root/'jobs.sqlite3') as db:
            row=tuple(db.execute('SELECT * FROM attempt_connections ORDER BY attempt LIMIT 1').fetchone())
            for sql in ("UPDATE attempt_connections SET connection_key='changed'",'DELETE FROM attempt_connections'):
                with self.assertRaises(sqlite3.IntegrityError),db:db.execute(sql)
            with self.assertRaises(sqlite3.IntegrityError),db:
                db.execute('INSERT OR REPLACE INTO attempt_connections VALUES (?,?,?)',row)

    async def test_failed_egress_validation_creates_no_registration_db_or_posts(self):
        with patch.object(network,'load',return_value=ROUTES),patch.object(network,'inventory',return_value={r.interface:[r.source_ip] for r in ROUTES}), \
                patch.object(network,'verify',new_callable=AsyncMock,side_effect=ValueError('IP changed')):
            with self.assertRaisesRegex(ValueError,'IP changed'):
                await single.run(self.profile,connections='synthetic.json',workers=2,client_factory=self.factory,install_signals=False)
        self.assertFalse((self.root/'jobs.sqlite3').exists());self.assertFalse(self.posts)

    async def test_connection_pacing_persists_independently_and_inherits_legacy_backoff(self):
        with opened(self.root/'jobs.sqlite3') as db:
            wifi=single.Control();wifi.bind(db,single.BASE,'campaign',ROUTES[0].key,'wifi')
            usb=single.Control();usb.bind(db,single.BASE,'campaign',ROUTES[1].key,'usb')
            wifi.backoff(30,'fixture_throttle',throttle=True)
            self.assertEqual(usb.deadline,0)
            restored=single.Control();restored.bind(db,single.BASE,'campaign',ROUTES[0].key,'wifi')
            self.assertGreater(restored.deadline,0);self.assertEqual(restored.submission_interval,7.5)
            self.assertEqual(db.execute('SELECT count(*) FROM portal_pacing').fetchone()[0],0)
            legacy=single.Control();legacy.bind(db,single.BASE,'old-campaign');legacy.backoff(45,'old_throttle',throttle=True)
            for route in ROUTES:
                control=single.Control();control.bind(db,single.BASE,'old-campaign',route.key,route.name)
                self.assertGreater(control.deadline,0);self.assertEqual(control.submission_interval,7.5)


class ConnectionsCliTests(unittest.TestCase):
    def test_existing_cli_passes_opt_in_connections_to_the_http_runner(self):
        from bise_allocate.__main__ import main
        with patch.object(sys,'argv',['bise_allocate','http-submit','--config','allocation.json','--connections','connections.json','--workers','6','--retry-uncertain','--json']), \
                patch('bise_allocate.app.load',return_value={'scheme':'Synthetic'}), \
                patch('bise_allocate.app.paths',return_value={'registration_profile':Path('/tmp/synthetic.json'),
                    'allocation_database':Path('/tmp/synthetic-allocations.sqlite3')}), \
                patch('bise_allocate.participation.current',return_value=None), \
                patch('bise_allocate.resume.prepare',return_value={'input_reused':True}), \
                patch('bise_allocate.app.progress',return_value={}),patch.object(network,'load',return_value=ROUTES), \
                patch.object(single,'run',new_callable=AsyncMock,return_value={'status':'complete'}) as run,redirect_stdout(io.StringIO()):
            self.assertEqual(main(),0)
        self.assertEqual(run.call_args.kwargs['connections'],Path('connections.json'))
        self.assertEqual(run.call_args.kwargs['workers'],6)
        self.assertTrue(run.call_args.kwargs['retry_uncertain'])


class DynamicConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_five_second_baseline_keeps_deadline_but_replaces_old_six(self):
        from datetime import datetime,timedelta,timezone
        with tempfile.TemporaryDirectory() as directory,opened(Path(directory)/'jobs.sqlite3') as db:
            until=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
            with db:
                db.execute('INSERT INTO connection_pacing VALUES (?,?,?,?,?,?,?,?,?)',
                    (single.BASE,'campaign',ROUTES[0].key,6,2,0,until,'uncertain_response_retry',until))
            before=tuple(db.execute('SELECT * FROM connection_pacing').fetchone())
            requested=single.Control(submission_interval=5)
            requested.bind(db,single.BASE,'campaign',ROUTES[0].key,'wifi',requested_interval=5)
            self.assertEqual(requested.submission_interval,5);self.assertEqual(requested.retry_round,2)
            self.assertGreater(requested.deadline,0)
            self.assertEqual(tuple(db.execute('SELECT * FROM connection_pacing').fetchone()),before)
            default=single.Control(submission_interval=5)
            default.bind(db,single.BASE,'campaign',ROUTES[0].key,'wifi')
            self.assertEqual(default.submission_interval,6)

    async def test_explicit_baseline_never_lowers_recorded_server_throttle_spacing(self):
        from datetime import datetime,timedelta,timezone
        with tempfile.TemporaryDirectory() as directory,opened(Path(directory)/'jobs.sqlite3') as db:
            until=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
            with db:
                db.execute('INSERT INTO portal_pacing VALUES (?,?,?,?,?,?,?,?)',
                    (single.BASE,'campaign',12,2,2,until,'explicit_server_throttle',until))
            control=single.Control(submission_interval=5)
            control.bind(db,single.BASE,'campaign',ROUTES[0].key,'wifi',requested_interval=5)
            self.assertEqual(control.submission_interval,12);self.assertGreater(control.deadline,0)

    async def test_changed_nat_is_accepted_on_same_device_without_editing_config(self):
        events=[]
        with patch.object(network,'load',return_value=ROUTES),patch.object(network,'verify',new_callable=AsyncMock,
                return_value=['9.9.9.9','4.2.2.2']) as verify:
            ready=await network.ready('fixed-config.json',asyncio.Event(),lambda phase,**v:events.append((phase,v)))
        verify.assert_awaited_once_with(ROUTES,pinned=False)
        self.assertEqual([r.key for r in ready],[r.key for r in ROUTES])
        self.assertEqual([r.interface for r in ready],[r.interface for r in ROUTES])
        self.assertEqual([r.public_ip for r in ready],['9.9.9.9','4.2.2.2'])
        self.assertTrue(any('2 changed IP' in values.get('message','') for _,values in events))

    async def test_missing_usb_and_transient_egress_recover_before_any_submission(self):
        events=[]
        with patch.object(network,'load',side_effect=[network.NetworkUnavailable('USB missing'),ROUTES,ROUTES]), \
                patch.object(network,'verify',new_callable=AsyncMock,
                    side_effect=[network.NetworkUnavailable('mobile internet reconnecting'),[r.public_ip for r in ROUTES]]):
            result=await network.ready('fixed.json',asyncio.Event(),lambda phase,**v:events.append((phase,v)),retry_base=.001)
        self.assertEqual(len(result),2)
        self.assertEqual(sum(phase=='network_wait' for phase,_ in events),2)

    async def test_wait_is_interruptible_and_bad_config_is_not_retried(self):
        stop=asyncio.Event()
        def emit(phase,**values):
            if phase=='network_wait':stop.set()
        with patch.object(network,'load',side_effect=network.NetworkUnavailable('offline')):
            self.assertIsNone(await asyncio.wait_for(network.ready('fixed.json',stop,emit),1))
        with patch.object(network,'load',side_effect=ValueError('invalid config')) as load:
            with self.assertRaisesRegex(ValueError,'invalid config'):
                await network.ready('fixed.json',asyncio.Event(),lambda *a,**k:None)
        self.assertEqual(load.call_count,1)

    async def test_runtime_config_validation_allows_offline_dhcp_and_stale_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'connections.json'
            path.write_text(json.dumps({'schema_version':1,'connections':[
                {'name':r.name,'interface':r.interface,'source_ip':None,'expected_public_ip':r.public_ip,
                 'dispatch_interval_seconds':6} for r in ROUTES]}))
            with patch.object(network,'inventory',side_effect=AssertionError('offline syntax validation must not inspect network')):
                loaded=network.load(path,require_pinned=False,online=False)
            self.assertTrue(all(r.dynamic_source and r.source_ip is None for r in loaded))

    async def test_interface_key_keeps_legacy_cooldowns_clocks_and_does_not_change_on_nat_or_rename(self):
        from datetime import datetime,timedelta,timezone
        from youthgames_register.config import signature
        with tempfile.TemporaryDirectory() as directory,opened(Path(directory)/'jobs.sqlite3') as db:
            old=signature({'public_ip':'1.1.1.1'});stamp=datetime.now(timezone.utc).isoformat()
            until=(datetime.now(timezone.utc)+timedelta(seconds=90)).isoformat()
            with db:
                db.execute('INSERT INTO connection_pacing VALUES (?,?,?,?,?,?,?,?,?)',
                    (single.BASE,'campaign',old,12,3,2,until,'prior throttle',stamp))
                db.execute('INSERT INTO connection_dispatch_clock VALUES (?,?,?,?)',(single.BASE,'campaign',old,stamp))
            legacy=tuple(db.execute('SELECT * FROM connection_pacing').fetchone())
            for route in ROUTES:retain_connection_history(db,single.BASE,'campaign',route)
            changed=replace(ROUTES[0],name='renamed',public_ip='9.9.9.9')
            self.assertEqual(changed.key,ROUTES[0].key)
            retain_connection_history(db,single.BASE,'campaign',changed)
            control=single.Control();control.bind(db,single.BASE,'campaign',changed.key,changed.name)
            self.assertEqual(control.submission_interval,12);self.assertEqual(control.throttle_round,2)
            self.assertGreater(control.deadline,0)
            self.assertEqual(db.execute('SELECT dispatched_at FROM connection_dispatch_clock WHERE connection_key=?',(changed.key,)).fetchone()[0],stamp)
            self.assertEqual(tuple(db.execute('SELECT * FROM connection_pacing WHERE connection_key=?',(old,)).fetchone()),legacy)
            self.assertEqual(db.execute('SELECT count(*) FROM connection_slots').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM connection_observations').fetchone()[0],3)
