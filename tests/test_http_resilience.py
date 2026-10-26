"""Pure local pacing persistence checks; no portal or browser access."""

from datetime import datetime,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from youthgames_register.http_async import Control,BASE
from youthgames_register.store import opened


class DurableBackoffTests(unittest.TestCase):
    def test_simultaneous_refusals_coalesce_and_later_round_increases_wait_and_spacing(self):
        control=Control()
        with patch('youthgames_register.http_async.time.monotonic',return_value=100):
            self.assertEqual(control.backoff(3,'throttle',throttle=True),15)
            self.assertEqual(control.backoff(4,'throttle',throttle=True),15)
        self.assertEqual(control.retry_round,1);self.assertEqual(control.throttle_round,1)
        self.assertEqual(control.submission_interval,7.5)
        with patch('youthgames_register.http_async.time.monotonic',return_value=116):
            self.assertEqual(control.backoff(4,'throttle',throttle=True),30)
        self.assertEqual(control.retry_round,2);self.assertEqual(control.submission_interval,11.25)

    def test_backoff_wait_caps_locally_but_never_truncates_longer_server_wait(self):
        control=Control()
        for number in range(10):
            with patch('youthgames_register.http_async.time.monotonic',return_value=number*1000):
                delay=control.backoff(0,'throttle',throttle=True)
                self.assertLessEqual(delay,300)
        self.assertEqual(delay,300);self.assertEqual(control.submission_interval,60)
        with patch('youthgames_register.http_async.time.monotonic',return_value=11000):
            self.assertEqual(control.backoff(902,'server_requested_long_wait',throttle=True),902)

    def test_database_restart_restores_cooldown_and_slower_pacing(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.sqlite3'
            with opened(path) as db:
                control=Control();control.bind(db,BASE,'campaign')
                control.backoff(49,'explicit_server_throttle',throttle=True)
                row=dict(db.execute('SELECT * FROM portal_pacing').fetchone())
                self.assertGreater((datetime.fromisoformat(row['not_before'])-datetime.now(timezone.utc)).total_seconds(),45)
            with opened(path) as db:
                restored=Control(submission_interval=5);restored.bind(db,BASE,'campaign')
                self.assertEqual(restored.submission_interval,7.5)
                self.assertEqual(restored.retry_round,1)
                self.assertGreater(restored.deadline,0)
                self.assertGreater(restored.retry_episode_until,0)
                other=Control();other.bind(db,BASE,'another-campaign')
                self.assertEqual(other.submission_interval,5);self.assertEqual(other.deadline,0)

    def test_bounded_opt_out_retains_existing_delay_without_adaptive_state(self):
        control=Control();control.auto_recover=False
        with patch.object(control,'delay') as delay:
            self.assertEqual(control.backoff(49,'explicit_server_throttle',throttle=True),49)
        delay.assert_called_once_with(49)
        self.assertEqual(control.retry_round,0);self.assertEqual(control.submission_interval,5)
