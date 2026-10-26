from datetime import datetime,timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from bise_results.parser import parse_result
from bise_results.store import locked_store,import_batch,jobs_for,retain_in_database,prepare_jobs
from bise_results.photos import inspect_photo
from bise_results.media import save_file,read_asset,reference,checked_path
from bise_remote.photo_recovery import commit_photo
from bise_remote import pipeline,worker,ssc
from youthgames_register.photos import payload,applicant_photo,matches_profile

ROOT=Path(__file__).resolve().parents[1]


class FilePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        (self.root/'runtime').mkdir();self.csv=self.root/'rolls.csv';self.csv.write_text('Roll Number\n001234\n')
        self.parsed=parse_result((ROOT/'tests/fixtures/ssc-part1.html').read_text(),'001234',2026,exam_type='1')
        image=BytesIO();Image.new('RGB',(120,140),'blue').save(image,'JPEG');self.image=image.getvalue()
        self.meta={**inspect_photo(self.image),'source_url':'http://result.biselahore.com/ImageLoader.aspx?Updateid=sample/PIC/1.jpg'}
        self.store=locked_store(self.root/'results.sqlite3');self.db=self.store.__enter__();self.addCleanup(self.store.__exit__,None,None,None)
        self.batch=import_batch(self.db,self.csv,'School Batch',2026,exam_type='1')
        self.job=jobs_for(self.db,self.batch)[0]
        with self.db:
            self.db.execute("UPDATE jobs SET state='verified',result_json=?,receipt_json='{}' WHERE id=?",(json.dumps(self.parsed),self.job['id']))
        retain_in_database(self.db,jobs_for(self.db,self.batch)[0],'existing cleaned result')

    def add_photo(self):
        fetched={**self.parsed,'student_photo':dict(self.meta)}
        commit_photo(self.db,self.job,fetched,self.image,self.root)
        return json.loads(self.db.execute('SELECT result_json FROM jobs').fetchone()[0])

    def test_original_file_and_retention_survive_cleanup_and_resume(self):
        original=json.loads(self.db.execute('SELECT result_json FROM jobs').fetchone()[0]);new=self.add_photo()
        for field in original:self.assertEqual(new[field],original[field])
        self.assertEqual(self.db.execute('SELECT count(*) FROM student_photos').fetchone()[0],0)
        self.assertEqual(read_asset(self.db,self.job['id'],new['student_photo']),self.image)
        self.assertEqual(prepare_jobs(self.db,self.batch,True),[])
        path=self.root/'photos'/new['student_photo']['relative_path'];path.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):read_asset(self.db,self.job['id'],new['student_photo'])

    def test_identity_mismatch_never_overwrites_result(self):
        before=self.db.execute('SELECT result_json FROM jobs').fetchone()[0]
        fetched={**self.parsed,'date_of_birth':'01/01/2005','student_photo':dict(self.meta)}
        with self.assertRaisesRegex(ValueError,'identity mismatch'):commit_photo(self.db,self.job,fetched,self.image,self.root)
        self.assertEqual(self.db.execute('SELECT result_json FROM jobs').fetchone()[0],before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM photo_assets').fetchone()[0],0)

    def test_lost_external_photo_queues_repair_without_refetching_saved_result(self):
        parsed=self.add_photo();(self.root/'photos'/parsed['student_photo']['relative_path']).unlink()
        before=self.db.execute('SELECT result_json FROM jobs').fetchone()[0]
        self.assertEqual(prepare_jobs(self.db,self.batch,True),[])
        self.assertEqual(self.db.execute('SELECT state FROM jobs').fetchone()[0],'verified')
        self.assertEqual(self.db.execute('SELECT result_json FROM jobs').fetchone()[0],before)
        self.assertEqual(self.db.execute('SELECT state FROM photo_recovery').fetchone()[0],'pending')

    def test_repeat_import_uses_existing_membership_without_reinserting_jobs(self):
        with patch('bise_results.store.csv.DictReader',side_effect=AssertionError('old input reparsed')):
            self.assertEqual(import_batch(self.db,self.csv,'School Batch',2026,exam_type='1'),self.batch)

    def test_file_before_db_crash_is_reusable_and_no_event_is_committed(self):
        before=self.db.execute('SELECT count(*) FROM result_events').fetchone()[0]
        try:
            with self.db:
                save_file(self.db,self.job['id'],{**self.parsed,'student_photo':dict(self.meta)},self.image,self.root/'photos')
                raise RuntimeError('crash before commit')
        except RuntimeError:pass
        self.assertEqual(self.db.execute('SELECT count(*) FROM photo_assets').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM result_events').fetchone()[0],before)
        self.add_photo();self.assertEqual(len(list((self.root/'photos').rglob('*.jpg'))),1)

    def test_corrupt_asset_is_preserved_and_repaired_at_same_content_address(self):
        parsed=self.add_photo();path=self.root/'photos'/parsed['student_photo']['relative_path'];path.write_bytes(b'damaged original')
        self.add_photo();self.assertEqual(path.read_bytes(),self.image)
        self.assertEqual(next((self.root/'photos/quarantine').glob('*.corrupt')).read_bytes(),b'damaged original')

    def test_broken_handoff_photo_is_repairable_without_blocking_delivery_queue(self):
        from test_portal_normalization import catalog
        from bise_remote.handoff import export
        (self.root/'normalization.json').write_text((ROOT/'normalization_profiles/9th_combined.json').read_text())
        (self.root/'catalog.json').write_text(json.dumps(catalog()))
        parsed=self.add_photo();pipeline.process(self.root)
        (self.root/'photos'/parsed['student_photo']['relative_path']).write_bytes(b'bad')
        self.assertEqual(export(self.root)['entries'],[])
        self.assertEqual(self.db.execute('SELECT state FROM jobs').fetchone()[0],'verified')
        pipeline.process(self.root)
        self.add_photo();pipeline.process(self.root)
        self.assertEqual(len(export(self.root)['entries']),1)

    def test_symlink_and_path_escape_are_rejected(self):
        with self.assertRaises(ValueError):checked_path(self.root,'../wrong')
        link=self.root/'linked';link.symlink_to(self.root/'outside',target_is_directory=True)
        with self.assertRaises(ValueError):checked_path(link,'originals/a.jpg')

    def test_registration_provider_binds_exam_identity_and_checksum(self):
        new=self.add_photo();ref=reference(self.db,jobs_for(self.db,self.batch)[0])
        config={'photos':{'mode':'original_file_reference','source_media_root':str(self.root/'photos'),
           'source_exam':{'year':2026,'course':'SSC','exam_type':'1'}}}
        origin={'photo_reference':ref,'data':{'BForm':ref['applicant_key']},'roll':ref['roll']}
        photo=applicant_photo(config,origin)
        self.assertTrue(matches_profile(config,photo));self.assertEqual(payload(photo,ref['sha256']),self.image)
        with self.assertRaises(ValueError):payload(photo,'0'*64)
        origin['photo_reference']={**ref,'year':2024}
        with self.assertRaises(ValueError):applicant_photo(config,origin)

    def test_event_checkpoint_sees_late_photo_and_reruns_are_noop(self):
        from test_portal_normalization import catalog
        settings=json.loads((ROOT/'normalization_profiles/9th_combined.json').read_text())
        (self.root/'normalization.json').write_text(json.dumps(settings));(self.root/'catalog.json').write_text(json.dumps(catalog()))
        first=pipeline.process(self.root);self.assertEqual(first['blocked'],1)
        self.assertEqual(pipeline.process(self.root)['events_processed'],0)
        self.add_photo();second=pipeline.process(self.root);self.assertEqual(second['ready'],1)
        self.assertEqual(pipeline.process(self.root)['events_processed'],0)
        d=sqlite3.connect(self.root/'pipeline.sqlite3')
        self.assertEqual(d.execute("SELECT count(*) FROM ready_outbox WHERE state='ready'").fetchone()[0],1);d.close()

    def test_ssc_command_and_ownership_are_isolated_from_inter(self):
        config={'year':2026,'class':9,'ocr_workers':2,'interval_seconds':3,'disk_pause_gib':6,'evidence_quota_mib':1024,
           'save_student_photos':True,'photo_storage':'files'}
        command=worker.collector_command(self.root,config,8,0)
        self.assertEqual(command[command.index('--photo-storage')+1],'files');self.assertIn('--photo-root',command)
        c={'marker':'bise-results:ssc-2026-part1','name':'bise-ssc-2026-part1','address':'192.168.10.218/24','storage':'local-lvm'}
        with self.assertRaises(ValueError):ssc.identity(c,{'description':'bise-results:inter-2024-pilot','hostname':c['name'],'unprivileged':1})

    def test_handoff_replays_without_duplicate_rows_and_skips_confirmed_identity(self):
        from test_portal_normalization import catalog
        from bise_remote.handoff import export,ingest,acknowledge
        (self.root/'normalization.json').write_text((ROOT/'normalization_profiles/9th_combined.json').read_text())
        (self.root/'catalog.json').write_text(json.dumps(catalog()));self.add_photo();pipeline.process(self.root)
        message=export(self.root);self.assertEqual(len(message['entries']),1)
        central=self.root/'central.sqlite3';db=sqlite3.connect(central)
        db.executescript('CREATE TABLE registered_applicants(portal,campaign,applicant_key,status,tracking);CREATE TABLE jobs(portal,campaign,applicant_key,state);');db.close()
        first=ingest(central,self.root/'central-photos',message)
        second=ingest(central,self.root/'central-photos',message);self.assertEqual(first,second)
        with sqlite3.connect(central) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM source_inbox').fetchone()[0],1)
            db.execute('INSERT INTO registered_applicants VALUES(?,?,?,?,?)',(message['portal'],message['campaign'],message['entries'][0]['record']['data']['BForm'],'registered','CM-test'))
        confirmed=ingest(central,self.root/'central-photos',message)
        self.assertEqual(confirmed['receipts'][0]['state'],'already_registered')
        acknowledge(self.root,confirmed);acknowledge(self.root,confirmed)
        self.assertEqual(export(self.root)['entries'],[])

    def test_bundle_restore_hashes_photos_and_remaps_root(self):
        from bise_remote.bundle import create,restore
        self.add_photo();metadata=create(self.root)
        output=self.root/'download';output.mkdir()
        target=restore(self.root/'snapshots'/metadata['filename'],output,metadata)
        with sqlite3.connect(target/'results.sqlite3') as db:
            db.row_factory=sqlite3.Row;job=db.execute('SELECT * FROM jobs').fetchone();parsed=json.loads(job['result_json'])
            self.assertEqual(read_asset(db,job['id'],parsed['student_photo']),self.image)
        corrupt=output/'bad.tar.gz';corrupt.write_bytes(b'bad')
        with self.assertRaises(ValueError):restore(corrupt,output,metadata)


if __name__=='__main__':unittest.main()
