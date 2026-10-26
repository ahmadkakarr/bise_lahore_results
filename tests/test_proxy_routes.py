"""Fixed proxy opt-in, independent recovery, and durable no-duplicate behavior.

All transports, routes and applicant records below are synthetic. No live POST.
"""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from rich.console import Console

from youthgames_register import connections as network, http_async as single, http_multi
from youthgames_register.proxy_routes import ProxyConnection, load_proxy
from youthgames_register.route_health import RouteHealth
from youthgames_register.store import opened, jobs
from youthgames_register.terminal import Dashboard
from tests import test_http_connections as fixture

ROUTES = fixture.ROUTES
PROXY = ProxyConnection('pakistan_proxy', 'http://1.0.0.1:8080', 5, '9.9.9.9')


class ProxyConfigurationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'proxy.json'
        self.config = {'schema_version': 1, 'name': 'pakistan_proxy', 'url': PROXY.proxy_url,
                       'expected_public_ip': PROXY.public_ip, 'dispatch_interval_seconds': 5}
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.config))

    def test_opt_in_only_and_unique_identity(self):
        with patch.object(network, 'load', return_value=ROUTES), patch(
                'youthgames_register.proxy_routes.load_proxy', wraps=load_proxy) as load:
            self.assertEqual(network.load_routes('synthetic.json'), ROUTES)
            load.assert_not_called()
            routes = network.load_routes('synthetic.json', self.path)
        self.assertEqual(len(routes), 3); self.assertEqual(routes[-1], PROXY)
        self.assertEqual(PROXY.label, 'Pakistan Proxy')
        self.assertNotIn(PROXY.proxy_url, json.dumps(PROXY.public()))
        self.assertNotEqual(PROXY.key, ROUTES[0].key)
        self.assertEqual(replace(PROXY, name='renamed', public_ip='4.2.2.2').key, PROXY.key)
        self.assertNotEqual(replace(PROXY, proxy_url='http://1.0.0.1:8081').key, PROXY.key)

    def test_invalid_endpoints_intervals_and_schema_fail_locally(self):
        cases = [('url', u) for u in ('http://127.0.0.1:8080', 'http://192.168.1.1:80',
            'http://169.254.169.254:80', 'http://localhost:80', 'socks5://1.0.0.1:80',
            'http://user:secret@1.0.0.1:80', 'http://1.0.0.1', 'http://1.0.0.1:0',
            'http://1.0.0.1:99999', 'http://1.0.0.1:80/path', 'http://1.0.0.1:80?x=1',
            'http://1.0.0.1:80#frag', None)]
        cases += [('dispatch_interval_seconds', n) for n in (True, 4, float('nan'), float('inf'))]
        cases += [('schema_version', True), ('expected_public_ip', '192.168.1.1'), ('name', 'bad name')]
        original = dict(self.config)
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.config = {**original, key: value}; self.write()
                with self.assertRaises(ValueError): load_proxy(self.path)

    def test_collision_with_a_device_name_rejected(self):
        self.config['name'] = ROUTES[0].name; self.write()
        with patch.object(network, 'load', return_value=ROUTES):
            with self.assertRaisesRegex(ValueError, 'unique'): network.load_routes('synthetic.json', self.path)

    def test_transport_explicit_proxy_verified_tls_and_no_environment(self):
        with patch('youthgames_register.proxy_routes.httpx.AsyncHTTPTransport') as transport:
            PROXY.transport()
        values = transport.call_args.kwargs
        self.assertEqual(values['proxy'], PROXY.proxy_url)
        self.assertTrue(values['verify']); self.assertFalse(values['trust_env'])
        self.assertEqual(values['retries'], 0); self.assertEqual(values['limits'].max_connections, 1)


class ProxyHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_does_not_require_fake_linux_interface_and_recovers(self):
        events = []; stop = asyncio.Event()
        health = RouteHealth([PROXY], stop, lambda p, **v: events.append((p, v)), poll_seconds=0, retry_seconds=0)
        with patch.object(network, 'inventory', side_effect=AssertionError('proxy is not a device')), patch.object(
                network, 'verify', new_callable=AsyncMock,
                side_effect=[network.NetworkUnavailable('Proxy offline'), [PROXY.public_ip]]) as verify:
            self.assertFalse(await health.check(PROXY.name))
            self.assertFalse(stop.is_set()); self.assertTrue(await health.check(PROXY.name))
        self.assertEqual(verify.await_args.args[0][0].proxy_url, PROXY.proxy_url)
        self.assertEqual([v['state'] for p, v in events if p == 'connection_health'], ['unreachable', 'online'])

    async def test_shared_exit_pauses_proxy_only(self):
        stop = asyncio.Event()
        health = RouteHealth([ROUTES[0], PROXY], stop, lambda *a, **v: None, poll_seconds=0, retry_seconds=0)
        with patch.object(network, 'inventory', return_value={ROUTES[0].interface: [ROUTES[0].source_ip]}), patch.object(
                network, 'verify', new_callable=AsyncMock, return_value=[ROUTES[0].public_ip]):
            self.assertTrue(await health.check('wifi')); self.assertFalse(await health.check(PROXY.name))
        self.assertFalse(stop.is_set()); self.assertTrue(health.online['wifi'])

    async def test_proxy_failure_has_no_direct_fallback(self):
        seen = []
        async def broken(request):
            seen.append(request); raise httpx.ProxyError('synthetic CONNECT failure')
        with patch('youthgames_register.proxy_routes.httpx.AsyncHTTPTransport',
                   return_value=httpx.MockTransport(broken)) as transport:
            async with single.Client(route=PROXY) as client:
                with self.assertRaises(httpx.ProxyError): await client.get()
        self.assertEqual(len(seen), 1); transport.assert_called_once()

    async def test_optional_proxy_cannot_silently_enter_single_route_mode(self):
        with self.assertRaisesRegex(ValueError, 'requires explicit'):
            await single.run('not-read.json', proxy_config='not-read.json')


class ProxyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.MultiConnectionTests.asyncSetUp
    write_input = fixture.MultiConnectionTests.write_input
    handler = fixture.MultiConnectionTests.handler

    async def run_routes(self, enabled=True, ui=None, retry=False):
        routes = [*ROUTES, PROXY] if enabled else ROUTES
        original = single.Control
        def fast(**kwargs):
            kwargs['submission_interval'] = .015
            control = original(**kwargs)
            delay = control.transport_delay
            control.transport_delay = lambda seconds: delay(min(seconds, .002))
            return control
        async def verify(candidates, pinned=False):
            if candidates[0].name == PROXY.name and not getattr(self, 'proxy_online', True):
                raise network.NetworkUnavailable('synthetic proxy offline')
            return [route.public_ip for route in candidates]
        def factory(worker):
            route = routes[(worker - 1) % len(routes)] if worker else ROUTES[0]
            async def handle(request):
                if route.name == PROXY.name and not getattr(self, 'proxy_online', True):
                    raise httpx.ProxyError('synthetic proxy offline')
                response = await self.handler(request)
                if request.method == 'POST' and route.name == PROXY.name and getattr(self, 'lose_proxy_response', False):
                    self.lose_proxy_response = False
                    raise httpx.ReadTimeout('Lost proxy POST response', request=request)
                return response
            return single.Client(transport=httpx.MockTransport(handle))
        def quick_health(*args, **kwargs):
            return RouteHealth(*args, **kwargs, poll_seconds=.002, retry_seconds=.002)
        with patch.object(network, 'load', return_value=ROUTES), patch(
                'youthgames_register.proxy_routes.load_proxy', return_value=PROXY), patch.object(
                network, 'inventory', return_value={r.interface: [r.source_ip] for r in ROUTES}), patch.object(
                network, 'verify', side_effect=verify), patch.object(single, 'Control', side_effect=fast), patch(
                'youthgames_register.http_multi.RouteHealth', side_effect=quick_health):
            return await asyncio.wait_for(http_multi.run(self.profile, 'synthetic.json', workers=6, limit=0,
                client_factory=factory, install_signals=False, prepare_ahead=2, max_in_flight=2, ui=ui,
                retry_uncertain=retry, uncertain_retries=0, proxy_config='proxy.json' if enabled else None), 8)

    async def test_three_routes_dashboard_ledger_and_rerun_skip(self):
        self.write_input(12); self.post_delay = .03
        output = io.StringIO()
        with Dashboard('Synthetic', 6, enabled=False, console=Console(file=output, width=180)) as ui:
            result = await self.run_routes(ui=ui)
            ui.console.print(ui.render())
            self.assertEqual(ui.connection_counts[PROXY.name]['registered'], result['connection_results'][PROXY.name]['registered'])
            progress = json.loads((Path(result['report']).parent / 'Progress.json').read_text())
        self.assertEqual(result['registered_now'], 12); self.assertEqual(result['connections'], 3)
        self.assertEqual(result['workers_by_connection'], {'wifi': 2, 'usb': 2, PROXY.name: 2})
        self.assertTrue(all(result['connection_results'][r.name].get('registered', 0) > 0 for r in [*ROUTES, PROXY]))
        self.assertIn('Pakistan Proxy', output.getvalue())
        self.assertEqual(progress['connections'][PROXY.name]['label'], 'Pakistan Proxy')
        self.assertGreater(progress['connections'][PROXY.name]['counts']['registered'], 0)
        with opened(self.root / 'jobs.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(DISTINCT connection_key) FROM attempt_connections').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM registered_applicants').fetchone()[0], 12)
        before = len(self.posts)
        again = await self.run_routes(enabled=False)
        self.assertEqual(len(self.posts), before); self.assertEqual(again['skipped_registered'], 12)

    async def test_proxy_offline_does_not_block_device_workers(self):
        self.write_input(12); self.proxy_online = False
        result = await self.run_routes()
        self.assertEqual(result['registered_now'], 12)
        self.assertFalse(result['connection_online'][PROXY.name]); self.assertNotIn('blocked', result)
        self.assertEqual(result['connection_results'][PROXY.name].get('submitted', 0), 0)

    async def test_proxy_rejoins_without_restart_while_wifi_and_usb_continue(self):
        self.write_input(30); self.proxy_online = False; self.post_delay = .02
        events = []; finished_on_devices = 0
        class UI:
            def attach(inner, directory): pass
            def event(inner, phase, **values):
                nonlocal finished_on_devices
                events.append((phase, values))
                if phase == 'result' and values.get('connection') != PROXY.name:
                    finished_on_devices += 1
                    if finished_on_devices == 3: self.proxy_online = True
        result = await self.run_routes(ui=UI())
        self.assertEqual(result['registered_now'], 30)
        self.assertGreater(result['connection_results'][PROXY.name]['registered'], 0)
        states = [v['state'] for p, v in events if p == 'connection_health' and v['connection'] == PROXY.name]
        self.assertEqual(states[:2], ['unreachable', 'online'])

    async def test_disabling_proxy_defers_its_unknown_post_instead_of_changing_route(self):
        self.write_input(6); self.lose_proxy_response = True
        first = await self.run_routes(retry=True)
        self.assertEqual(first['outcomes'].get('uncertain'), 1)
        before = len(self.posts)
        second = await self.run_routes(enabled=False, retry=True)
        self.assertEqual(len(self.posts), before)
        self.assertEqual(second['deferred_disabled_connections'], 1)
        self.assertEqual(second['status'], 'complete_with_issues')
        third = await self.run_routes(retry=True)
        self.assertEqual(third['registered_now'], 1)
        self.assertEqual(third['connection_results'][PROXY.name]['registered'], 1)
        with opened(self.root / 'jobs.sqlite3') as db:
            job = db.execute('SELECT job FROM attempts GROUP BY job HAVING count(*)=2').fetchone()[0]
            keys = [r[0] for r in db.execute('SELECT c.connection_key FROM attempts a JOIN attempt_connections c ON c.attempt=a.id WHERE a.job=?', (job,))]
            self.assertEqual(keys, [PROXY.key, PROXY.key])
            self.assertTrue(all(j['state'] == 'registered' for j in jobs(db, 'Lahore')))


class ProxyCliTests(unittest.TestCase):
    def test_opt_in_counts_proxy_workers_and_passes_profile(self):
        from bise_allocate.__main__ import main
        with patch.object(sys, 'argv', ['bise_allocate', 'http-submit', '--config', 'allocation.json',
                '--connections', 'connections.json', '--pakistan-proxy', '--workers-per-connection', '8', '--json']), patch(
                'bise_allocate.app.load', return_value={'scheme': 'Synthetic'}), patch(
                'bise_allocate.app.paths', return_value={'registration_profile': Path('/tmp/synthetic.json'),
                    'allocation_database':Path('/tmp/synthetic-allocations.sqlite3')}), patch(
                'bise_allocate.participation.current', return_value=None), patch(
                'bise_allocate.resume.prepare', return_value={}), patch('bise_allocate.app.progress', return_value={}), patch(
                'bise_allocate.app.batch_progress', return_value={}), patch.object(network, 'load', return_value=ROUTES), patch(
                'youthgames_register.proxy_routes.load_proxy', return_value=PROXY), patch.object(
                single, 'run', new_callable=AsyncMock, return_value={'status': 'complete'}) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertEqual(run.call_args.kwargs['workers'], 24)
        self.assertEqual(run.call_args.kwargs['proxy_config'], Path('connection_profiles/pakistan-proxy.json'))

    def test_missing_opt_in_or_connections_rejected_before_any_preparation(self):
        from bise_allocate.__main__ import main
        for flags in (['--proxy-config', 'proxy.json'], ['--pakistan-proxy']):
            with self.subTest(flags=flags), patch.object(sys, 'argv', ['bise_allocate', 'http-submit',
                    '--config', 'allocation.json', '--json', *flags]), patch(
                    'bise_allocate.resume.prepare') as prepare, redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
                prepare.assert_not_called()
