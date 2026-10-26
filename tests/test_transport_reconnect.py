"""Regression for an online USB route stranded in cumulative local backoff.

All network requests are mocked. No applicant POST reaches a live portal.
"""
import asyncio
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch

import httpx
from rich.console import Console

from youthgames_register import http_async as single,connections as network
from youthgames_register.route_health import RouteHealth
from youthgames_register.store import opened
from youthgames_register.terminal import Dashboard
from tests.test_http_connections import ROUTES


class RecoveryPolicyTests(unittest.TestCase):
    def test_transport_counter_caps_and_success_restarts_at_five_seconds(self):
        control=single.Control()
        for number in range(20):
            with patch.object(single.time,'monotonic',return_value=number*100):
                seconds=control.backoff(0,'network',temporary_transport=True)
                self.assertEqual(seconds,min(30,5*2**min(number,3)))
        self.assertEqual(control.transport_retry_round,20)
        self.assertEqual(control.retry_round,0)
        self.assertEqual(control.throttle_round,0)
        self.assertTrue(control.transport_recovered())
        self.assertEqual(control.transport_retry_round,0)
        with patch.object(single.time,'monotonic',return_value=2100):
            self.assertEqual(control.backoff(0,'network',temporary_transport=True),5)

    def test_concurrent_network_errors_coalesce_without_counter_explosion(self):
        control=single.Control()
        with patch.object(single.time,'monotonic',return_value=100):
            for _ in range(36):self.assertEqual(control.backoff(0,'network',temporary_transport=True),5)
        self.assertEqual(control.transport_retry_round,1)

    def test_transport_recovery_preserves_server_retry_after_and_slower_spacing(self):
        control=single.Control()
        control.backoff(902,'server',throttle=True)
        server=control.deadline;spacing=control.submission_interval
        control.backoff(0,'network',temporary_transport=True)
        self.assertTrue(control.transport_recovered())
        self.assertEqual(control.effective_deadline,server)
        self.assertEqual(control.submission_interval,spacing)
        self.assertGreater(server-time.monotonic(),900)

    def test_typed_restart_keeps_server_and_transport_separate_for_both_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.sqlite3'
            with opened(path) as db:
                global_ctl=single.Control();global_ctl.bind(db,single.BASE,'campaign')
                global_ctl.backoff(60,'server',throttle=True)
                mobile=single.Control();mobile.bind(db,single.BASE,'campaign','usb')
                mobile.backoff(0,'network',temporary_transport=True)
            with opened(path) as db:
                mobile=single.Control();mobile.bind(db,single.BASE,'campaign','usb',requested_interval=5)
                self.assertGreater(mobile.transport_deadline-time.monotonic(),3)
                self.assertEqual(mobile.transport_retry_round,1)
                mobile.transport_recovered()
                self.assertEqual(mobile.transport_deadline,0)
                self.assertGreater(mobile.deadline-time.monotonic(),55)
                self.assertEqual(mobile.submission_interval,7.5)
            with opened(path) as db:
                again=single.Control();again.bind(db,single.BASE,'campaign','usb')
                self.assertEqual(again.transport_deadline,0)
                self.assertEqual(again.transport_retry_round,0)
                self.assertGreater(again.deadline-time.monotonic(),55)

    def test_untyped_legacy_deadline_is_not_mistakenly_cleared(self):
        with tempfile.TemporaryDirectory() as directory,opened(Path(directory)/'state.sqlite3') as db:
            until=(datetime.now(timezone.utc)+timedelta(seconds=300)).isoformat()
            db.execute('INSERT INTO connection_pacing VALUES (?,?,?,?,?,?,?,?,?)',
                (single.BASE,'campaign','usb',5,16,0,until,'temporary_undispatched_form_failure',until))
            control=single.Control();control.bind(db,single.BASE,'campaign','usb')
            control.transport_recovered()
            self.assertGreater(control.deadline-time.monotonic(),295)


class ReconnectWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_36_waiters_wake_even_when_a_new_waiter_arrives_during_notification(self):
        control=single.Control();control.transport_delay(300)
        tasks=[asyncio.create_task(control.wait()) for _ in range(36)]
        asyncio.get_running_loop().call_soon(control.transport_recovered)
        tasks.append(asyncio.create_task(control.wait()))
        self.assertTrue(all(await asyncio.wait_for(asyncio.gather(*tasks),.5)))
        self.assertFalse(control.waiters)

    async def test_recovery_wakes_old_wait_immediately_but_retains_dispatch_spacing(self):
        control=single.Control();control.transport_delay(300)
        waiting=asyncio.create_task(control.wait(.04))
        await asyncio.sleep(.005);self.assertFalse(waiting.done())
        control.transport_recovered()
        await asyncio.sleep(.005);self.assertFalse(waiting.done())
        self.assertTrue(await asyncio.wait_for(waiting,.2))

    async def test_stop_and_cancellation_drain_wait_tasks(self):
        for cancel in (False,True):
            control=single.Control();control.transport_delay(300)
            waiting=asyncio.create_task(control.wait());await asyncio.sleep(.001)
            if cancel:
                waiting.cancel()
                with self.assertRaises(asyncio.CancelledError):await waiting
            else:
                control.stop.set();self.assertFalse(await asyncio.wait_for(waiting,.2))
        self.assertFalse([task for task in asyncio.all_tasks() if task is not asyncio.current_task()])

    async def test_ip_echo_after_portal_failure_does_not_clear_backoff_but_real_replug_does(self):
        stop=asyncio.Event();control=single.Control();events=[]
        route=replace(ROUTES[1],dynamic_source=True)
        health=RouteHealth([route],stop,lambda p,**v:events.append((p,v)),poll_seconds=0,retry_seconds=0)
        health.on_reconnect=lambda name:control.transport_recovered()
        available={route.interface:[route.source_ip]}
        with patch.object(network,'inventory',side_effect=lambda:dict(available)),patch.object(network,'verify',new=AsyncMock(return_value=[route.public_ip])):
            self.assertTrue(await health.check(route.name));original=health.generation[route.name]
            control.backoff(0,'network',temporary_transport=True)
            health.invalidate(route.name)
            self.assertTrue(await health.check(route.name))
            self.assertEqual(control.transport_retry_round,1)
            self.assertEqual(health.generation[route.name],original)
            # Reproduce the old stalled 300s local wait, with a real server wait too.
            control.transport_delay(300);control.delay(.06)
            waiting=asyncio.create_task(control.wait());await asyncio.sleep(.001)
            available.clear();self.assertFalse(await health.check(route.name))
            available[route.interface]=[route.source_ip]
            self.assertTrue(await health.check(route.name))
            self.assertEqual(control.transport_retry_round,0)
            self.assertFalse(waiting.done())
            self.assertTrue(await asyncio.wait_for(waiting,.2))

    async def test_inner_get_retry_waits_offline_and_replaces_pool_after_replug(self):
        control=single.Control();gate_open=asyncio.Event();failed=asyncio.Event();calls=[]
        generation=1
        async def gate():await gate_open.wait();return True
        control.request_gate=gate;control.network_generation=lambda:generation
        class Client:
            route_generation=1
            resets=0
            async def get(self):
                calls.append('GET')
                if len(calls)==1:
                    gate_open.clear();failed.set();raise httpx.ConnectError('synthetic unplug')
                return single.HttpResponse(200,'synthetic')
            async def reset(self):self.resets+=1
        client=Client();gate_open.set()
        waiting=asyncio.create_task(single.get_form(client,control,lambda *a,**v:None,{}))
        await asyncio.wait_for(failed.wait(),.2);await asyncio.sleep(.01)
        self.assertEqual(calls,['GET']);self.assertFalse(waiting.done())
        self.assertEqual(control.transport_retry_round,1)
        generation=2;control.transport_recovered();gate_open.set()
        response=await asyncio.wait_for(waiting,.2)
        self.assertEqual(response.status,200);self.assertEqual(client.resets,2)
        self.assertEqual(control.transport_retry_round,0)

    async def test_fresh_pool_recovers_without_replaying_any_post(self):
        requests=[]
        def handler(request):
            requests.append(request.method)
            return httpx.Response(200,text='synthetic',headers={'set-cookie':'session=synthetic; Path=/; Secure'})
        async with single.Client(transport=httpx.MockTransport(handler)) as client:
            old=client.client
            await client.post(b'synthetic','text/plain')
            await client.reset()
            self.assertTrue(old.is_closed);self.assertIsNot(client.client,old)
            with self.assertRaisesRegex(ValueError,'already sent'):await client.post(b'synthetic','text/plain')
            await client.get()
        self.assertEqual(requests,['POST','GET'])


class RecoveryPresentationTests(unittest.TestCase):
    def test_transport_retry_is_visible_and_reconnect_retains_server_wait(self):
        output=io.StringIO();console=Console(file=output,width=180,height=70)
        ui=Dashboard('Synthetic',enabled=False,console=console)
        ui.event('connections',routes=[ROUTES[1].public()])
        ui.event('connection_health',connection='usb',state='online')
        ui.event('cooldown',connection='usb',kind='transport',seconds=300)
        console.print(ui.render());self.assertIn('retry in 300s',output.getvalue());self.assertIn('retrying',output.getvalue())
        ui.event('cooldown',connection='usb',seconds=60)
        ui.event('recovery_reset',connection='usb',server_seconds=60)
        output.seek(0);output.truncate(0);console.print(ui.render())
        self.assertNotIn('300s',output.getvalue());self.assertIn('server cooldown 60s',output.getvalue())
        ui.event('recovery_reset',connection='usb',server_seconds=0)
        output.seek(0);output.truncate(0);console.print(ui.render())
        self.assertIn('ready',output.getvalue());self.assertNotIn('retrying',output.getvalue())
