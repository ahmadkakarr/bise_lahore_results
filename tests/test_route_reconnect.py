"""No live network: independent route availability and USB hotplug lifecycle."""
import asyncio
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock,patch

import httpx

from youthgames_register import connections as network,http_async as single
from youthgames_register.route_health import RouteHealth
from youthgames_register.store import opened,jobs
from tests import test_http_connections as fixture

ROUTES=fixture.ROUTES


class HealthTests(unittest.IsolatedAsyncioTestCase):
    def setup_health(self):
        self.stop=asyncio.Event();self.events=[]
        routes=[ROUTES[0],replace(ROUTES[1],source_ip=None,dynamic_source=True)]
        return RouteHealth(routes,self.stop,lambda p,**v:self.events.append((p,v)),poll_seconds=.001,retry_seconds=.001)

    async def test_wifi_can_start_alone_and_usb_wait_resumes_with_new_dhcp_source(self):
        health=self.setup_health();available={ROUTES[0].interface:[ROUTES[0].source_ip]}
        async def echo(routes,pinned=False):return [ROUTES[0].public_ip if routes[0].name=='wifi' else ROUTES[1].public_ip]
        with patch.object(network,'inventory',side_effect=lambda:dict(available)),patch.object(network,'verify',side_effect=echo):
            current=await health.initial()
            self.assertEqual(len(current),2);self.assertEqual(health.online,{'wifi':True,'usb':False})
            waiter=asyncio.create_task(health.wait('usb'))
            await asyncio.sleep(.005);self.assertFalse(waiter.done())
            self.assertTrue(await health.check('wifi'));self.assertFalse(self.stop.is_set())
            available[ROUTES[1].interface]=['192.0.2.25']
            self.assertTrue(await asyncio.wait_for(waiter,1))
            self.assertEqual(health.current['usb'].source_ip,'192.0.2.25')
            self.assertEqual(health.current['usb'].key,ROUTES[1].key)
        self.assertTrue(any(p=='connection_health' and v['state']=='offline' for p,v in self.events))
        self.assertTrue(any(p=='connection_health' and v['connection']=='usb' and v['state']=='online' for p,v in self.events))

    async def test_a_shared_egress_pauses_only_the_conflicting_route(self):
        health=self.setup_health()
        with patch.object(network,'inventory',return_value={r.interface:[r.source_ip] for r in ROUTES}), \
                patch.object(network,'verify',new_callable=AsyncMock,return_value=[ROUTES[0].public_ip]):
            await health.check('wifi');await health.check('usb')
        self.assertTrue(health.online['wifi']);self.assertFalse(health.online['usb'])
        self.assertFalse(self.stop.is_set())

    async def test_ambiguous_phone_sources_do_not_stop_wifi(self):
        health=self.setup_health()
        available={ROUTES[0].interface:[ROUTES[0].source_ip],ROUTES[1].interface:['192.0.2.2','192.0.2.3']}
        with patch.object(network,'inventory',return_value=available), \
                patch.object(network,'verify',new_callable=AsyncMock,return_value=[ROUTES[0].public_ip]):
            await health.initial()
        self.assertEqual(health.online,{'wifi':True,'usb':False});self.assertFalse(self.stop.is_set())

    async def test_no_work_left_does_not_wait_for_an_unplugged_phone(self):
        health=self.setup_health()
        with patch.object(network,'inventory',side_effect=AssertionError('must not probe when no work remains')):
            self.assertFalse(await health.wait('usb',needed=lambda:False))

    async def test_inventory_outage_pauses_routes_without_crashing_watcher(self):
        health=self.setup_health();seen=asyncio.Event()
        def emit(phase,**values):
            if phase=='connection_health' and values['state']=='checking':seen.set()
        health.emit=emit
        with patch.object(network,'inventory',side_effect=network.NetworkUnavailable('temporary netlink outage')):
            watcher=asyncio.create_task(health.watch())
            try:
                await asyncio.wait_for(seen.wait(),1)
                self.assertFalse(self.stop.is_set());self.assertIsNone(health.error)
            finally:
                watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)

    async def test_stop_interrupts_an_offline_wait(self):
        health=self.setup_health()
        with patch.object(network,'inventory',return_value={}):
            task=asyncio.create_task(health.wait('usb'))
            await asyncio.sleep(.005);self.stop.set()
            self.assertFalse(await asyncio.wait_for(task,1))


class HotplugRunTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.MultiConnectionTests.asyncSetUp
    write_input=fixture.MultiConnectionTests.write_input
    handler=fixture.MultiConnectionTests.handler
    factory=fixture.MultiConnectionTests.factory
    multi=fixture.MultiConnectionTests.multi

    async def test_all_new_work_can_finish_on_wifi_when_phone_is_missing_at_start(self):
        self.write_input(10)
        result=await self.multi(workers=12,inventory=lambda:{ROUTES[0].interface:[ROUTES[0].source_ip]})
        self.assertEqual(result['registered_now'],10);self.assertNotIn('blocked',result)
        self.assertEqual(result['connection_results']['wifi']['registered'],10)
        self.assertEqual(result['connection_results']['usb'].get('registered',0),0)
        self.assertTrue(all(worker%2==1 for worker,_ in self.sent_on))
        self.assertEqual(result['connection_online'],{'wifi':True,'usb':False})

    async def test_phone_unplug_wifi_continues_and_same_workers_resume_after_replug(self):
        self.write_input(30);self.post_delay=.03
        mobile_up=True;dropped=False;wifi_during_pause=0
        disconnected=asyncio.Event();wifi_progress=asyncio.Event()
        events=[]
        class UI:
            def attach(self,directory):pass
            def event(ui,phase,**values):
                nonlocal mobile_up,dropped,wifi_during_pause
                events.append((phase,values))
                if phase=='result' and values.get('state')=='registered':
                    if values.get('connection')=='usb' and not dropped:
                        dropped=True;mobile_up=False;disconnected.set()
                    elif values.get('connection')=='wifi' and dropped and not mobile_up:
                        wifi_during_pause+=1
                        if wifi_during_pause>=3:wifi_progress.set()
        def factory(worker):
            route=ROUTES[(worker-1)%2] if worker else ROUTES[0]
            async def handler(request):
                if route.name=='usb' and not mobile_up:raise httpx.ConnectError('synthetic unplug',request=request)
                if request.method=='POST':self.sent_on.append((worker,await request.aread()))
                return await self.handler(request)
            return single.Client(transport=httpx.MockTransport(handler))
        self.factory=factory
        async def reconnect():
            nonlocal mobile_up
            await asyncio.wait_for(disconnected.wait(),3)
            await asyncio.wait_for(wifi_progress.wait(),3)
            await asyncio.sleep(.015)
            mobile_up=True
        controller=asyncio.create_task(reconnect())
        delay=single.Control.delay
        try:
            with patch.object(single.Control,'delay',lambda c,s:delay(c,min(s,.003))):
                result=await asyncio.wait_for(self.multi(workers=12,retry_uncertain=True,uncertain_retries=2,ui=UI(),
                    inventory=lambda:{r.interface:[r.source_ip] for r in ROUTES if r.name!='usb' or mobile_up}),8)
            await controller
        finally:
            controller.cancel();await asyncio.gather(controller,return_exceptions=True)
        self.assertNotIn('blocked',result);self.assertEqual(result['registered_now'],30)
        self.assertGreaterEqual(wifi_during_pause,3)
        health=[v['state'] for p,v in events if p=='connection_health' and v.get('connection')=='usb']
        self.assertIn('offline',health)
        last_offline=max(i for i,state in enumerate(health) if state=='offline')
        self.assertIn('online',health[last_offline+1:])
        self.assertTrue(result['connection_online']['usb'])
        self.assertEqual(len({p['cnic'] for p in self.posts}),30)
        with opened(self.root/'jobs.sqlite3') as db:
            self.assertTrue(all(j['state']=='registered' for j in jobs(db,'Lahore')))
            self.assertEqual(db.execute('SELECT count(*) FROM registered_applicants').fetchone()[0],30)
