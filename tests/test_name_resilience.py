"""Offline regressions for the saved father-name rejection and safe recovery."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from youthgames_shared.names import canonical_name,valid_name
from youthgames_shared.catalog import validate_fields
from youthgames_register.domain import classify,form_values
from youthgames_register.http_pilot import form_catalog,receipt,Response
from youthgames_register.name_recovery import recover_name_formatting,NAME_ERRORS
from youthgames_register.store import opened,import_batch,jobs,begin,intent,dispatched,finish,check_receipt,ready_job_refs,last_dispatch_time
from tests.test_youthgames import applicant,config,catalog,data
from tests.test_youthgames_http import form_fixture

ERROR=next(iter(NAME_ERRORS))
HTML='<form id="registration-form" data-has-server-errors="1"><div id="registration-errors">'+ERROR+'</div></form>'


class NamePolicyTests(unittest.TestCase):
    def test_exact_failure_and_separator_variants_preserve_every_letter_and_are_idempotent(self):
        for original,expected in [('AHSAN -UL-HAQ','AHSAN-UL-HAQ'),('M. AHSAN','M.AHSAN'),
                ('  Synthetic   Father ','Synthetic Father'),('AHSAN – UL – HAQ','AHSAN-UL-HAQ'),
                ('عبد الرحمان','عبد الرحمان'),("O’ Connor","O'Connor")]:
            with self.subTest(original=original):
                fixed=canonical_name(original)
                self.assertEqual(fixed,expected);self.assertTrue(valid_name(fixed))
                self.assertEqual(canonical_name(fixed),fixed)
                self.assertEqual(''.join(c for c in original if c.isalpha()),''.join(c for c in fixed if c.isalpha()))

    def test_digits_slashes_empty_and_mixed_punctuation_require_individual_correction(self):
        for value in ('Student 123','Father/Son','A.-B','---','',None):
            with self.subTest(value=value),self.assertRaises(ValueError):canonical_name(value)

    def test_catalog_and_wire_apply_same_policy_without_mutating_confirmed_source(self):
        original=data();original['FatherName']='AHSAN -UL-HAQ';before=deepcopy(original)
        root=form_catalog(form_fixture())[2]
        values,_,issues,changes=validate_fields(original,{'data':root})
        self.assertEqual(values['FatherName'],'AHSAN-UL-HAQ')
        self.assertEqual(form_values(original)['FatherName'],values['FatherName'])
        self.assertEqual(changes['FatherName']['from'],before['FatherName'])
        self.assertNotIn('portal_name_format:FatherName',issues);self.assertEqual(original,before)

    def test_mixed_success_and_validation_is_unknown_and_never_safe_to_replay(self):
        for html in (HTML+'<span id="registration-success-tracking-id">TEST-123</span>',
                HTML.replace('data-has-server-errors="1"','data-has-server-errors="1" data-registration-success="1"')+'<span id="registration-success-tracking-id">TEST-123</span>'):
            self.assertEqual(classify(html,200)['state'],'uncertain')


class NameRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.settings=config();self.photo=self.root/'photo.jpg';self.photo.write_bytes(b'fixture')
        self.item=applicant(self.photo);self.item['data']['FatherName']='AHSAN -UL-HAQ'

    def reject(self,db,html=HTML,proof=None):
        import_batch(db,self.settings,{'csv':'old'},catalog(),self.root,[self.item])
        job=jobs(db,self.settings['profile'])[0];attempt=begin(db,job);intent(db,job['id'],attempt);dispatched(db,job['id'],attempt)
        outcome=classify(html,200)
        path=receipt(self.root,job,attempt,outcome,Response(200,html),proof or {})
        finish(db,job['id'],attempt,outcome['state'],outcome['detail'],path)
        return job,attempt,path

    def test_receipt_bound_recovery_once_retains_raw_payload_attempt_and_original_evidence(self):
        with opened(self.root/'db.sqlite3') as db:
            job,attempt,path=self.reject(db);original=path.read_bytes()
            self.assertEqual(len(recover_name_formatting(db,self.settings)),1)
            fixed=jobs(db,self.settings['profile'])[0]
            self.assertEqual(fixed['state'],'ready');self.assertEqual(fixed['payload'],job['payload'])
            self.assertEqual(fixed['payload_hash'],job['payload_hash']);self.assertEqual(path.read_bytes(),original)
            self.assertTrue(check_receipt(fixed['receipt']))
            self.assertEqual(db.execute('SELECT outcome FROM attempts WHERE id=?',(attempt,)).fetchone()[0],'rejected')
            self.assertEqual(recover_name_formatting(db,self.settings),[])
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM submission_recoveries').fetchone()[0],1)
            changed=deepcopy(self.item);changed['data']['FatherName']='AHSAN-UL-HAQ'
            import_batch(db,self.settings,{'csv':'new'},catalog(),self.root,[changed])
            self.assertEqual(len(ready_job_refs(db,self.settings)),1)
            self.assertEqual(db.execute('SELECT count(*) FROM conflicts').fetchone()[0],0)

    def test_damaged_mixed_or_nonfixable_rejections_are_never_reset(self):
        for kind in ('damaged','mixed','bound','canonical'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as folder,opened(Path(folder)/'db.sqlite3') as db:
                self.item['data']['FatherName']='AHSAN-UL-HAQ' if kind=='canonical' else 'AHSAN -UL-HAQ'
                html=HTML.replace('</div>','; Mobile phone is invalid.</div>') if kind=='mixed' else HTML
                _,_,path=self.reject(db,html)
                if kind=='damaged':(self.root/'response.html').write_text('damaged')
                if kind=='bound':
                    saved=json.loads(path.read_text());saved['payload_hash']='wrong';path.write_text(json.dumps(saved))
                self.assertEqual(recover_name_formatting(db,self.settings),[])
                self.assertEqual(jobs(db,self.settings['profile'])[0]['state'],'rejected')

    def test_queue_is_metadata_only_and_dispatch_clock_persists(self):
        path=self.root/'db.sqlite3'
        with opened(path) as db:
            import_batch(db,self.settings,{'csv':'old'},catalog(),self.root,[self.item])
            refs=ready_job_refs(db,self.settings,limit=1)
            self.assertEqual(refs[0].keys(),['id','roll'])
            job=jobs(db,self.settings['profile'])[0]
            attempt=begin(db,job);intent(db,job['id'],attempt);dispatched(db,job['id'],attempt)
            stamp=last_dispatch_time(db,job['portal'],job['campaign'])
            self.assertEqual(stamp,db.execute('SELECT dispatched_at FROM attempts').fetchone()[0])
            finish(db,job['id'],attempt,'registered',tracking='TEST-123')
            self.assertEqual(ready_job_refs(db,self.settings),[])
        with opened(path) as db:
            self.assertEqual(last_dispatch_time(db,job['portal'],job['campaign']),stamp)
            self.assertEqual(ready_job_refs(db,self.settings),[])

    def test_corrected_wire_name_rejected_again_is_quarantined_without_another_retry(self):
        with opened(self.root/'db.sqlite3') as db:
            self.reject(db,proof={'name_formatting':{'FatherName':{'from':'AHSAN -UL-HAQ','to':'AHSAN-UL-HAQ'}}})
            self.assertEqual(recover_name_formatting(db,self.settings),[])
            self.assertEqual(jobs(db,self.settings['profile'])[0]['state'],'rejected')
