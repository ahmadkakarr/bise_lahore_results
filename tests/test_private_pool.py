"""Private normalization only: synthetic source/ledgers, no registration POST."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import csv
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from bise_allocate import app, private_pool
from bise_allocate.store import opened
from youthgames_shared.catalog import signature
from tests import test_allocations as fixture


class PrivatePoolTests(unittest.TestCase):
    setUp = fixture.AllocationTests.setUp

    def prepare(self):
        plan=json.loads(Path('allocation_profiles/lahore_private_9th_split.json').read_text())
        plan.update(batch='Synthetic private pool',seed='synthetic-private-v1')
        plan['groups']['raiwind'].update(scheme='Synthetic private Raiwind',maximum_records=10,
            allocation_profile=str(self.root/'private-raiwind.json'))
        plan['groups']['model_town'].update(scheme='Synthetic private Model Town',maximum_records=15,
            allocation_profile=str(self.root/'private-model-town.json'))
        plan['paths']={
            'source_database':str(self.source),'allocation_database':str(self.alloc),'registration_database':str(self.reg),
            'normalization_database':str(self.root/'normalization.sqlite3'),'catalog':str(self.root/'catalog.json'),
            'normalization_base':str(self.root/'base.json'),'registration_settings':str(self.root/'reg.json'),
            'sports_policy':str(Path('sports_profiles/lahore_9th_raiwind.json').resolve()),
            'normalization_profiles':str(self.root/'profiles'),'output':str(self.root/'private-output')}
        self.plan_path=self.root/'private-plan.json';self.plan_path.write_text(json.dumps(plan));self.plan=plan
        with closing(sqlite3.connect(self.source)) as db,db:
            for index,original in enumerate(self.rows):
                record={**original,'institution':f'Actual Private School {index%3}, LAHORE'}
                db.execute('UPDATE jobs SET result_json=? WHERE roll=?',(json.dumps(record),record['roll']))

    def output_rows(self,group):
        with (self.root/'private-output'/self.plan['groups'][group]['tehsil']/'Normalized.csv').open(encoding='utf-8-sig',newline='') as stream:
            return list(csv.DictReader(stream))

    def test_either_command_first_freezes_one_disjoint_cohort_and_preserves_actual_institutes(self):
        self.prepare()
        original=self.source.read_bytes()
        town=private_pool.run(self.plan_path,'model_town')
        raiwind=private_pool.run(self.plan_path,'raiwind')
        self.assertEqual(town['assigned'],15);self.assertEqual(raiwind['assigned'],10)
        self.assertEqual(town['pool']['selected_pool'],25)
        a=self.output_rows('raiwind');b=self.output_rows('model_town')
        self.assertEqual(len(a),10);self.assertEqual(len(b),15)
        self.assertFalse({r['BForm'] for r in a}&{r['BForm'] for r in b})
        self.assertTrue(all(r['SubCategory']=='Private' and r['Category']=='Schools (Secondary)' for r in a+b))
        self.assertTrue(all(r['Institute'].startswith(('Actual Private School','Named School')) and r['Institute'].endswith(('LAHORE','Lahore')) for r in a+b))
        self.assertTrue(all(r['Tehsil']=='Raiwind' for r in a))
        self.assertTrue(all(r['Tehsil']=='Model Town' for r in b))
        self.assertEqual(town['summary']['ready_to_register'],15)
        self.assertEqual(raiwind['summary']['ready_to_register'],10)
        self.assertEqual(self.source.read_bytes(),original)
        self.assertFalse(list((self.root/'private-output').rglob('*.xlsx')))
        with opened(self.alloc,readonly=True) as db:
            membership=[tuple(r) for r in db.execute('SELECT applicant_key,group_name,roll FROM private_pool_members ORDER BY applicant_key')]
            targets=db.execute('SELECT uc,uc_id FROM allocation_targets WHERE scheme=? AND uc_number=223',('Synthetic private Model Town',)).fetchone()
            self.assertEqual(targets[0],'UC No. 223, Q Block Model Town')
            self.assertEqual(db.execute('SELECT count(*) FROM sport_assignments').fetchone()[0],25)
        with patch.object(private_pool,'candidates',side_effect=AssertionError('reruns must not scan the source cohort again')):
            again=private_pool.run(self.plan_path,'raiwind')
        self.assertEqual(again['status'],'already_up_to_date')
        with opened(self.alloc,readonly=True) as db:
            self.assertEqual(membership,[tuple(r) for r in db.execute('SELECT applicant_key,group_name,roll FROM private_pool_members ORDER BY applicant_key')])

    def test_reversed_first_group_produces_same_membership_and_new_source_does_not_shuffle(self):
        self.prepare();private_pool.run(self.plan_path,'raiwind')
        with opened(self.alloc,readonly=True) as db:
            old=[tuple(r) for r in db.execute('SELECT applicant_key,group_name FROM private_pool_members ORDER BY applicant_key')]
        record={**self.rows_original()[0],'roll':'800001','student_cnic':'9999999999992','institution':'New School, LAHORE'}
        with closing(sqlite3.connect(self.source)) as db,db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(record['roll'],2026,'SSC','1','verified',json.dumps(record)))
        private_pool.run(self.plan_path,'model_town')
        with opened(self.alloc,readonly=True) as db:
            self.assertEqual(old,[tuple(r) for r in db.execute('SELECT applicant_key,group_name FROM private_pool_members ORDER BY applicant_key')])
        # Recompute deterministic initial selection on the original records.
        p=private_pool.paths(self.plan,self.plan_path)
        with opened(self.alloc,readonly=True) as db,patch.object(private_pool,'registration_snapshot',return_value={}):
            original=[json.loads(r[0]) for r in db.execute('SELECT source_record FROM private_pool_members ORDER BY applicant_key')]
        original.sort(key=lambda r:signature([self.plan['seed'],private_pool.identifier(r['student_cnic'])]))
        expected={private_pool.identifier(r['student_cnic']):'raiwind' if i<10 else 'model_town' for i,r in enumerate(original)}
        self.assertEqual(dict(old),expected)

    def rows_original(self):
        # setUp's source records are stored as self.rows by the shared fixture.
        return self.__dict__['rows']

    def test_wrong_district_public_review_invalid_and_used_identities_are_excluded(self):
        self.prepare();p=private_pool.paths(self.plan,self.plan_path)
        cases=[('700001','Private High School, LAHORE ROAD, KASUR','1234512345671'),
            ('700002','GOVT GIRLS SCHOOL, LAHORE','1234512345672'),
            ('700003','DIVISIONAL PUBLIC SCHOOL, LAHORE','1234512345673'),
            ('700004','LAHORE','1234512345674'),
            ('700005','Private High School, LAHORE','bad')]
        with closing(sqlite3.connect(self.source)) as db,db:
            for roll,institute,bform in cases:
                record={**self.rows_original()[0],'roll':roll,'institution':institute,'student_cnic':bform}
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(roll,2026,'SSC','1','verified',json.dumps(record)))
        used=self.rows_original()[0]['student_cnic']
        with opened(self.alloc) as db,patch.object(private_pool,'registration_snapshot',return_value={used:'registered'}):
            rows,excluded=private_pool.candidates(self.plan,p,db,private_pool.catalog(p['catalog']))
        self.assertTrue(all(r['roll'] not in {x[0] for x in cases} for r in rows))
        self.assertNotIn(used,{private_pool.identifier(r['student_cnic']) for r in rows})
        for reason in ('government','public_or_ownership_review','district_only_no_institute','district_not_confirmed_lahore','invalid_bform','already_normalized_allocated_or_imported'):
            self.assertGreater(excluded[reason],0)

    def test_duplicate_identity_group_never_divided_across_tehsils(self):
        self.prepare();first=self.rows_original()[0]
        record={**first,'roll':'800002','institution':'Different Private School, LAHORE'}
        with closing(sqlite3.connect(self.source)) as db,db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(record['roll'],2026,'SSC','1','verified',json.dumps(record)))
        result=private_pool.run(self.plan_path,'raiwind')
        self.assertEqual(result['pool']['excluded']['duplicate_identity_rows'],2)
        with opened(self.alloc,readonly=True) as db:
            self.assertFalse(db.execute('SELECT 1 FROM private_pool_members WHERE applicant_key=?',(first['student_cnic'],)).fetchone())

    def test_shortage_is_reported_instead_of_filling_with_public_or_duplicate_rows(self):
        self.prepare();self.plan['groups']['model_town']['maximum_records']=50
        self.plan_path.write_text(json.dumps(self.plan))
        result=private_pool.run(self.plan_path,'model_town')
        self.assertGreater(result['pool']['unfilled_slots'],0)
        self.assertLess(result['assigned'],50)
        self.assertEqual(result['summary']['ready_to_register'],result['assigned'])

    def test_changed_plan_missing_uc_and_insufficient_raiwind_fail_without_reassignment(self):
        self.prepare();private_pool.run(self.plan_path,'raiwind')
        changed=deepcopy(self.plan);changed['seed']='changed'
        self.plan_path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError,'plan changed'):private_pool.run(self.plan_path,'model_town')
        changed=deepcopy(self.plan);changed['groups']['model_town']['uc_numbers']=[999999]
        self.plan_path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError,'missing or ambiguous'):private_pool.run(self.plan_path,'model_town')

    def test_insufficient_raiwind_quota_or_invalid_sports_never_freezes_members(self):
        self.prepare();self.plan['groups']['raiwind']['maximum_records']=100
        self.plan_path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError,'No cohort created'):private_pool.run(self.plan_path,'raiwind')
        with opened(self.alloc,readonly=True) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM private_pools').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM allocations').fetchone()[0],0)

    def test_unrelated_registration_changes_reuse_exports_but_related_upload_updates_ready_subset(self):
        self.prepare();first=private_pool.run(self.plan_path,'raiwind')
        with opened(self.alloc,readonly=True) as db:
            key=db.execute('SELECT applicant_key FROM allocations WHERE scheme=? LIMIT 1',('Synthetic private Raiwind',)).fetchone()[0]
        with patch('bise_normalize.state.registration_snapshot',return_value={'9999999999999':'registered'}):
            second=private_pool.run(self.plan_path,'raiwind')
        self.assertEqual(second['status'],'already_up_to_date')
        with patch('bise_normalize.state.registration_snapshot',return_value={key:'registered'}):
            third=private_pool.run(self.plan_path,'raiwind')
        self.assertEqual(third['summary']['ready_to_register'],9)
        self.assertEqual(third['summary']['already_registered'],1)

    def test_a_later_batch_skips_the_other_groups_reserved_but_not_yet_allocated_members(self):
        self.prepare();private_pool.run(self.plan_path,'raiwind')
        p=private_pool.paths(self.plan,self.plan_path)
        with opened(self.alloc,readonly=True) as db:
            reserved={r[0] for r in db.execute('SELECT applicant_key FROM private_pool_members')}
            self.assertEqual(db.execute('SELECT count(*) FROM allocations').fetchone()[0],10)
            candidates,_=private_pool.candidates(self.plan,p,db,private_pool.catalog(p['catalog']))
        self.assertEqual(len(reserved),25)
        self.assertTrue(reserved.isdisjoint({private_pool.identifier(r['student_cnic']) for r in candidates}))

    def test_pool_rows_are_immutable_and_generated_profiles_preserve_operator_edits(self):
        self.prepare();private_pool.run(self.plan_path,'raiwind')
        with opened(self.alloc) as db:
            for sql in ("UPDATE private_pool_members SET group_name='model_town'",'DELETE FROM private_pool_members',
                        'DELETE FROM private_pools','INSERT OR REPLACE INTO private_pool_members SELECT * FROM private_pool_members LIMIT 1'):
                with self.assertRaises(sqlite3.IntegrityError):db.execute(sql)
        path=self.root/'private-raiwind.json';config=json.loads(path.read_text());config['tehsil']='Shalimar';path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError,'operator edits'):private_pool.run(self.plan_path,'raiwind')

    def test_normalization_only_never_acquires_registration_writer_or_submits(self):
        self.prepare()
        # Import before mocking store.opened: lazily importing the runner under
        # that mock would retain its writer alias in later tests.
        from youthgames_register import http_async
        with patch('youthgames_register.store.opened',side_effect=AssertionError('registration DB is read-only')),patch.object(
                http_async,'run',side_effect=AssertionError('no live submission')):
            result=private_pool.run(self.plan_path,'raiwind')
        self.assertEqual(result['applicant_posts'],0);self.assertFalse(result['registration_database_written'])
        self.assertIn('Normalization only',result['note'])


class PrivateCatalogTests(unittest.TestCase):
    def test_expired_catalog_refresh_is_one_verified_get_with_no_post(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'catalog.json'
            old=json.loads(Path('data/youthgames_catalog.json').read_text())
            old['fetched_at']=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat();path.write_text(json.dumps(old))
            with patch('httpx.Client') as client,patch(
                    'youthgames_register.http_pilot.form_catalog',return_value=(None,None,deepcopy(old['data']))):
                client.return_value.__enter__.return_value.get.return_value.text='synthetic form'
                result=private_pool.catalog(path)
            self.assertEqual(result['content_sha256'],old['content_sha256'])
            client.assert_called_once_with(timeout=30,verify=True,trust_env=False,follow_redirects=False)
            api=client.return_value.__enter__.return_value
            api.get.assert_called_once_with('https://youthgames.punjab.gov.pk/')
            api.post.assert_not_called()
