"""Offline sports distribution, normalized handoff and immutable upload history."""
from collections import Counter,defaultdict
from contextlib import closing
from copy import deepcopy
import csv
import json
from pathlib import Path
import re
import sqlite3
import unittest

from PIL import Image
from bise_allocate import app,sports,resume
from bise_allocate.store import opened as allocations
from bise_normalize.sources import table
from youthgames_register import http_pilot
from youthgames_register.config import load as registration_config,sha
from youthgames_register.domain import validate
from youthgames_register.store import opened as registrations
from tests import test_allocations as allocation_fixture
from tests.test_youthgames import catalog as browser_catalog,config as browser_config,data
from tests.test_youthgames_http import form_fixture


class SportQuotaTests(unittest.TestCase):
    def test_exact_gender_totals_small_uc_groups_determinism_and_conservation(self):
        weights=json.loads(Path('sports_profiles/lahore_9th_raiwind.json').read_text())['weights']
        candidates=[{'roll':str(i),'applicant_key':f'{i:013d}','gender':'Male' if i<107 else 'Female','uc_number':255+i%20} for i in range(500)]
        assigned=sports.distribute(candidates,weights,'seed')
        self.assertEqual(assigned,sports.distribute(list(reversed(candidates)),weights,'seed'))
        self.assertEqual(len(assigned),len(candidates));self.assertEqual(len({r['applicant_key'] for r in assigned}),500)
        for gender in weights:
            counts=Counter(r['sport'] for r in assigned if r['gender']==gender)
            expected=sports.quotas(sum(r['gender']==gender for r in candidates),weights[gender],'seed'+gender)
            self.assertEqual(dict(counts),expected)
        before=Counter((r['gender'],r['uc_number']) for r in candidates)
        self.assertEqual(before,Counter((r['gender'],r['uc_number']) for r in assigned))
        self.assertFalse(any(r['gender']=='Female' and 'Boys Only' in r['sport'] for r in assigned))

    def test_zero_candidates_and_tiny_group_never_lose_students(self):
        self.assertEqual(sports.distribute([],{'Male':{'A':100}},'seed'),[])
        self.assertEqual(sum(sports.quotas(1,{'A':50,'B':50}).values()),1)


class SportIntegrationTests(unittest.TestCase):
    def setUp(self):
        allocation_fixture.AllocationTests.setUp(self)
        self.policy=json.loads(Path('sports_profiles/lahore_9th_raiwind.json').read_text())
        self.policy['scheme']=self.definition['scheme'];self.ratios=self.root/'sports.json';self.write_policy()
        image=self.root/'image.jpeg';Image.new('RGB',(3,3),'white').save(image)
        settings=json.loads((self.root/'reg.json').read_text());settings['photos'].update(shared_file=str(image),expected_sha256=sha(image.read_bytes()))
        (self.root/'reg.json').write_text(json.dumps(settings))
        # Include boys in the existing synthetic allocation fixture.
        with closing(sqlite3.connect(self.source)) as db,db:
            for row in self.rows[:7]:
                row['student_cnic']=row['student_cnic'][:-1]+'1'
                db.execute('UPDATE jobs SET result_json=? WHERE roll=?',(json.dumps(row),row['roll']))
        app.apply(self.profile);app.sync(self.profile)

    def write_policy(self):self.ratios.write_text(json.dumps(self.policy))

    def records(self):
        with (self.root/'normalized/Ready to Register.csv').open(encoding='utf-8-sig') as stream:return list(csv.DictReader(stream))

    def test_pipeline_updates_normalization_registration_input_and_real_payload_sport_ids(self):
        result=sports.apply(self.profile,self.ratios);self.assertEqual(result['assigned_now'],43)
        app.sync(self.profile)
        rows=self.records();self.assertGreater(len({r['Sport'] for r in rows}),3)
        self.assertEqual(Counter(r['Sport'] for r in rows),Counter({sport:sum(x['records'] for x in result['by_gender_sport'] if x['sport']==sport) for sport in {x['sport'] for x in result['by_gender_sport']}}))
        settings=registration_config(self.root/'register-profile.json');self.assertIsInstance(settings['selection']['sport'],list)
        self.assertIn('Sport',settings['confirmation']['fields'])
        catalog=json.loads((self.root/'catalog.json').read_text());targets=app.targets(self.definition,catalog)
        html=form_fixture().replace('Shalimar','Raiwind').replace('"77"','"76"')
        html=re.sub(r'const ucsByDistrict = .*?;</script>',
            'const ucsByDistrict = '+json.dumps({'21':[{'id':x['uc_id'],'name':x['UC']} for x in targets]})+';</script>',html)
        part=http_pilot.literal_assignment(html,'participationCatalog')
        part['sports']=catalog['data']['participation']['sports'];part['sportsByCategoryName']=catalog['data']['participation']['sportsByCategoryName']
        html=re.sub(r'window.participationCatalog = .*?;',lambda m:'window.participationCatalog = '+json.dumps(part)+';',html)
        for ident,key in [('kit-size-id','kit_sizes'),('jogger-size-id','jogger_sizes')]:
            options=''.join(f'<option value="{v["id"]}">{v["name"]}</option>' for v in catalog['data'][key])
            html=re.sub(r'(<select\b[^>]*id="'+ident+r'"[^>]*>).*?(</select>)',lambda m:m[1]+options+m[2],html,flags=re.S)
        class Client:
            def get(self):return http_pilot.Response(200,html)
            def post(self,*args):raise AssertionError('No live or synthetic POST required for preparation')
        prepared=http_pilot.prepare(self.root/'register-profile.json',Client())
        self.assertEqual(prepared['prepared'],43);self.assertEqual(prepared['applicant_posts'],0)
        option_ids={s['name']:str(s['id']) for s in part['sports']}
        with registrations(self.reg) as db:
            first=[tuple(r) for r in db.execute('SELECT id,payload_hash,state FROM jobs ORDER BY id')]
            for (payload,) in db.execute('SELECT payload FROM jobs'):
                item=json.loads(payload)
                self.assertEqual(item['option_ids']['Sport'],option_ids[item['data']['Sport']])
                self.assertEqual(item['sport_allocation']['sport'],item['data']['Sport'])
                self.assertEqual(item['validation']['state'],'ready',item['validation']['issues'])
        http_pilot.prepare(self.root/'register-profile.json',Client())
        with registrations(self.reg) as db:self.assertEqual(first,[tuple(r) for r in db.execute('SELECT id,payload_hash,state FROM jobs ORDER BY id')])
        self.assertFalse(resume.prepare(self.profile)['input_reused'])
        self.assertTrue(resume.prepare(self.profile)['input_reused'])

    def test_rerun_preserves_mapping_source_rows_uc_and_assignment_timestamps(self):
        with allocations(self.alloc,readonly=True) as db:before=[tuple(r) for r in db.execute('SELECT * FROM allocations ORDER BY roll')]
        sports.apply(self.profile,self.ratios);app.sync(self.profile)
        with allocations(self.alloc,readonly=True) as db:assigned=[tuple(r) for r in db.execute('SELECT * FROM sport_assignments ORDER BY roll')]
        result=sports.apply(self.profile,self.ratios);self.assertEqual(result['assigned_now'],0)
        app.sync(self.profile)
        with allocations(self.alloc,readonly=True) as db:
            self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM allocations ORDER BY roll')])
            self.assertEqual(assigned,[tuple(r) for r in db.execute('SELECT * FROM sport_assignments ORDER BY roll')])
            self.assertEqual(db.execute('SELECT count(*) FROM sport_batches').fetchone()[0],1)
        self.assertEqual(app.apply(self.profile)['status'],'already_allocated')

    def test_registered_uncertain_and_even_ready_with_prior_post_are_excluded(self):
        with allocations(self.alloc,readonly=True) as db:rows=[dict(r) for r in db.execute('SELECT * FROM allocations ORDER BY roll LIMIT 3')]
        with registrations(self.reg) as db:
            for row,state in zip(rows,('registered','uncertain','ready')):
                cur=db.execute('INSERT INTO jobs(portal,campaign,applicant_key,roll,payload,payload_hash,state,tracking) VALUES(?,?,?,?,?,?,?,?)',
                    ('https://youthgames.punjab.gov.pk','punjab-youthgames-2026',row['applicant_key'],row['roll'],json.dumps({'data':{'Sport':'Athletics'}}),'frozen-hash',state,'TEST-TRACK' if state=='registered' else None))
                if state=='ready':db.execute("INSERT INTO attempts(job,started_at,intent_at,dispatched_at,outcome) VALUES(?, 'test','test','test','rejected')",(cur.lastrowid,))
            db.commit();before=[tuple(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')];attempts=[tuple(r) for r in db.execute('SELECT * FROM attempts')]
        result=sports.apply(self.profile,self.ratios);self.assertEqual(result['assigned_now'],40)
        self.assertEqual(result['skipped']['registered'],1);self.assertEqual(result['skipped']['posted_or_held'],2)
        with registrations(self.reg) as db:
            self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')])
            self.assertEqual(attempts,[tuple(r) for r in db.execute('SELECT * FROM attempts')])
        with allocations(self.alloc,readonly=True) as db:
            self.assertFalse(set(r[0] for r in db.execute('SELECT applicant_key FROM sport_assignments'))&{r['applicant_key'] for r in rows})

    def test_invalid_weights_or_female_boys_only_fail_before_any_assignments(self):
        for change in ('sum','boys','unknown'):
            broken=deepcopy(self.policy)
            if change=='sum':broken['weights']['Female']['Athletics']=19
            elif change=='boys':broken['weights']['Female']['Athletics']=19;broken['weights']['Female']['Kabaddi (Boys Only)']=1
            else:broken['weights']['Male']['Invented Sport']=broken['weights']['Male'].pop('Football')
            self.ratios.write_text(json.dumps(broken))
            with self.assertRaises(ValueError):sports.apply(self.profile,self.ratios)
        with allocations(self.alloc,readonly=True) as db:self.assertEqual(db.execute('SELECT count(*) FROM sport_assignments').fetchone()[0],0)

    def test_existing_assignment_and_policy_cannot_be_replaced_or_deleted(self):
        sports.apply(self.profile,self.ratios)
        with allocations(self.alloc) as db:
            for sql in ("UPDATE sport_assignments SET sport='Football'",'DELETE FROM sport_assignments',
                'INSERT OR REPLACE INTO sport_assignments SELECT * FROM sport_assignments LIMIT 1'):
                with self.assertRaises(sqlite3.IntegrityError):db.execute(sql)


class SelectionTests(unittest.TestCase):
    def test_explicit_sport_allow_list_still_rejects_unapproved_or_unavailable_sports(self):
        cfg=browser_config();cat=browser_catalog();row=data()
        cfg['selection']['sport']=['Athletics','Badminton']
        self.assertEqual(validate(row,cfg,cat)['state'],'ready')
        row['Sport']='Badminton'
        self.assertIn('unresolved_option:Sport',validate(row,cfg,cat)['issues'])
        row['Sport']='Football'
        self.assertIn('profile_selection_mismatch:Sport',validate(row,cfg,cat)['issues'])
