"""Cancellable manual input and explicit CAPTCHA CLI override."""
import asyncio
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch, AsyncMock

from youthgames_register.browser import terminal_line


@unittest.skipUnless(os.name == 'posix', 'Linux selectable terminal input')
class TerminalInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_keeps_following_line_for_next_prompt_and_handles_eof(self):
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd, 'r') as stdin, patch('sys.stdin', stdin), redirect_stdout(io.StringIO()):
                os.write(write_fd, b'13\n/skip\n')
                self.assertEqual(await terminal_line('answer: '), '13')
                self.assertEqual(await terminal_line('answer: '), '/skip')
                os.close(write_fd); write_fd = None
                with self.assertRaises(EOFError):
                    await terminal_line('answer: ')
        finally:
            if write_fd is not None: os.close(write_fd)

    async def test_cancellation_with_partial_line_does_not_leave_a_reader_or_executor(self):
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd, 'r') as stdin, patch('sys.stdin', stdin), redirect_stdout(io.StringIO()):
                task = asyncio.create_task(terminal_line('answer: '))
                os.write(write_fd, b'1')
                await asyncio.sleep(0.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
                self.assertIsNone(asyncio.get_running_loop()._default_executor)
                self.assertFalse(asyncio.get_running_loop().remove_reader(read_fd))
        finally:
            os.close(write_fd)


@unittest.skipUnless(os.name == 'posix', 'Linux SIGINT behavior')
class ShutdownTests(unittest.TestCase):
    def test_sigint_exits_even_when_stdin_remains_open_without_shutdown_thread_trace(self):
        script = '''import asyncio,sys
from youthgames_register.browser import terminal_line
try:
    asyncio.run(terminal_line("READY\\n"))
except KeyboardInterrupt:
    print("STOPPED")
    sys.exit(130)
'''
        child = subprocess.Popen([sys.executable, '-u', '-c', script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0])
            self.assertEqual(child.stdout.readline().strip(), 'READY')
            child.send_signal(signal.SIGINT)
            self.assertEqual(child.wait(timeout=5), 130)
            out, err = child.communicate(timeout=1)
            self.assertIn('STOPPED', out)
            self.assertNotIn('threading shutdown', err)
            self.assertNotIn('Traceback', err)
        finally:
            if child.poll() is None: child.kill()
            child.communicate()

    def test_submit_cli_passes_mode_override_through_to_runner(self):
        from youthgames_register.__main__ import main
        for mode in ('auto', 'assist'):
            runner = AsyncMock(return_value={'outcomes': []})
            argv = ['youthgames_register', 'submit', '--config', 'profile.json', '--captcha-mode', mode]
            with patch.object(sys, 'argv', argv), patch('youthgames_register.__main__.run', runner), patch('youthgames_register.__main__.load', return_value={'state': {'output': '/tmp/reports'}}), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            self.assertEqual(runner.call_args.kwargs['captcha_mode'], mode)
