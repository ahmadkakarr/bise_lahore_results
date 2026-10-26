"""Private handoff integrity, WAL snapshots, proof closure and chat boundaries."""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
import zipfile

script=Path(__file__).resolve().parents[1]/'scripts/export_context.py'
spec=importlib.util.spec_from_file_location('export_context',script)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'project';self.root.mkdir()
        self.destination=self.root/'context_exports/test';self.destination.mkdir(parents=True)
        self.stage=self.base/'stage';self.stage.mkdir();self.chat=self.base/'chats';self.chat.mkdir()
        (self.root/'app.py').write_text('print("source")\n')
        (self.root/'.env').write_text('PRIVATE_TOKEN=DO_NOT_CAPTURE_SECRET\n')
        for path in ('.venv/lib/big','artifacts/backups/old','normalized_output/.normalization/old','context_exports/old/old.zip'):
            p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('MUST_NOT_CAPTURE')
        (self.root/'data').mkdir();(self.root/'data/youthgames_catalog.json').write_text('{}')
        for name in module.DATABASES:
            with sqlite3.connect(self.root/'data'/name) as db:
                db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY,value TEXT)')
                db.execute("INSERT INTO records(value) VALUES ('full-private-row')")
        self.proof=self.root/'registration_output/profile/Receipts/attempt/response.html'
        self.proof.parent.mkdir(parents=True);self.proof.write_text('<p>Original proof</p>')
        self.receipt=self.proof.with_name('receipt.json')
        self.receipt.write_text(json.dumps({'files':{'response.html':{'path':str(self.proof),'sha256':module.sha_file(self.proof)}}}))
        with sqlite3.connect(self.root/'data/youthgames.sqlite3') as db:
            db.execute('CREATE TABLE attempts(id INTEGER PRIMARY KEY,evidence TEXT)')
            db.execute('INSERT INTO attempts(evidence) VALUES (?)',(str(self.receipt),))
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,state TEXT,receipt TEXT)')
            db.execute("INSERT INTO jobs(state,receipt) VALUES ('registered',?)",(str(self.receipt),))
        self.exporter=module.Exporter(self.root,self.destination,self.stage,chat='none',related=False)

    def finish(self):
        e=self.exporter;e.collect_source();e.snapshot_databases();e.collect_references();e.collect_chat();e.collect_diagnostics()
        e.freeze_code();e.context_documents();return e.pack()

    def test_full_bundle_preserves_rows_proofs_and_hashes_without_cache_or_secrets(self):
        before={p:module.sha_file(p) for p in (self.root/'data').glob('*.sqlite3')}
        report=self.finish();self.assertEqual(report['status'],'COMPLETE',report['gaps'])
        result=module.verify(self.destination);self.assertEqual(result['status'],'verified')
        self.assertEqual(before,{p:module.sha_file(p) for p in before})
        with zipfile.ZipFile(self.destination/'Code-and-Context.zip') as archive:
            manifest=json.loads(archive.read('MANIFEST.json'));names=[r['path'] for r in manifest['files']]
            self.assertIn('project/app.py',names)
            self.assertNotIn('project/.env',names)
            self.assertFalse(any('/.venv/' in p or '/backups/' in p for p in names))
            self.assertFalse(any(b'DO_NOT_CAPTURE_SECRET' in archive.read(n) for n in archive.namelist()))
        with zipfile.ZipFile(self.destination/'Data-results.zip') as archive:
            raw=archive.read('project/data/results.sqlite3')
        restored=self.base/'restored.sqlite3';restored.write_bytes(raw)
        with sqlite3.connect(restored) as db:self.assertEqual(db.execute('select value from records').fetchone()[0],'full-private-row')
        proof_record=next(r for r in manifest['files'] if r['path'].endswith('/response.html'))
        with zipfile.ZipFile(self.destination/proof_record['archive']) as archive:
            with tarfile.open(fileobj=io.BytesIO(archive.read(proof_record['container'])),mode='r:xz') as tar:
                self.assertEqual(tar.extractfile(proof_record['path']).read(),self.proof.read_bytes())

    def test_wal_commits_are_captured_without_touching_live_database(self):
        path=self.root/'data/results.sqlite3';live=sqlite3.connect(path)
        self.addCleanup(live.close);live.execute('PRAGMA journal_mode=WAL')
        live.execute("INSERT INTO records(value) VALUES ('wal-committed')");live.commit()
        before=module.sha_file(path)
        self.exporter.snapshot_databases()
        self.assertEqual(module.sha_file(path),before)
        with sqlite3.connect(self.stage/'snapshots/results.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0],2)
        live.execute("INSERT INTO records(value) VALUES ('later')");live.commit()
        with sqlite3.connect(self.stage/'snapshots/results.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0],2)

    def test_broken_original_proof_is_reported_without_rewriting_or_hiding_it(self):
        self.proof.write_text('CHANGED ORIGINAL')
        report=self.finish();self.assertEqual(report['status'],'INCOMPLETE')
        self.assertTrue(any('referenced SHA-256' in g['reason'] for g in report['gaps']))
        self.assertEqual(self.proof.read_text(),'CHANGED ORIGINAL')
        self.assertEqual(module.verify(self.destination)['capture_status'],'INCOMPLETE')

    def test_zip_tampering_fails_verification(self):
        self.finish()
        path=self.destination/'Data-results.zip'
        with path.open('ab') as stream:stream.write(b'tamper')
        with self.assertRaisesRegex(ValueError,'checksum'):module.verify(self.destination)

    def test_local_visible_chat_and_images_exclude_reasoning_tools_and_unrelated_chats(self):
        png=b'synthetic-image';events=[
            {'type':'session_meta','payload':{'cwd':str(self.root)}},
            {'type':'response_item','timestamp':'2026-10-23T10:00:00Z','payload':{'type':'message','role':'user','content':[
                {'type':'input_text','text':'Keep user request'}, {'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(png).decode()}]}},
            {'type':'response_item','timestamp':'2026-10-23T10:01:00Z','payload':{'type':'message','role':'assistant','channel':'analysis','content':[{'type':'output_text','text':'PRIVATE_REASONING'}]}},
            {'type':'response_item','timestamp':'2026-10-23T10:02:00Z','payload':{'type':'message','role':'assistant','channel':'final','content':[{'type':'output_text','text':'Keep final response'}]}},
            {'type':'response_item','payload':{'type':'function_call_output','output':'RAW_TOOL_SECRET'}},
        ]
        (self.chat/'matching.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
        (self.chat/'unrelated.jsonl').write_text(json.dumps({'type':'session_meta','payload':{'cwd':'/other/project'}})+'\n')
        e=self.exporter;e.chat='auto';e.chat_root=self.chat;e.collect_chat()
        text=(self.stage/'context/chat/CHAT.md').read_text()
        self.assertIn('Keep user request',text);self.assertIn('Keep final response',text)
        self.assertNotIn('PRIVATE_REASONING',text);self.assertNotIn('RAW_TOOL_SECRET',text)
        self.assertEqual(e.chat_stats['sessions'],1);self.assertEqual(e.chat_stats['images'],1)

    def test_missing_local_chat_is_an_explicit_gap(self):
        self.exporter.chat='auto';self.exporter.chat_root=self.chat;self.exporter.collect_chat()
        self.assertTrue(any(g['source']=='local chat' for g in self.exporter.gaps))

    def test_reference_cannot_export_an_outside_file(self):
        secret=self.base/'outside-receipt.json';secret.write_text('{"outside":"SECRET"}')
        self.exporter.follow_receipt(secret)
        self.assertFalse(any(i['path']==secret for i in self.exporter.items.values()))
        self.assertTrue(any('authorised project' in g['reason'] for g in self.exporter.gaps))

    def test_paths_cannot_escape_archive_root(self):
        for name in ('.','/etc/passwd','../outside','project/../../outside','a\\b'):
            self.assertFalse(module.safe_member(name))

    def test_database_symlink_is_a_gap_not_an_outside_snapshot(self):
        source=self.root/'data/results.sqlite3';source.unlink()
        outside=self.base/'outside.sqlite3'
        with sqlite3.connect(outside) as db:db.execute('CREATE TABLE private(value TEXT)')
        source.symlink_to(outside)
        self.exporter.snapshot_databases()
        self.assertFalse((self.stage/'snapshots/results.sqlite3').exists())
        self.assertTrue(any('symlinks' in g['reason'] for g in self.exporter.gaps))

    def test_rule_provenance_labels_are_not_mistaken_for_missing_files(self):
        profile=self.root/'normalization_profiles/profile.json';profile.parent.mkdir()
        profile.write_text(json.dumps({'source':{'path':'../data/results.sqlite3'},
            'rules':{'Gender':{'provenance':'student_bform_parity_unverified'},
                     'Phone':{'provenance':'operator_shared_contact'}}}))
        self.assertEqual(self.finish()['status'],'COMPLETE')
        self.assertFalse(any('parity_unverified' in g['source'] for g in self.exporter.gaps))

    def test_actual_registration_provenance_file_is_still_required(self):
        profile=self.root/'registration_profiles/profile.json';profile.parent.mkdir()
        profile.write_text(json.dumps({'input':{'provenance':'missing-provenance.jsonl'}}))
        report=self.finish()
        self.assertEqual(report['status'],'INCOMPLETE')
        self.assertTrue(any('missing-provenance.jsonl' in g['source'] for g in report['gaps']))

    def test_evidence_volumes_have_distinct_container_names_and_full_coverage(self):
        self.exporter.volume_bytes=128*1024
        for name in ('payload-a.bin','payload-b.bin'):
            path=self.proof.with_name(name);path.write_bytes(os.urandom(200_000))
            receipt=json.loads(self.receipt.read_text())
            receipt['files'][name]={'path':str(path),'sha256':module.sha_file(path)}
            self.receipt.write_text(json.dumps(receipt))
        self.assertEqual(self.finish()['status'],'COMPLETE')
        bundle=json.loads((self.destination/'BUNDLE.json').read_text())
        volumes=[a for a in bundle['archives'] if a.get('container')]
        self.assertGreaterEqual(len(volumes),2)
        self.assertEqual(len(volumes),len({a['container'] for a in volumes}))
        self.assertEqual(module.verify(self.destination)['status'],'verified')


if __name__=='__main__':unittest.main()
