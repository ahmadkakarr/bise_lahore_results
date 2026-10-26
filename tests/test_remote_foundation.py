import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bise_remote import snapshots
from bise_remote.common import counts
from bise_remote.worker import run
from bise_results.resources import available_memory_mb


class RemoteFoundationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)

    def test_missing_dob_gate_never_starts_collector_or_creates_results_db(self):
        (self.root/'config.json').write_text(json.dumps({'blocked_reason':'verified DOB unavailable'}))
        self.assertEqual(run(self.root),2)
        status=json.loads((self.root/'runtime/status.json').read_text())
        self.assertEqual(status['status'],'held_missing_dob_source')
        self.assertFalse(status['collector_started'])
        self.assertFalse((self.root/'results.sqlite3').exists())

    def database(self):
        path=self.root/'results.sqlite3';db=sqlite3.connect(path)
        self.addCleanup(db.close);db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE jobs(state TEXT)')
        db.execute('CREATE TABLE database_retention(job_id INTEGER)')
        db.execute("INSERT INTO jobs VALUES ('verified')");db.commit()
        return db

    def test_snapshot_includes_committed_wal_and_restores_verified_counts(self):
        live=self.database();item=snapshots.create(self.root)
        live.execute("INSERT INTO jobs VALUES ('pending')");live.commit()
        target=self.root/'download.sqlite3'
        snapshots.restore(self.root/'snapshots'/item['filename'],target,item)
        self.assertEqual(counts(target),{'verified':1,'total':1,'retained':0})
        self.assertEqual(counts(self.root/'results.sqlite3')['total'],2)

    def test_interrupted_snapshot_stream_resumes_same_compressed_bytes(self):
        self.database();item=snapshots.create(self.root)
        expected=(self.root/'snapshots'/item['filename']).read_bytes()
        resumed=io.BytesIO();snapshots.stream(self.root,item['id'],17,resumed)
        self.assertEqual(expected[:17]+resumed.getvalue(),expected)
        snapshots.release(self.root,item['id'])
        self.assertEqual(snapshots.metadata(self.root,item['id'])['pinned_until'],0)
        with self.assertRaises(ValueError):snapshots.metadata(self.root,'../../other')

    def test_corrupt_download_does_not_overwrite_local_database(self):
        self.database();item=snapshots.create(self.root)
        compressed=self.root/'snapshots'/item['filename']
        with compressed.open('ab') as stream:stream.write(b'corrupt')
        target=self.root/'saved.sqlite3';target.write_bytes(b'preserve existing')
        with self.assertRaisesRegex(ValueError,'checksum'):snapshots.restore(compressed,target,item)
        self.assertEqual(target.read_bytes(),b'preserve existing')

    def test_lxc_memory_admission_uses_remaining_cgroup_limit(self):
        mem=self.root/'meminfo';mem.write_text('MemAvailable: 50000000 kB\n')
        (self.root/'memory.max').write_text(str(6*1024**3))
        (self.root/'memory.current').write_text(str(5*1024**3))
        self.assertEqual(available_memory_mb(mem,self.root),1024)
        (self.root/'memory.max').write_text('max')
        self.assertEqual(available_memory_mb(mem,self.root),50000000//1024)
