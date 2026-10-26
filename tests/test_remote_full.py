from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from bise_remote import control,worker,guardian,pilot


class FullRemoteTests(unittest.TestCase):
    def test_guardian_owns_native_pmxcfs_comment_format_and_rejects_foreign_guest(self):
        text='#bise-results%3Ainter-2024-pilot\narch: amd64\nhostname: bise-inter-2024-pilot\nunprivileged: 1\n'
        self.assertTrue(guardian.owns_guest(text))
        self.assertFalse(guardian.owns_guest(text.replace('unprivileged: 1','unprivileged: 0')))
        self.assertFalse(guardian.owns_guest(text.replace('hostname: bise-inter-2024-pilot','hostname: another-guest')))

    def test_collector_command_is_full_headless_and_uses_system_chromium(self):
        c={'year':2024,'class':12,'ocr_workers':2,'interval_seconds':3,'disk_pause_gib':4,
           'evidence_quota_mib':1024,'missing_dob_policy':'estimate','estimated_age_at_exam':18,
           'save_student_photos':True,'browser_executable':'/usr/bin/chromium'}
        command=worker.collector_command(Path('/var/lib/job'),c,8,0)
        for flag,value in (('--workers','8'),('--limit','0'),('--browser-executable','/usr/bin/chromium')):
            self.assertEqual(command[command.index(flag)+1],value)
        self.assertIn('--headless',command);self.assertIn('--save-student-photos',command)

    def test_storage_uses_hysteresis_and_stale_or_invalid_guardian_pauses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'runtime').mkdir();(root/'evidence').mkdir()
            c={'disk_pause_gib':4,'disk_resume_gib':6,'evidence_quota_mib':1024}
            path=root/'runtime/host-storage.json';path.write_text(json.dumps({'safe':True,'observed_at':time.time()}))
            with patch.object(worker.shutil,'disk_usage',return_value=SimpleNamespace(free=5*1024**3)):
                self.assertEqual(worker.capacity(root,c)[0],'ready')
                self.assertEqual(worker.capacity(root,c,paused=True)[0],'paused_storage')
                for value in ('{broken',json.dumps({'safe':True,'observed_at':time.time()-181}),json.dumps({'safe':False,'observed_at':time.time()})):
                    path.write_text(value)
                    self.assertEqual(worker.capacity(root,c)[0],'paused_host_storage')

    def test_evidence_cleanup_race_does_not_crash_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'evidence').mkdir()
            fake=SimpleNamespace(is_file=lambda:True,is_symlink=lambda:False,
                                 stat=lambda:(_ for _ in ()).throw(FileNotFoundError()))
            with patch.object(Path,'rglob',return_value=iter([fake])):
                self.assertEqual(worker.evidence_size(root),0)

    def test_host_pool_and_metadata_reserves_are_independent(self):
        gb=1024**3
        self.assertTrue(guardian.storage_safe(100*gb,20*gb,20,1))
        for values in ((63*gb,20*gb,20,1),(100*gb,7*gb,20,1),
                       (100*gb,20*gb,85,1),(100*gb,20*gb,20,75)):
            self.assertFalse(guardian.storage_safe(*values))

    def test_repeated_start_returns_before_any_install_or_input_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'deployment.json').write_text(json.dumps({'vmid':950}))
            with patch.object(control,'STATE',root),patch.object(pilot,'STATE',root),\
                 patch.object(pilot,'guest',return_value=b'active\n') as guest,\
                 patch.object(control,'install') as install,patch.object(pilot,'setup') as setup,redirect_stdout(io.StringIO()):
                control.start({})
                self.assertEqual(guest.call_count,1);install.assert_not_called();setup.assert_not_called()

    def test_input_mismatch_preserves_pinned_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);csv=root/'rolls.csv';csv.write_text('Roll Number\n500001\n')
            (root/'deployment.json').write_text(json.dumps({'input_sha256':'different'}))
            with patch.object(control,'STATE',root),patch.object(pilot,'STATE',root),\
                 patch.object(pilot,'guest',return_value=b'inactive\n'),patch.object(control,'install') as install:
                with self.assertRaisesRegex(ValueError,'differs'):control.start({'input':str(csv)})
                install.assert_not_called()

    def test_monitor_ctrl_c_never_stops_remote_service(self):
        with patch.object(control,'status',side_effect=KeyboardInterrupt),patch.object(control,'stop') as stop,redirect_stdout(io.StringIO()):
            control.watch({})
            stop.assert_not_called()


if __name__=='__main__':unittest.main()
