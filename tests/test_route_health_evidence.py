"""Separate link loss, portal socket errors and an unreliable IP echo service.

All requests and interface inventories are synthetic; no live submissions.
"""
import asyncio
from dataclasses import replace
import io
import time
import unittest
from unittest.mock import AsyncMock,patch

from rich.console import Console

from youthgames_register import connections as network,http_async as single
from youthgames_register.route_health import RouteHealth
from youthgames_register.terminal import Dashboard
from tests.test_http_connections import ROUTES


class HealthEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.route=replace(ROUTES[1],dynamic_source=True)
        self.name=self.route.name;self.events=[]
        self.available={self.route.interface:[self.route.source_ip]}
        self.health=RouteHealth([self.route],asyncio.Event(),lambda p,**v:self.events.append((p,v)),
            poll_seconds=0,retry_seconds=0)
        self.inventory=patch.object(network,'inventory',side_effect=lambda:dict(self.available))
        self.inventory.start();self.addCleanup(self.inventory.stop)
        self.echo=AsyncMock(return_value=[self.route.public_ip])
        self.verify=patch.object(network,'verify',self.echo)
        self.verify.start();self.addCleanup(self.verify.stop)

    async def start_online(self):
        self.assertTrue(await self.health.check(self.name))

    async def test_socket_errors_coalesce_without_fabricating_link_loss_or_resetting_waits(self):
        await self.start_online();generation=self.health.generation[self.name]
        control=single.Control();control.backoff(902,'server',throttle=True)
        control.backoff(0,'network',temporary_transport=True)
        before=(control.deadline,control.transport_deadline,control.submission_interval)
        self.health.on_reconnect=lambda name:control.transport_recovered()
        for _ in range(8):self.health.invalidate(self.name,'ReadTimeout')
        self.assertTrue(self.health.online[self.name])
        self.assertEqual([v['state'] for p,v in self.events],['online','degraded'])
        self.assertTrue(await self.health.check(self.name))
        self.assertEqual(self.echo.await_count,2)
        self.assertEqual(self.health.generation[self.name],generation)
        self.assertEqual((control.deadline,control.transport_deadline,control.submission_interval),before)

    async def test_transient_echo_failure_does_not_take_verified_unchanged_route_offline(self):
        await self.start_online();self.health.invalidate(self.name)
        self.echo.side_effect=network.NetworkUnavailable('echo timed out')
        self.assertTrue(await self.health.check(self.name))
        self.assertEqual(self.events[-1][1]['state'],'degraded')
        self.assertTrue(self.events[-1][1]['local_ready'])
        self.assertTrue(self.events[-1][1]['dispatch_ready'])
        self.assertEqual(self.events[-1][1]['probe_failures'],1)
        self.echo.side_effect=None
        self.assertTrue(await self.health.check(self.name))
        self.assertEqual(self.health.probe_failures[self.name],0)
        self.assertEqual(self.events[-1][1]['state'],'online')

    async def test_repeated_echo_failures_without_portal_success_pause_then_recover(self):
        await self.start_online();self.health.invalidate(self.name)
        self.echo.side_effect=network.NetworkUnavailable('echo timed out')
        self.assertTrue(await self.health.check(self.name))
        self.assertTrue(await self.health.check(self.name))
        self.assertFalse(await self.health.check(self.name))
        self.assertEqual(self.events[-1][1]['state'],'unreachable')
        self.assertTrue(self.events[-1][1]['local_ready'])
        self.assertFalse(self.events[-1][1]['dispatch_ready'])
        self.echo.side_effect=None
        self.assertTrue(await self.health.check(self.name))

    async def test_actual_portal_success_keeps_working_route_usable_during_echo_outage(self):
        await self.start_online();self.health.portal_succeeded(self.name)
        self.health.invalidate(self.name)
        self.echo.side_effect=network.NetworkUnavailable('echo service unavailable')
        for _ in range(5):self.assertTrue(await self.health.check(self.name))
        self.health.last_portal_success[self.name]=time.monotonic()-31
        self.assertFalse(await self.health.check(self.name))

    async def test_unverified_or_changed_source_never_gets_echo_grace(self):
        self.echo.side_effect=network.NetworkUnavailable('no egress')
        self.assertFalse(await self.health.check(self.name))
        self.echo.side_effect=None;await self.start_online()
        self.health.portal_succeeded(self.name)
        self.available[self.route.interface]=['192.0.2.25']
        self.echo.side_effect=network.NetworkUnavailable('new source cannot reach egress')
        self.assertFalse(await self.health.check(self.name))
        self.assertEqual(self.events[-1][1]['state'],'unreachable')

    async def test_real_unplug_blocks_immediately_even_with_recent_portal_success(self):
        await self.start_online();self.health.portal_succeeded(self.name)
        self.available.clear()
        self.assertFalse(await self.health.check(self.name))
        self.assertEqual(self.events[-1][1]['state'],'offline')
        self.assertFalse(self.events[-1][1]['local_ready'])
        self.available[self.route.interface]=[self.route.source_ip]
        self.assertTrue(await self.health.check(self.name))

    async def test_slow_echo_does_not_serialize_all_workers_on_unchanged_verified_route(self):
        await self.start_online();started=asyncio.Event();release=asyncio.Event()
        async def slow(*args,**kwargs):
            started.set();await release.wait();return [self.route.public_ip]
        self.echo.side_effect=slow;self.health.invalidate(self.name)
        probe=asyncio.create_task(self.health.check(self.name))
        try:
            await asyncio.wait_for(started.wait(),1)
            results=await asyncio.wait_for(asyncio.gather(*[self.health.check(self.name) for _ in range(8)]),.3)
            self.assertTrue(all(results));self.assertFalse(probe.done())
        finally:
            release.set();await probe

    async def test_slow_echo_on_changed_source_blocks_workers_until_verified(self):
        await self.start_online();started=asyncio.Event();release=asyncio.Event()
        self.available[self.route.interface]=['192.0.2.25']
        async def slow(*args,**kwargs):
            started.set();await release.wait();return [self.route.public_ip]
        self.echo.side_effect=slow
        probe=asyncio.create_task(self.health.check(self.name))
        await asyncio.wait_for(started.wait(),1)
        worker=asyncio.create_task(self.health.check(self.name))
        try:
            await asyncio.sleep(.01);self.assertFalse(worker.done())
            self.assertFalse(self.health.online[self.name])
        finally:
            release.set();await asyncio.gather(probe,worker)


class HealthPresentationTests(unittest.TestCase):
    def test_unplug_and_probe_outage_are_not_hidden_by_transport_retry_label(self):
        for state in ('offline','unreachable','paused','checking','degraded'):
            with self.subTest(state=state):
                output=io.StringIO();console=Console(file=output,width=180,height=70)
                ui=Dashboard('Synthetic',enabled=False,console=console)
                ui.event('connections',routes=[ROUTES[1].public()])
                ui.event('cooldown',connection='usb',kind='transport',seconds=5)
                ui.event('connection_health',connection='usb',state=state,reason='synthetic health evidence')
                console.print(ui.render())
                # Degraded routes still show the real request retry wait.
                self.assertIn(state if state!='degraded' else 'retrying',output.getvalue())
