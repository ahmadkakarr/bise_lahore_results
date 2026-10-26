"""Headless worker integration: real Chromium, synthetic inputs, intercepted traffic."""

import asyncio
from contextlib import asynccontextmanager, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock

from PIL import Image
from playwright.async_api import async_playwright

from youthgames_register import app, browser
from youthgames_register.config import load, validate_browser
from youthgames_register.store import opened, import_batch, jobs, check_receipt
from tests.test_youthgames import applicant, config, catalog, fixture, SUCCESS, REJECTED


class WorkerSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_overrides_and_unattended_assist_fail_before_state_changes(self):
        for overrides in ({'workers': 0}, {'workers': 37}, {'workers': True}, {'headless': 'yes'},
                          {'workers': 2, 'captcha_mode': 'assist'}, {'headless': True, 'captcha_mode': 'assist'}):
            with self.subTest(overrides=overrides), patch.object(app, 'load', return_value=config()), patch.object(app, 'opened') as opened_mock, patch.object(browser, 'visible_browser') as launch:
                with self.assertRaises(ValueError):
                    await app.run(Path('/tmp/unused-profile.json'), 'submit', **overrides)
                opened_mock.assert_not_called(); launch.assert_not_called()

    async def test_profiles_accept_boolean_visibility_and_one_to_thirty_six_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            for count in (1, 3, 9, 18, 20, 36):
                settings = config(); settings['browser'].update(headless=True, workers=count)
                path.write_text(json.dumps(settings))
                self.assertEqual(load(path)['browser']['workers'], count)
            for count in (0, 37, True, '3'):
                settings['browser']['workers'] = count
                with self.assertRaises(ValueError): validate_browser(settings['browser'])

    async def test_cli_passes_visibility_workers_limit_and_mode(self):
        import sys
        from youthgames_register.__main__ import main
        # main owns its event loop, so exercise it in a thread for this CLI-only test.
        for flag, expected in (('--headless', True), ('--headed', False)):
            runner = AsyncMock(return_value={'outcomes': []})
            args = ['youthgames_register', 'submit', '--config', 'profile.json', '--workers', '9', flag, '--captcha-mode', 'auto', '--limit', '17','--no-diagnostics']
            with patch.object(sys, 'argv', args), patch('youthgames_register.__main__.run', runner), patch('youthgames_register.__main__.load', return_value={'state': {'output': '/tmp/reports'}}), redirect_stdout(io.StringIO()):
                self.assertEqual(await asyncio.to_thread(main), 0)
            self.assertEqual(runner.call_args.kwargs, {'workers': 9, 'headless': expected, 'captcha_mode': 'auto'})
            self.assertEqual(runner.call_args.args[2], 17)


class WorkerBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.photo = self.root / 'synthetic.jpeg'; Image.new('RGB', (3, 3), 'white').save(self.photo)
        self.item = applicant(self.photo)
        self.settings = config()
        self.settings['state'] = {'database': str(self.root / 'jobs.sqlite3'), 'output': str(self.root / 'reports')}
        self.posts, self.contexts, self.gets = [], [], {}
        self.active = self.peak = 0
        self.barrier = asyncio.Event()
        self.overlap = 1
        self.abort_context = None
        self.reject_once = False
        self.rejection_html = REJECTED
        self.http_status = 200
        self.manager = await async_playwright().start()
        self.chromium = await self.manager.chromium.launch(headless=True)
        self.addAsyncCleanup(self.manager.stop); self.addAsyncCleanup(self.chromium.close)

    def applicants(self, count):
        rows = []
        for number in range(count):
            row = deepcopy(self.item)
            row['roll'] = f'{number + 1:06d}'
            row['data'].update(FullName='Synthetic Student', FatherName='Synthetic Father', BForm=f'{number:012d}2')
            rows.append(row)
        return rows

    def proxy(self):
        owner = self
        class Context:
            def __init__(self, real, index): self.real, self.index = real, index
            async def route(self, pattern, handler):
                index = self.index
                class Route:
                    def __init__(self, route): self.real, self.request = route, route.request
                    async def abort(self): await self.real.abort()
                    async def continue_(self):
                        if self.request.method == 'POST':
                            owner.posts.append({'context': index, 'body': self.request.post_data, 'time': time.monotonic()})
                            owner.active += 1; owner.peak = max(owner.peak, owner.active)
                            if owner.active >= owner.overlap: owner.barrier.set()
                            try:
                                await asyncio.wait_for(owner.barrier.wait(), timeout=10)
                                await asyncio.sleep(0.1)
                                if index == owner.abort_context:
                                    await self.real.abort()
                                else:
                                    response = SUCCESS.replace('TEST-YG-123', f'TEST-YG-{len(owner.posts)}-{index}')
                                    if owner.reject_once:
                                        owner.reject_once = False; response = owner.rejection_html
                                    await self.real.fulfill(status=owner.http_status, content_type='text/html', body=response)
                            finally:
                                owner.active -= 1
                        else:
                            owner.gets[index] = owner.gets.get(index, 0) + 1
                            html = fixture().replace('synthetic-csrf', f'csrf-{index}-{owner.gets[index]}')
                            await self.real.fulfill(status=200, content_type='text/html', body=html)
                await self.real.route(pattern, lambda route: handler(Route(route)))
            async def new_page(self): return await self.real.new_page()
            async def close(self): await self.real.close()
        class Browser:
            async def new_context(self, **kwargs):
                real = await owner.chromium.new_context(**kwargs)
                owner.contexts.append(real)
                return Context(real, len(owner.contexts))
        return Browser()

    async def run_batch(self, rows, workers=3, limit=0, prepare=True, **kwargs):
        @asynccontextmanager
        async def launch(headless=False):
            self.assertTrue(headless)
            yield self.proxy()
        async def prepared(db, *args, **kwargs):
            if prepare:
                import_batch(db, self.settings, {'csv': 'synthetic'}, catalog(), self.root, rows)
            return {'counts': {'total': len(rows)}}
        with patch.object(app, 'load', return_value=deepcopy(self.settings)), patch.object(browser, 'visible_browser', launch), patch.object(app, 'prepare_in', prepared), patch.object(browser, 'assisted_answer', AsyncMock(side_effect=AssertionError('must not prompt'))), redirect_stdout(io.StringIO()):
            return await app.run(self.root / 'profile.json', 'submit', workers=workers, headless=True, limit=limit, **kwargs)

    async def test_three_workers_overlap_reuse_sessions_skip_registered_and_share_limit(self):
        rows = self.applicants(8)
        self.overlap = 3
        result = await self.run_batch(rows, limit=6)
        self.assertEqual(result['attempted_now'], 6)
        self.assertEqual(result['counts'], {'registered': 6, 'ready': 2})
        self.assertEqual(self.peak, 3)
        self.assertEqual(len(self.contexts), 3)
        self.assertEqual(sorted(self.gets.values()), [2, 2, 2])
        self.assertEqual(len(self.posts), 6)
        for index in range(1, 4):
            posts = [p for p in self.posts if p['context'] == index]
            self.assertEqual(len(posts), 2)
            self.assertGreaterEqual(posts[1]['time'] - posts[0]['time'], 5)
            for sequence, post in enumerate(posts, 1):
                self.assertIn(f'csrf-{index}-{sequence}', post['body'])
        with opened(self.root / 'jobs.sqlite3') as db:
            receipts = {r['id']: Path(r['receipt']).read_bytes() for r in jobs(db, 'Lahore', ('registered',))}
            self.assertTrue(all(check_receipt(r['receipt']) for r in jobs(db, 'Lahore', ('registered',))))
            self.assertEqual(db.execute('SELECT COUNT(DISTINCT job) FROM attempts').fetchone()[0], 6)
        self.overlap = 1
        result = await self.run_batch(rows, limit=1)
        self.assertEqual(result['counts'], {'registered': 7, 'ready': 1})
        self.assertEqual(len(self.posts), 7)
        with opened(self.root / 'jobs.sqlite3') as db:
            for row in jobs(db, 'Lahore', ('registered',)):
                if row['id'] in receipts: self.assertEqual(Path(row['receipt']).read_bytes(), receipts[row['id']])
        self.assertEqual(self.chromium.contexts, [])

    async def test_nine_headless_workers_submit_unique_payloads_concurrently(self):
        self.overlap = 9
        rows = self.applicants(12)
        result = await self.run_batch(rows, workers=9, limit=9)
        self.assertEqual(result['counts'], {'registered': 9, 'ready': 3})
        self.assertEqual(self.peak, 9)
        self.assertEqual(len(self.contexts), 9)
        self.assertEqual(len(self.posts), 9)
        for row in rows[:9]:
            value = row['data']['BForm']
            wire = f'{value[:5]}-{value[5:12]}-{value[12]}'
            self.assertEqual(sum(wire in post['body'] for post in self.posts), 1)
        with opened(self.root / 'jobs.sqlite3') as db:
            self.assertEqual(db.execute('SELECT COUNT(DISTINCT job),COUNT(*) FROM attempts').fetchone()[:], (9, 9))
            self.assertTrue(all(check_receipt(r['receipt']) for r in jobs(db, 'Lahore', ('registered',))))
        self.assertEqual(self.chromium.contexts, [])

    async def test_eighteen_workers_submit_once_per_applicant_and_preserve_tracking(self):
        self.overlap = 18
        rows = self.applicants(20)
        result = await self.run_batch(rows, workers=18, limit=20)
        self.assertEqual(result['counts'], {'registered': 20})
        self.assertEqual(self.peak, 18)
        self.assertEqual(len(self.contexts), 18)
        self.assertEqual(len(self.posts), 20)
        with opened(self.root / 'jobs.sqlite3') as db:
            saved = jobs(db, 'Lahore', ('registered',))
            self.assertEqual(len(saved), 20)
            self.assertTrue(all(row['tracking'] and check_receipt(row['receipt']) for row in saved))
            self.assertEqual(db.execute('SELECT count(DISTINCT job),count(*) FROM attempts').fetchone()[:], (20,20))
        result = await self.run_batch(rows, workers=18, limit=20)
        self.assertEqual(result['attempted_now'], 0)
        self.assertEqual(len(self.posts), 20)
        self.assertEqual(self.chromium.contexts, [])

    async def test_twenty_headless_workers_with_diagnostic_network_receipts(self):
        self.overlap=20;events=[]
        def emit(phase,**values):events.append({'phase':phase,**values})
        result=await self.run_batch(self.applicants(20),workers=20,limit=20,emit=emit)
        self.assertEqual(result['counts'],{'registered':20});self.assertEqual(self.peak,20)
        self.assertTrue(any(e['phase']=='browser_response' for e in events))
        self.assertEqual(sum(e['phase']=='awaiting_response' for e in events),20)
        with opened(self.root/'jobs.sqlite3') as db:
            for saved in jobs(db,'Lahore'):
                evidence=json.loads(Path(saved['receipt']).read_text())
                self.assertIn('browser-network.json',evidence['files'])
                self.assertTrue(check_receipt(saved['receipt']))

    async def test_explicit_server_throttle_stops_browser_queue_and_preserves_receipt(self):
        self.reject_once=True
        self.rejection_html='<form id="registration-form" data-has-server-errors="1"><div id="registration-errors">Too many registration attempts. Please wait 48 seconds and try again.</div></form>'
        events=[]
        result=await self.run_batch(self.applicants(3),workers=1,limit=3,
            emit=lambda phase,**values:events.append({'phase':phase,**values}))
        self.assertEqual(len(self.posts),1)
        self.assertEqual(result['counts'],{'rejected':1,'ready':2})
        self.assertEqual(result['stop_reason'],'server_rate_limit_detected')
        self.assertTrue(any(e['phase']=='browser_throttle' for e in events))
        with opened(self.root/'jobs.sqlite3') as db:
            saved=jobs(db,'Lahore',('rejected',))[0]
            self.assertTrue(check_receipt(saved['receipt']))

    async def test_ambiguous_post_stops_queue_preserves_inflight_receipts_and_blocks_rerun(self):
        self.overlap = 3; self.abort_context = 1
        rows = self.applicants(8)
        result = await self.run_batch(rows)
        self.assertEqual(result['attempted_now'], 3)
        self.assertEqual(result['counts'], {'uncertain': 1, 'registered': 2, 'ready': 5})
        self.assertEqual(len(self.posts), 3)
        with opened(self.root / 'jobs.sqlite3') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 3)
        result = await self.run_batch(rows)
        self.assertEqual(result['attempted_now'], 0)
        self.assertEqual(result['blocked'], 'uncertain_submission_requires_reconciliation')
        self.assertEqual(len(self.posts), 3)
        self.assertEqual(self.chromium.contexts, [])

    async def test_shared_stop_leaves_filled_but_unarmed_worker_ready(self):
        control = app.RunControl()
        async def stop_before_intent(form, job):
            answer = await browser.automatic_answer(form, job)
            control.outcome('uncertain')
            return answer
        with opened(self.root / 'jobs.sqlite3') as db:
            import_batch(db, self.settings, {'csv': 'synthetic'}, catalog(), self.root, [self.item])
            result = await app.process_one(db, self.proxy(), self.settings, jobs(db, 'Lahore')[0], self.root / 'reports', answer_provider=stop_before_intent, control=control)
            self.assertEqual(result['state'], 'ready')
            attempt = db.execute('SELECT * FROM attempts').fetchone()
            self.assertIsNone(attempt['intent_at']); self.assertIsNone(attempt['dispatched_at'])
            self.assertEqual(self.posts, [])

    async def test_shared_stop_inside_post_gate_keeps_undispatched_student_ready(self):
        control = app.RunControl()
        original = app.intent
        def stop_after_intent(*args):
            original(*args)
            control.reason = 'server_rate_limit_detected'; control.stopped.set()
        with opened(self.root / 'jobs.sqlite3') as db:
            import_batch(db, self.settings, {'csv': 'synthetic'}, catalog(), self.root, [self.item])
            with patch.object(app, 'intent', side_effect=stop_after_intent):
                result = await app.process_one(db, self.proxy(), self.settings, jobs(db, 'Lahore')[0],
                    self.root / 'reports', control=control)
            self.assertEqual(result['state'], 'ready'); self.assertFalse(result['post_dispatched'])
            self.assertEqual(self.posts, [])
            self.assertIsNone(db.execute('SELECT dispatched_at FROM attempts').fetchone()[0])
            self.assertEqual(db.execute('SELECT count(*) FROM submission_recoveries').fetchone()[0], 1)
            self.assertTrue(check_receipt(jobs(db, 'Lahore')[0]['receipt']))

    async def test_explicit_captcha_rejection_retries_fresh_form_only(self):
        self.reject_once = True
        result = await self.run_batch(self.applicants(1), workers=3)
        self.assertEqual(result['counts'], {'registered': 1})
        self.assertEqual(result['attempted_now'], 1)
        self.assertEqual(len(self.contexts), 1)
        self.assertEqual(len(self.posts), 2)
        self.assertGreaterEqual(self.posts[1]['time'] - self.posts[0]['time'], 5)
        self.assertIn('csrf-1-1', self.posts[0]['body']); self.assertIn('csrf-1-2', self.posts[1]['body'])
        with opened(self.root / 'jobs.sqlite3') as db:
            self.assertEqual([r[0] for r in db.execute('SELECT outcome FROM attempts ORDER BY id')], ['rejected', 'registered'])

    async def test_stop_between_intent_and_dispatch_returns_ready_with_original_intent_audit(self):
        control = app.RunControl()
        original_intent = app.intent
        def stop_after_intent(*args):
            original_intent(*args)
            control.outcome('uncertain')
        with opened(self.root / 'jobs.sqlite3') as db:
            import_batch(db, self.settings, {'csv': 'synthetic'}, catalog(), self.root, [self.item])
            with patch.object(app, 'intent', stop_after_intent), redirect_stdout(io.StringIO()):
                result = await app.process_one(db, self.proxy(), self.settings, jobs(db, 'Lahore')[0], self.root / 'reports', control=control)
            self.assertEqual(result['state'], 'ready')
            self.assertFalse(result['post_dispatched'])
            attempt = db.execute('SELECT * FROM attempts').fetchone()
            self.assertIsNotNone(attempt['intent_at']); self.assertIsNone(attempt['dispatched_at'])
            self.assertEqual(self.posts, [])

    async def test_rate_limited_response_is_uncertain_and_never_retried(self):
        self.overlap = 3; self.http_status = 429
        result = await self.run_batch(self.applicants(6))
        self.assertEqual(result['counts'], {'uncertain': 3, 'ready': 3})
        self.assertEqual(len(self.posts), 3)

    async def test_cancel_parallel_batch_finishes_db_cleanup_before_connection_close(self):
        entered = 0
        all_filled = asyncio.Event()
        async def wait_answer(form, job):
            nonlocal entered
            entered += 1
            if entered == 3: all_filled.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(self.run_batch(self.applicants(6), answer_provider=wait_answer))
        await asyncio.wait_for(all_filled.wait(), 15)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.posts, [])
        self.assertEqual(self.chromium.contexts, [])
        with opened(self.root / 'jobs.sqlite3') as db:
            self.assertEqual(len(jobs(db, 'Lahore', ('ready',))), 6)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts WHERE outcome IS NULL').fetchone()[0], 0)
