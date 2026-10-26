import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('record_snapshot',Path(__file__).parents[1]/'scripts/snapshot_records_only.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class RecordsOnlySnapshotTests(unittest.TestCase):
    def fixture(self,root,state='verified'):
        path=root/'source.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''CREATE TABLE jobs(id INTEGER PRIMARY KEY,state TEXT,result_json TEXT);
                CREATE TABLE student_photos(job_id INTEGER PRIMARY KEY REFERENCES jobs(id),sha256 TEXT,
                  mime_type TEXT,width INTEGER,height INTEGER,source_url TEXT,payload BLOB);
                CREATE INDEX jobs_state ON jobs(state);''')
            db.execute('INSERT INTO jobs VALUES(1,?,?)',(state,json.dumps({'name':'Synthetic Student','date_of_birth':''})))
            db.execute('INSERT INTO student_photos VALUES(1,?,?,?,?,?,?)',('hash','image/jpeg',100,100,'https://example.invalid/photo',b'photo bytes'))
            db.commit()
        return path

    def test_copy_preserves_records_without_photo_payload_and_skips_identical_rerun(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=self.fixture(root);before=module.sha(source);target=root/'Records Only.sqlite3'
            result=module.snapshot(source,target)
            self.assertEqual(result['photo_payload_bytes_copied'],0)
            self.assertEqual(result['photo_references'],1);self.assertEqual(module.sha(source),before)
            with closing(sqlite3.connect(target)) as db:
                self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='student_photos'").fetchone())
                self.assertEqual(db.execute('SELECT source_database,payload_bytes FROM photo_references').fetchone(),(str(source),11))
                self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
            self.assertEqual(module.snapshot(source,target)['status'],'already_up_to_date')

    def test_running_claims_are_not_published_as_a_frozen_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=self.fixture(root,'running');target=root/'Records Only.sqlite3'
            with self.assertRaisesRegex(ValueError,'running'):module.snapshot(source,target)
            self.assertFalse(target.exists())

    def test_changed_source_does_not_replace_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=self.fixture(root);target=root/'Records Only.sqlite3'
            module.snapshot(source,target);before=module.sha(target)
            with closing(sqlite3.connect(source)) as db:
                db.execute("UPDATE jobs SET state='pending'");db.commit()
            with self.assertRaisesRegex(ValueError,'differs'):module.snapshot(source,target)
            self.assertEqual(module.sha(target),before)
