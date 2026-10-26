"""DB-only cleanup never requeues saved rolls or deletes unresolved captures."""

import json
from pathlib import Path
import tempfile
import unittest

from bise_results.parser import parse_result
from bise_results.retention import cleanup, discard_verified_capture
from bise_results.store import locked_store,import_batch,jobs_for,prepare_jobs,retained_in_database
from bise_results.reports import export_batch
from test_identity_fields import FIXTURE,receipt_for


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'data').mkdir()
        self.path=self.root/'data/results.sqlite3'
        self.csv=self.root/'rolls.csv';self.csv.write_text('Roll Numbers\n001234\n001235\n')

    def verified(self,db):
        batch=import_batch(db,self.csv,'Retention')
        row=jobs_for(db,batch)[0]
        directory=self.root/'artifacts/results'/f"job-{row['id']}"/'attempt'
        directory.mkdir(parents=True)
        result=parse_result(FIXTURE.read_text(),'001234')
        receipt=receipt_for(directory,result=result)
        with db:
            db.execute("UPDATE jobs SET state='verified',result_json=?,receipt_json=? WHERE id=?",
                (json.dumps(result),json.dumps(receipt),row['id']))
        return batch,db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone(),directory.parent

    def test_discard_then_resume_skips_verified_without_original_files(self):
        with locked_store(self.path) as db:
            batch,row,directory=self.verified(db);before=dict(row)
            discard_verified_capture(db,row['id'],directory)
            self.assertFalse(directory.exists());self.assertTrue(retained_in_database(db,row))
            self.assertEqual([r['roll'] for r in prepare_jobs(db,batch)],['001235'])
            self.assertEqual(dict(db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone()),before)
        with locked_store(self.path) as db:
            self.assertTrue(retained_in_database(db,jobs_for(db,batch)[0]))

    def test_changed_payload_cannot_reuse_cleanup_exemption(self):
        with locked_store(self.path) as db:
            batch,row,directory=self.verified(db);discard_verified_capture(db,row['id'],directory)
            payload=json.loads(row['result_json']);payload['name']='Unexpected mutation'
            with db:db.execute('UPDATE jobs SET result_json=? WHERE id=?',(json.dumps(payload),row['id']))
            changed=db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone()
            self.assertFalse(retained_in_database(db,changed))
            self.assertEqual(len(prepare_jobs(db,batch)),2)

    def test_unresolved_capture_is_never_deleted(self):
        with locked_store(self.path) as db:
            _,row,directory=self.verified(db)
            with db:db.execute("UPDATE jobs SET state='needs_review' WHERE id=?",(row['id'],))
            with self.assertRaises(ValueError):discard_verified_capture(db,row['id'],directory)
            self.assertTrue((directory/'attempt/card.png').is_file())

    def test_optional_export_keeps_verified_database_only_rows_without_card_paths(self):
        with locked_store(self.path) as db:
            batch,row,directory=self.verified(db);discard_verified_capture(db,row['id'],directory)
            destination,counts=export_batch(db,batch,self.root/'output',include_pending=False)
            self.assertEqual(counts['verified'],1)
            exported=json.loads((destination/'Data/results.json').read_text())
            self.assertEqual(exported[0]['name'],json.loads(row['result_json'])['name'])
            self.assertFalse((destination/'Result Cards/001234.png').exists())

    def test_cleanup_is_scoped_audited_and_idempotent(self):
        with locked_store(self.path) as db:
            batch,row,directory=self.verified(db);before=dict(row)
        protected=[self.root/'registration_output/Receipts/receipt.png',
            self.root/'artifacts/results/job-99999/keep.png',self.root/'output/Personal Files/keep.png']
        for file in protected:file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(b'keep')
        preview=cleanup(self.root,self.path)
        self.assertFalse(preview['applied']);self.assertTrue(directory.exists())
        result=cleanup(self.root,self.path,apply=True)
        self.assertTrue(result['jobs_unchanged']);self.assertTrue(Path(result['backup']).is_file())
        self.assertFalse(directory.exists());self.assertTrue(all(p.read_bytes()==b'keep' for p in protected))
        with locked_store(self.path) as db:self.assertEqual(dict(jobs_for(db,batch)[0]),before)
        repeated=cleanup(self.root,self.path,apply=True)
        self.assertEqual(repeated['deleted_files'],0)
