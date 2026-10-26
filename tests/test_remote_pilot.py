import io
import hashlib
import json
from pathlib import Path
import tarfile
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bise_remote import pilot
from bise_remote.common import load


class RemotePilotTests(unittest.TestCase):
    def test_proxmox_description_newline_is_normalized_but_foreign_guests_are_refused(self):
        config={'name':'bise-inter-2024-pilot','address':'192.168.10.239/24','storage':'local-lvm'}
        current={'hostname':config['name'],'description':'bise-results:inter-2024-pilot\n',
                 'unprivileged':1,'net0':'name=eth0,ip=192.168.10.239/24',
                 'rootfs':'local-lvm:vm-950-disk-0,size=32G'}
        pilot.validate_guest_identity(config,current)
        for change in ({'description':'another service'},{'unprivileged':0},
                       {'net0':'name=eth0,ip=192.168.10.1/24'},{'rootfs':'other:disk'}):
            with self.assertRaises(ValueError):pilot.validate_guest_identity(config,{**current,**change})

    def test_reproducible_source_only_package_has_no_database_or_environment(self):
        payload=pilot.package()
        self.assertEqual(payload,pilot.package())
        with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as archive:
            names=archive.getnames()
            self.assertIn('bise_results/photos.py',names)
            self.assertIn('requirements.lock.txt',names)
            self.assertTrue(all(n.endswith('.py') or n.endswith('/profile.template.json') or n=='requirements.lock.txt' for n in names))
            self.assertFalse(any('.venv' in n or 'results.sqlite3' in n for n in names))

    def test_repeated_start_does_not_mutate_input_or_create_second_collector(self):
        with patch.object(pilot,'guest',return_value=b'active\n') as guest:
            pilot.pilot({'release':'/opt/bise-results/releases/test'})
            self.assertEqual(guest.call_count,1)

    def test_pilot_uses_configured_eight_headless_workers_with_bounded_input(self):
        config={'release':'/opt/bise-results/releases/test','workers':8,'ocr_workers':2,'interval_seconds':3}
        with patch.object(pilot,'guest',side_effect=[b'inactive\n',b'',b'',b'']) as guest:
            pilot.pilot(config)
        command=guest.call_args_list[-1].args[1]
        self.assertEqual(command[command.index('--workers')+1],'8')
        self.assertEqual(command[command.index('--ocr-workers')+1],'2')
        self.assertEqual(command[command.index('--limit')+1],'4')
        self.assertIn('--headless',command)

    def test_estimate_profile_requires_explicit_registration_dob_gate(self):
        config=json.loads((pilot.ROOT/'remote_profiles/inter-2024-part2.json').read_text())
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'profile.json'
            for change in ({'estimated_age_at_exam':0},{'sports_registration_requires_verified_dob':False},
                           {'missing_dob_policy':'invent_date'}):
                path.write_text(json.dumps({**config,**change}))
                with self.assertRaises(ValueError):load(path)

    def test_download_checksum_failure_never_overwrites_existing_local_db(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);target=root/'results.sqlite3';target.write_bytes(b'preserve existing')
            with patch.object(pilot,'STATE',root),patch.object(pilot,'guest',side_effect=[b'',b'a'*64+b' snapshot.sqlite3\n',b'corrupt']):
                with self.assertRaisesRegex(ValueError,'checksum'):pilot.download({'source_sha256':'test'})
            self.assertEqual(target.read_bytes(),b'preserve existing')

    def test_download_closes_wal_snapshot_before_rename_and_preserves_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source.sqlite3'
            connection=sqlite3.connect(source)
            connection.execute('pragma journal_mode=WAL')
            connection.execute('create table students(roll text)')
            connection.execute("insert into students values ('500001')");connection.commit()
            connection.execute('pragma wal_checkpoint(TRUNCATE)')
            payload=source.read_bytes();connection.close()
            sha=hashlib.sha256(payload).hexdigest().encode()
            with patch.object(pilot,'STATE',root),patch.object(pilot,'guest',side_effect=[b'',sha+b' snapshot.sqlite3\n',payload]):
                pilot.download({'source_sha256':'test'})
            db=sqlite3.connect(root/'results.sqlite3')
            try:
                self.assertEqual(db.execute('select roll from students').fetchone()[0],'500001')
                self.assertEqual(db.execute('pragma journal_mode').fetchone()[0],'delete')
            finally:db.close()
            self.assertFalse(list(root.glob('*.download*')))
