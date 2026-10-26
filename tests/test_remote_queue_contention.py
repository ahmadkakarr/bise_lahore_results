"""Reproduce concurrent photo/result writes and protect bounded queue claims."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bise_results.store import locked_store,import_batch,prepare_jobs,now
from bise_remote.photo_recovery import claim_photo
from bise_remote.worker import recovery_wait


class QueueContentionTests(unittest.TestCase):
    def test_resume_does_not_hold_writer_lock_during_retention_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);csv=root/'rolls.csv';csv.write_text('Roll Number\n001234\n')
            with locked_store(root/'source.sqlite3') as db:
                batch=import_batch(db,csv,'Batch',2026,exam_type='1')
                with db:db.execute("UPDATE jobs SET state='verified',result_json='{}',receipt_json='{}'")
                writes=[]
                def peer_write(receipt,*args):
                    # An independent backfill must be able to commit while the
                    # collector inspects cached identities/files.
                    with closing(sqlite3.connect(root/'source.sqlite3',timeout=.05)) as peer,peer:
                        peer.execute("INSERT INTO attempts(job_id,started_at) SELECT id,'peer-write' FROM jobs")
                    writes.append(True)
                    return False
                with patch('bise_results.store.check_receipt',side_effect=peer_write):
                    pending=prepare_jobs(db,batch)
                self.assertTrue(writes);self.assertEqual(len(pending),1)
                self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],len(writes))

    def test_photo_claim_is_index_bounded_and_excludes_future_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            with locked_store(Path(temp)/'source.sqlite3') as db:
                with db:
                    db.executemany("INSERT INTO jobs(id,roll,year,course,exam_type,state,result_json,updated_at) VALUES(?,?,2026,'SSC','1','verified',?,?)",
                       ((n,str(n),json.dumps({'padding':'x'*1000}),now()) for n in range(1,5001)))
                    db.executemany('INSERT INTO photo_recovery(job_id,updated_at) VALUES(?,?)',((n,now()) for n in range(1,5001)))
                    db.execute('UPDATE photo_recovery SET retry_after=1000 WHERE job_id=1')
                steps=[]
                def budget():
                    steps.append(1);return len(steps)>20
                db.set_progress_handler(budget,100)
                try:first=claim_photo(db,at=10)
                finally:db.set_progress_handler(None,0)
                self.assertEqual(first['id'],2);self.assertFalse(db.in_transaction)
                self.assertEqual(claim_photo(db,at=10)['id'],3)
                self.assertEqual(claim_photo(db,at=1001)['id'],1)

    def test_recovery_wait_refreshes_heartbeat_and_honors_stop(self):
        clock=[0];heartbeats=[]
        recovery_wait(12,lambda status,**v:heartbeats.append((clock[0],status,v)),lambda:False,
                      sleep=lambda s:clock.__setitem__(0,clock[0]+s),clock=lambda:clock[0])
        self.assertEqual([h[0] for h in heartbeats],[0,5,10])
        self.assertEqual(clock[0],12)
        clock[0]=0
        recovery_wait(100,lambda *a,**v:None,lambda:clock[0]>=2,
                      sleep=lambda s:clock.__setitem__(0,clock[0]+s),clock=lambda:clock[0])
        self.assertEqual(clock[0],2)


if __name__=='__main__':unittest.main()
