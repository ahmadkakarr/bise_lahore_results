"""Silent regressions: fake players, real detached watcher, no live registrations."""

import argparse
import importlib
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
import wave

from run_alarm import core


class AlarmUnitTests(unittest.TestCase):
    def test_collection_and_both_registration_clis_arm_by_default_and_allow_opt_out(self):
        cases=[('bise_results.__main__',['collect','synthetic.csv']),
            ('youthgames_register.__main__',['submit','--config','synthetic.json']),
            ('bise_allocate.__main__',['http-submit','--config','synthetic.json'])]
        for module_name,arguments in cases:
            module=importlib.import_module(module_name)
            for disabled in (False,True):
                with self.subTest(module=module_name,disabled=disabled),patch.object(sys,'argv',['fixture',*arguments,*(['--no-alarm'] if disabled else [])]), \
                        patch.object(module,'_execute',return_value=0),patch.object(core,'RunAlarm') as watcher,redirect_stderr(io.StringIO()):
                    self.assertEqual(module.main(alarm_default=True),0)
                    if disabled:watcher.assert_not_called()
                    else:watcher.return_value.start.assert_called_once();watcher.return_value.close.assert_called_once()

    def test_tone_is_valid_pcm_without_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sound.wav';core.make_sound(path)
            with wave.open(str(path)) as sound:
                self.assertEqual((sound.getnchannels(),sound.getsampwidth(),sound.getframerate()),(1,2,16000))
                self.assertEqual(sound.getnframes(),19200)

    def test_native_backend_order_stream_volume_and_fallbacks(self):
        with patch.object(core.shutil,'which',side_effect=lambda name:'/fake/'+name):commands=core.players(Path('/tmp/sound.wav'))
        self.assertEqual([Path(command[0]).name for command in commands],['pw-play','paplay','ffplay','aplay'])
        self.assertIn('--volume=1.0',commands[0]);self.assertFalse(any('wpctl' in str(c) or 'pactl' in str(c) for c in commands))

    def test_disabled_alarm_preserves_action_result_without_child(self):
        action=Mock(return_value=130)
        with patch.object(core,'RunAlarm') as watcher:
            self.assertEqual(core.guarded(argparse.Namespace(alarm=False),action,'fixture'),130)
        watcher.assert_not_called()

    def test_all_exit_codes_and_unhandled_failure_close_the_pipe(self):
        for code in (0,1,2,130):
            with self.subTest(code=code),patch.object(core,'RunAlarm') as watcher,redirect_stderr(io.StringIO()):
                self.assertEqual(core.guarded(argparse.Namespace(alarm=True),lambda args:code,'fixture'),code)
                watcher.return_value.close.assert_called_once_with({'event':'command_returned','exit_code':code})
        with patch.object(core,'RunAlarm') as watcher,redirect_stderr(io.StringIO()),self.assertRaises(RuntimeError):
            core.guarded(argparse.Namespace(alarm=True),Mock(side_effect=RuntimeError('fixture')),'fixture')
        watcher.return_value.close.assert_called_once_with({'event':'unhandled_exit','exception':'RuntimeError'})

    def test_audio_unavailable_fails_before_running_submissions(self):
        action=Mock()
        with patch.object(core,'RunAlarm') as watcher,redirect_stderr(io.StringIO()):
            watcher.return_value.start.side_effect=RuntimeError('no player')
            self.assertEqual(core.guarded(argparse.Namespace(alarm=True),action,'fixture'),1)
        action.assert_not_called()

    def test_only_explicit_success_and_user_stop_are_silent(self):
        for code in (0,130):
            self.assertTrue(core.silent_exit({'event':'command_returned','exit_code':code}))
        self.assertTrue(core.silent_exit({'event':'user_interrupted'}))
        for reason in ({'event':'command_returned','exit_code':1},
                       {'event':'command_returned','exit_code':2},
                       {'event':'command_returned','exit_code':False},
                       {'event':'command_returned','exit_code':'0'},
                       {'event':'command_returned'}, {'event':'process_disappeared'},
                       {'event':'unhandled_exit','exception':'RuntimeError'},None,[]):
            with self.subTest(reason=reason):self.assertFalse(core.silent_exit(reason))

    def test_keyboard_interrupt_and_system_exit_keep_their_exit_semantics(self):
        cases=[(KeyboardInterrupt(),{'event':'user_interrupted'}),
               (SystemExit(),{'event':'command_returned','exit_code':0}),
               (SystemExit(130),{'event':'command_returned','exit_code':130}),
               (SystemExit('failed'),{'event':'command_returned','exit_code':1})]
        for error,reason in cases:
            with self.subTest(error=repr(error)),patch.object(core,'RunAlarm') as watcher,redirect_stderr(io.StringIO()):
                with self.assertRaises(type(error)):
                    core.guarded(argparse.Namespace(alarm=True),Mock(side_effect=error),'fixture')
                watcher.return_value.close.assert_called_once_with(reason)

    def test_ack_is_repeatable_and_does_not_disarm_other_active_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'alert.json').write_text('{"status":"alarming"}')
            (root/'working.json').write_text('{"status":"watching"}')
            (root/'bad.json').write_text('bad json')
            self.assertEqual(core.acknowledge(root),1);self.assertEqual(core.acknowledge(root),0)
            self.assertFalse((root/'working.ack').exists())

    def test_ack_interrupts_stuck_audio_player_and_reaps_it(self):
        player=Mock();player.poll.return_value=None
        with patch.object(core.subprocess,'Popen',return_value=player):
            self.assertFalse(core.play_once(['fake'],lambda:True))
        player.terminate.assert_called_once();player.wait.assert_called_once()

    def test_metadata_disk_failure_does_not_raise_over_alarm(self):
        with patch.object(Path,'write_text',side_effect=OSError('disk full')):
            self.assertFalse(core.write_state(Path('/tmp/fixture.json'),{}))


class DetachedAlarmTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.bin=self.root/'bin';self.bin.mkdir();self.log=self.root/'played.log'
        # Restrict PATH to this fake player: these tests cannot touch real audio
        # or desktop notifications, including in the detached subprocess.
        fake=self.bin/'pw-play'
        fake.write_text('#!'+sys.executable+'\nimport os,time\nwith open(os.environ["ALARM_TEST_LOG"],"a") as f:f.write("played\\n")\ntime.sleep(.05)\n')
        fake.chmod(0o700)
        self.env={**os.environ,'PATH':str(self.bin),'ALARM_TEST_LOG':str(self.log)}
        self.addCleanup(self.silence)

    def silence(self):
        for path in self.root.glob('*.json'):path.with_suffix('.ack').touch(exist_ok=True)
        # Let the independent watcher observe its ack before removing fixtures.
        until=time.monotonic()+3
        while time.monotonic()<until:
            states=[json.loads(p.read_text()) for p in self.root.glob('*.json')]
            if not any(s['status'] in ('watching','alarming') for s in states):return
            time.sleep(.03)

    def wait_for(self,predicate,timeout=5):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            value=predicate()
            if value:return value
            time.sleep(.03)
        self.fail('Watchdog did not reach the expected state')

    def parent(self,code=0):
        source='''from run_alarm import core
from pathlib import Path
import sys,argparse
original=core.RunAlarm
core.RunAlarm=lambda label:original(label,Path(sys.argv[1]))
def action(args):
    print(next(Path(sys.argv[1]).glob('*.json')),flush=True)
    sys.stdin.readline()
    return int(sys.argv[2])
raise SystemExit(core.guarded(argparse.Namespace(alarm=True),action,"Synthetic stopped run"))
'''
        parent=subprocess.Popen([sys.executable,'-c',source,str(self.root),str(code)],env=self.env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(self.cleanup_parent,parent)
        state=Path(parent.stdout.readline().strip())
        self.assertTrue(state.is_file())
        return parent,state

    @staticmethod
    def cleanup_parent(parent):
        if parent.poll() is None:parent.kill()
        parent.wait(timeout=3)
        for stream in (parent.stdin,parent.stdout,parent.stderr):stream.close()

    def assert_repeats_and_acknowledges(self,path):
        self.wait_for(lambda:self.log.exists() and len(self.log.read_text().splitlines())>=2)
        self.assertEqual(json.loads(path.read_text())['status'],'alarming')
        self.assertEqual(core.acknowledge(self.root),1)
        self.wait_for(lambda:json.loads(path.read_text())['status']=='acknowledged')
        self.assertEqual(core.acknowledge(self.root),0)

    def assert_silent_finished(self,path):
        self.wait_for(lambda:json.loads(path.read_text())['status']=='finished')
        state=json.loads(path.read_text())
        self.assertEqual(state['plays'],0)
        self.assertFalse(self.log.exists())
        self.assertFalse(path.with_suffix('.wav').exists())

    def test_normal_completion_is_silent_and_does_not_ring_while_parent_runs(self):
        parent,path=self.parent()
        self.assertFalse(self.log.exists());self.assertEqual(json.loads(path.read_text())['status'],'watching')
        parent.stdin.write('finish\n');parent.stdin.flush();self.assertEqual(parent.wait(timeout=3),0)
        self.assert_silent_finished(path)
        self.assertEqual(json.loads(path.read_text())['reason'],{'event':'command_returned','exit_code':0})

    def test_ctrl_c_keyboard_interrupt_is_silent(self):
        parent,path=self.parent();parent.send_signal(signal.SIGINT);parent.wait(timeout=3)
        self.assert_silent_finished(path)
        self.assertEqual(json.loads(path.read_text())['reason'],{'event':'user_interrupted'})

    def test_cli_returning_130_after_graceful_drain_is_silent(self):
        parent,path=self.parent(code=130)
        parent.stdin.write('stop\n');parent.stdin.flush();self.assertEqual(parent.wait(timeout=3),130)
        self.assert_silent_finished(path)

    def test_error_exit_still_repeats_until_ack(self):
        parent,path=self.parent(code=2)
        parent.stdin.write('error\n');parent.stdin.flush();self.assertEqual(parent.wait(timeout=3),2)
        self.assert_repeats_and_acknowledges(path)

    def test_sigkill_still_triggers_detached_repeating_alarm(self):
        parent,path=self.parent();parent.kill();self.assertEqual(parent.wait(timeout=3),-signal.SIGKILL)
        self.assert_repeats_and_acknowledges(path)
        self.assertEqual(json.loads(path.read_text())['reason']['event'],'process_disappeared')

    def test_closed_terminal_signal_also_triggers_alarm(self):
        parent,path=self.parent();parent.send_signal(signal.SIGHUP);parent.wait(timeout=3)
        self.assert_repeats_and_acknowledges(path)

    def test_player_failure_uses_next_backend(self):
        fake=self.bin/'pw-play';fake.write_text('#!'+sys.executable+'\nraise SystemExit(1)\n')
        fallback=self.bin/'paplay'
        fallback.write_text('#!'+sys.executable+'\nimport os\nwith open(os.environ["ALARM_TEST_LOG"],"a") as f:f.write("fallback\\n")\n')
        fallback.chmod(0o700)
        parent,path=self.parent();parent.kill();parent.wait(timeout=3)
        self.assert_repeats_and_acknowledges(path)
        self.assertEqual(json.loads(path.read_text())['backend'],'paplay')
