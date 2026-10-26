"""Offline regressions for explicit refusals and a guard-blocked browser POST."""

import json
from pathlib import Path
import tempfile
import unittest

from youthgames_register.browser_recovery import recover_browser_jobs, recover_stopped_dispatches, STOP_DETAIL
from youthgames_register.config import sha
from youthgames_register.domain import classify
from youthgames_register.http_recovery import recover_rate_limited
from youthgames_register.store import opened, import_batch, jobs, begin, intent, dispatched, finish, check_receipt, now
from tests.test_youthgames import applicant, config, catalog


THROTTLED = '<form id="registration-form" data-has-server-errors="1"><div id="registration-errors">Too many registration attempts. Please wait 48 seconds and try again.</div></form>'


class BrowserRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.path = self.root / 'jobs.sqlite3'
        self.settings = config()
        image = self.root / 'image.jpeg'; image.write_bytes(b'synthetic image')
        self.item = applicant(image)

    def save_attempt(self, db, *, html=THROTTLED, state='rejected', sent=True, transport=None, detail=None):
        import_batch(db, self.settings, {'csv': 'synthetic'}, catalog(), self.root, [self.item])
        job = jobs(db, self.settings['profile'])[0]
        attempt = begin(db, job); intent(db, job['id'], attempt)
        if sent: dispatched(db, job['id'], attempt)
        body = self.root / 'response.html'; body.write_text(html)
        outcome = classify(html, 200) if state == 'rejected' else {'state': state, 'tracking': None, 'detail': detail}
        receipt = {'application': 'youthgames_register', 'job': job['id'], 'attempt': attempt,
            'payload_hash': job['payload_hash'], 'post_dispatched': sent, 'http_status': 200 if sent else 0,
            'outcome': outcome, 'captured_at': now(),
            'files': {'response.html': {'path': str(body), 'sha256': sha(body.read_bytes())}}}
        if transport is not None: receipt['transport'] = transport
        path = self.root / 'receipt.json'; path.write_text(json.dumps(receipt))
        finish(db, job['id'], attempt, state, outcome['detail'], path)
        return job, path

    def test_legacy_and_current_browser_throttle_recovery_honors_cooldown_and_is_idempotent(self):
        for transport in (None, 'playwright'):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as folder, opened(Path(folder) / 'db.sqlite3') as db:
                # Each synthetic attempt needs its own immutable receipt folder,
                # matching production; reusing one overwrites prior evidence.
                self.root=Path(folder)/'receipts';self.root.mkdir()
                job, path = self.save_attempt(db, transport=transport)
                original = path.read_bytes()
                # HTTP's existing default does not silently broaden its scope.
                self.assertEqual(recover_rate_limited(db, self.settings), [])
                result = recover_browser_jobs(db, self.settings)
                self.assertEqual(len(result['recovered_throttles']), 1)
                self.assertGreater(result['cooldown_seconds'], 45)
                self.assertEqual(result['applicant_posts'], 0)
                self.assertEqual(jobs(db, self.settings['profile'])[0]['state'], 'ready')
                self.assertTrue(check_receipt(jobs(db, self.settings['profile'])[0]['receipt']))
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(recover_browser_jobs(db, self.settings)['recovered_throttles'], [])
                self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)

    def test_exact_shared_guard_stop_without_dispatch_is_recovered_once(self):
        with opened(self.path) as db:
            _, path = self.save_attempt(db, state='blocked_input', sent=False, detail=STOP_DETAIL)
            original = path.read_bytes()
            self.assertEqual(len(recover_stopped_dispatches(db, self.settings)), 1)
            self.assertEqual(recover_stopped_dispatches(db, self.settings), [])
            self.assertEqual(jobs(db, self.settings['profile'])[0]['state'], 'ready')
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(db.execute('SELECT outcome FROM attempts').fetchone()[0], 'blocked_input')

    def test_dispatched_unknown_or_generic_error_never_recovers(self):
        for sent, state, detail in ((True, 'blocked_input', STOP_DETAIL),
                                    (True, 'uncertain', STOP_DETAIL),
                                    (False, 'blocked_input', 'TimeoutError: navigation failed')):
            with self.subTest(sent=sent, state=state, detail=detail), tempfile.TemporaryDirectory() as folder, opened(Path(folder) / 'db.sqlite3') as db:
                self.save_attempt(db, state=state, sent=sent, detail=detail)
                result = recover_browser_jobs(db, self.settings)
                self.assertEqual(result['recovered_stopped_dispatches'], [])
                self.assertEqual(result['recovered_throttles'], [])
                self.assertEqual(jobs(db, self.settings['profile'])[0]['state'], state)

    def test_damaged_throttle_receipt_never_recovers(self):
        with opened(self.path) as db:
            self.save_attempt(db)
            (self.root / 'response.html').write_text('damaged')
            self.assertEqual(recover_browser_jobs(db, self.settings)['recovered_throttles'], [])
            self.assertEqual(jobs(db, self.settings['profile'])[0]['state'], 'rejected')
