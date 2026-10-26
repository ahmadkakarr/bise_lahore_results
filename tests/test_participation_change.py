"""Frozen Model Town cohort participation changes; no live registrations."""
from copy import deepcopy
from html import escape
import json
from pathlib import Path
import re
import unittest

from bise_allocate import app,participation,private_pool
from bise_allocate.store import opened as allocations
from youthgames_register import http_pilot as http
from youthgames_register.config import load,input_snapshot
from youthgames_register.store import opened,jobs,check_receipt,begin
from tests import test_private_registration as fixtures
from tests.test_youthgames import fixture,SUCCESS


def form(catalog_path):
    data=json.loads(catalog_path.read_text())['data'];html=fixture()
    html=html.replace('<input type="hidden" name="institution_id" value=""><input type="hidden" name="institute_name" value="">',
        '<input type="hidden" id="institution-id" name="institution_id" value=""><input type="text" id="institute-name-text" name="institute_name" value=""><button type="button" id="use-manual-institute-btn">Manual</button>')
    for ident,key in [('participation-category','categories'),('division','divisions'),('gender','genders'),('kit-size-id','kit_sizes'),('jogger-size-id','jogger_sizes')]:
        options=''.join(f'<option value="{escape(str(o["id"]))}">{escape(o["name"])}</option>' for o in data[key])
        html=re.sub(r'(<select id="'+ident+r'"[^>]*>).*?(</select>)',lambda m:m[1]+options+m[2],html,flags=re.S)
    return html+'<script>window.participationCatalog = '+json.dumps(data['participation'])+';'+''.join('const '+variable+' = '+json.dumps(data[key])+';' for variable,key in [('districtsByDivision','districts_by_division'),('tehsilsByDistrict','tehsils_by_district'),('ucsByDistrict','ucs_by_district')])+'</script>'


class ParticipationTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.PrivatePilotTests('test_pins_three_per_group_disjoint_and_no_replacements_on_rerun');f.setUp();self.addCleanup(f.doCleanups)
        self.f=f;self.profile=f.profiles[1];self.root=f.f.root
        app.sync(self.profile);self.c=app.load(self.profile);self.p=app.paths(self.c,self.profile)
        owner=self
        class Client:
            posts=0;gets=0;fail=False
            def get(self):self.gets+=1;return http.Response(200,form(owner.root/'catalog.json'))
            def post(self,*args):
                self.posts+=1
                if self.fail:raise TimeoutError('synthetic lost response')
                return http.Response(200,SUCCESS)
        self.Client=Client
        http.prepare(self.p['registration_profile'],self.Client())

    def cohort(self):
        with allocations(self.p['allocation_database'],readonly=True) as db:
            return ([tuple(r) for r in db.execute('SELECT * FROM allocations ORDER BY scheme,roll')],
                    [tuple(r) for r in db.execute('SELECT * FROM sport_assignments ORDER BY scheme,roll')])

    def test_open_changes_only_remaining_preserves_actual_private_receipt_and_resume(self):
        registered=http.run(self.p['registration_profile'],self.Client());self.assertEqual(registered['state'],'registered')
        history=self.cohort()
        with opened(self.p['registration_database']) as db:
            original=dict(jobs(db,self.c['scheme'],('registered',))[0]);before=participation.fingerprint(db)
            candidate=jobs(db,self.c['scheme'],('ready',))[0];begin(db,candidate)  # Ctrl+C before intent.
        client=self.Client();result=participation.apply(self.profile,'Open Category',client)
        self.assertEqual(result['ready_records'],14);self.assertEqual(result['completed_preserved'],1)
        self.assertEqual(client.posts,0);self.assertEqual(self.cohort(),history)
        settings=load(self.p['registration_profile']);self.assertEqual(settings['selection']['subcategory'],'Open Category')
        with input_snapshot(settings,self.p['registration_profile']) as (rows,*_):
            self.assertEqual(len(rows),14);self.assertTrue(all(r['SubCategory']=='Open Category' for r in rows))
            self.assertTrue(all(r['Institute'] for r in rows))
        with opened(self.p['registration_database']) as db:
            self.assertEqual(participation.fingerprint(db),before)
            self.assertEqual(dict(db.execute('SELECT * FROM jobs WHERE id=?',(original['id'],)).fetchone()),original)
            self.assertTrue(check_receipt(original['receipt']))
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE state='ready' AND json_extract(payload,'$.data.SubCategory')='Open Category'").fetchone()[0],14)
        with (self.p['output']/'Normalized.csv').open(encoding='utf-8-sig') as stream:
            categories={r['BForm']:r['SubCategory'] for r in __import__('csv').DictReader(stream)}
        self.assertEqual(categories[original['applicant_key']],'Private')
        self.assertEqual(app.normalization_settings(self.profile)['defaults']['SubCategory'],'Open Category')
        # Original public normalization entrypoint must honor the durable override.
        refreshed=private_pool.run(self.f.f.plan_path,'model_town')
        self.assertEqual(refreshed['subcategory'],'Open Category');self.assertEqual(refreshed['summary']['ready_to_register'],14)
        self.assertEqual(app.normalization_settings(self.f.profiles[0])['defaults']['SubCategory'],'Private')
        again=participation.apply(self.profile,'Open Category',self.Client())
        self.assertEqual(again['status'],'already_applied');self.assertEqual(again['ready_records'],14)
        with allocations(self.p['allocation_database'],readonly=True) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM participation_changes').fetchone()[0],1)
        from bise_allocate.resume import prepare
        self.assertTrue(prepare(self.profile)['input_reused'])

    def test_active_writer_blocks_change_before_profiles_are_edited(self):
        before=self.p['registration_profile'].read_bytes()
        with opened(self.p['registration_database']):
            with self.assertRaisesRegex(ValueError,'Another registration process'):participation.apply(self.profile,'Open Category',self.Client())
        self.assertEqual(self.p['registration_profile'].read_bytes(),before)
        self.assertIsNone(participation.current(self.p['allocation_database'],self.c['scheme']))

    def test_uncertain_prior_private_dispatch_is_not_silently_reclassified(self):
        client=self.Client();client.fail=True
        result=http.run(self.p['registration_profile'],client);self.assertEqual(result['state'],'uncertain')
        with self.assertRaisesRegex(ValueError,'submission intent/history'):participation.apply(self.profile,'Open Category',self.Client())
        self.assertIsNone(participation.current(self.p['allocation_database'],self.c['scheme']))
        self.assertEqual(load(self.p['registration_profile'])['selection']['subcategory'],'Private')

    def test_unrelated_operator_edits_are_preserved_before_override_is_recorded(self):
        value=json.loads(self.p['normalization_profile'].read_text());value['defaults']['Phone']='03000000000'
        self.p['normalization_profile'].write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'Unrelated normalization profile edits'):participation.apply(self.profile,'Open Category',self.Client())
        self.assertIsNone(participation.current(self.p['allocation_database'],self.c['scheme']))
        self.assertEqual(json.loads(self.p['normalization_profile'].read_text())['defaults']['Phone'],'03000000000')
