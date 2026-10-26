"""Worker submissions overlap while preserving local pacing and shared backoff."""

import asyncio
from argparse import Namespace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bise_results.parser import parse_result
from bise_results.runner import collect, Pacer, SharedCooldown
from bise_results.store import jobs_for, locked_store
from test_identity_fields import FIXTURE, receipt_for


class PacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_submissions_from_two_workers_are_independent(self):
        cooldown = SharedCooldown()
        first, second = Pacer(3, cooldown), Pacer(3, cooldown)
        await asyncio.wait_for(asyncio.gather(first.wait(), second.wait()), timeout=0.2)
        self.assertGreater(first.next_at, time.monotonic())
        self.assertGreater(second.next_at, time.monotonic())

    async def test_one_worker_still_observes_its_interval(self):
        pacer = Pacer(0.03)
        await pacer.wait()
        first = time.monotonic()
        await pacer.wait()
        self.assertGreaterEqual(time.monotonic() - first, 0.028)

    async def test_error_cooldown_delays_both_workers(self):
        cooldown = SharedCooldown()
        first, second = Pacer(0, cooldown), Pacer(0, cooldown)
        first.backoff(0.03)
        deadline = cooldown.until
        await asyncio.gather(first.wait(), second.wait())
        self.assertGreaterEqual(time.monotonic(), deadline)

    async def test_waiting_workers_recheck_extended_cooldown(self):
        cooldown = SharedCooldown()
        pacer = Pacer(0, cooldown)
        cooldown.backoff(0.04)
        task = asyncio.create_task(pacer.wait())
        await asyncio.sleep(0.01)
        cooldown.backoff(0.06)
        deadline = cooldown.until
        await task
        self.assertGreaterEqual(time.monotonic(), deadline)


class WorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_collect(self, headless=False, workers=2, grade=10):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'rolls.csv'
            source.write_text('Roll Numbers\n' + '\n'.join(str(1234 + i).zfill(6) for i in range(workers)) + '\n')
            args = Namespace(csv=source, school='Parallel test', year=2026, database=root / 'results.sqlite3',
                             retry_unresolved=False, limit=0, workers=workers, interval=3, captcha_mode='auto',
                             artifacts=root / 'artifacts', output=root / 'output', browser_executable=None, headless=headless, grade=grade)
            contexts = [SimpleNamespace(new_page=AsyncMock(return_value=MagicMock()), close=AsyncMock()) for _ in range(workers)]
            browser = SimpleNamespace(new_context=AsyncMock(side_effect=contexts), close=AsyncMock())
            launch = AsyncMock(return_value=browser)
            manager = MagicMock()
            manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=launch)))
            manager.__aexit__ = AsyncMock(return_value=None)
            ready = asyncio.Event()
            arrivals, active, peak = 0, 0, 0
            pacers, starts = [], []

            async def lookup(page, job, folder, options, pacer, operator_lock, progress=None):
                nonlocal arrivals, active, peak
                pacers.append(pacer)
                arrivals += 1
                if arrivals == workers:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 0.3)
                await pacer.wait()
                progress('submitting')
                starts.append(time.monotonic())
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.03)
                active -= 1
                fixture = FIXTURE.with_name('ssc-part1.html') if grade == 9 else FIXTURE
                html = fixture.read_text().replace('001234', job['roll'])
                parsed = parse_result(html, job['roll'], exam_type=job['exam_type'])
                return parsed, receipt_for(folder, html, parsed)

            output = io.StringIO()
            with patch('bise_results.runner.async_playwright', return_value=manager), patch('bise_results.runner.lookup', side_effect=lookup) as lookups, redirect_stdout(output):
                self.assertEqual(await asyncio.wait_for(collect(args), 5), 0)
                args.retry_unresolved = True
                self.assertEqual(await asyncio.wait_for(collect(args), 5), 0)
                self.assertEqual(lookups.call_count, workers)  # Rerun skips every saved roll.
            self.assertEqual(peak, workers)
            self.assertEqual(len({id(pacer) for pacer in pacers}), workers)
            self.assertEqual(len({id(pacer.cooldown) for pacer in pacers}), 1)
            self.assertIsNot(pacers[0], pacers[1])
            self.assertIs(pacers[0].cooldown, pacers[1].cooldown)
            self.assertLess(max(starts) - min(starts), 0.2)
            self.assertEqual(browser.new_context.await_count, workers)
            launch.assert_awaited_once_with(headless=headless)
            phases = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
            self.assertEqual({line['worker'] for line in phases if line.get('phase') == 'submitting'}, set(range(1, workers + 1)))
            with locked_store(args.database) as db:
                rows = db.execute('SELECT * FROM jobs').fetchall()
                self.assertEqual(len(rows), workers)
                self.assertTrue(all(row['state'] == 'verified' and row['attempts'] == 1 for row in rows))
                self.assertTrue(all(row['exam_type'] == ('1' if grade == 9 else '2') for row in rows))
                self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], workers)

    async def test_collect_runs_distinct_workers_concurrently_and_commits_once(self):
        await self.exercise_collect()

    async def test_headless_runs_three_parallel_workers_with_same_commit_contract(self):
        await self.exercise_collect(headless=True, workers=3)

    async def test_nine_headless_workers_process_each_job_once(self):
        await self.exercise_collect(headless=True, workers=9)

    async def test_eighteen_headless_workers_process_9th_once_and_resume_without_refetch(self):
        await self.exercise_collect(headless=True, workers=18, grade=9)

    async def test_eighteen_headless_workers_process_10th_once_and_resume_without_refetch(self):
        await self.exercise_collect(headless=True, workers=18)

    async def test_invalid_worker_counts_do_not_change_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'must-not-exist.sqlite3'
            for workers in (0, -1, 19):
                with self.subTest(workers=workers), self.assertRaisesRegex(ValueError, 'between 1 and 18'):
                    await collect(Namespace(headless=True, captcha_mode='auto', workers=workers, database=database))
                self.assertFalse(database.exists())

    async def test_headless_assist_is_rejected_before_any_state_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'must-not-exist.sqlite3'
            with self.assertRaisesRegex(ValueError, '--captcha-mode auto'):
                await collect(Namespace(headless=True, captcha_mode='assist', database=path))
            self.assertFalse(path.exists())


class WorkerCliTests(unittest.TestCase):
    def test_cli_accepts_nine_headless_workers(self):
        from bise_results.__main__ import main
        arguments = ['bise_results', 'collect', 'rolls.csv', '--workers', '9', '--headless', '--captcha-mode', 'auto']
        with patch('sys.argv', arguments), patch('bise_results.runner.collect', new_callable=AsyncMock, return_value=0) as run:
            self.assertEqual(main(), 0)
        self.assertEqual(run.await_args.args[0].workers, 9)
        self.assertTrue(run.await_args.args[0].headless)

    def test_cli_accepts_eighteen_workers_for_9th(self):
        from bise_results.__main__ import main
        arguments = ['bise_results', 'collect', 'rolls.csv', '--class', '9', '--workers', '18', '--headless', '--captcha-mode', 'auto']
        with patch('sys.argv', arguments), patch('bise_results.runner.collect', new_callable=AsyncMock, return_value=0) as run:
            self.assertEqual(main(), 0)
        self.assertEqual(run.await_args.args[0].workers, 18)
        self.assertEqual(run.await_args.args[0].grade, 9)


if __name__ == '__main__':
    unittest.main()
