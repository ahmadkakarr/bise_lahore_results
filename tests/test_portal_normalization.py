"""Offline portal eligibility, incremental cache, read-only registration state and handoff."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import csv
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_normalization as base
from test_normalization import student
from bise_normalize.app import execute
from bise_normalize.publish import hashes
from bise_normalize.sources import digest
from bise_normalize.state import registration_snapshot, status
from youthgames_shared.catalog import PORTAL, signature, age_eligibility, validate_fields, load


def catalog():
    pairs = lambda values: [{'id': str(i + 1), 'name': n} for i, n in enumerate(values)]
    rule = {'min_dob': '2009-01-01', 'max_dob': '2016-12-31', 'cutoff_date': '2026-12-31'}
    data = {'categories': pairs(['Schools (Secondary)']), 'divisions': pairs(['Lahore']),
      'genders': pairs(['Male', 'Female']), 'kit_sizes': pairs(['Small', 'Medium', 'Large']),
      'jogger_sizes': pairs([str(i) for i in range(33, 47)]),
      'districts_by_division': {'1': pairs(['Lahore'])}, 'tehsils_by_district': {'1': pairs(['Shalimar'])},
      'ucs_by_district': {'1': pairs(['UC No. 178, Manawan Ram Pur Jageer'])},
      'aliases': {'UC': {'UC No. 178, Manawan': {'Division': 'Lahore', 'District': 'Lahore', 'target': 'UC No. 178, Manawan Ram Pur Jageer'}}},
      'participation': {'subCategories': pairs(['Open Category']), 'sports': pairs(['Athletics']),
        'subCategoriesByCategoryName': {'Schools (Secondary)': ['Open Category']},
        'sportsByCategoryName': {'Schools (Secondary)': ['Athletics']},
        'showUcByCategoryName': {'Schools (Secondary)': True},
        'dobEligibilityRulesByCategoryName': {'Schools (Secondary)': rule}}}
    return {'schema_version': 1, 'source_url': PORTAL + '/#registration', 'fetched_at': datetime.now(timezone.utc).isoformat(), 'data': data, 'content_sha256': signature(data)}


class PortalNormalizationTests(unittest.TestCase):
    # Reuse the synthetic master/profile fixture, without inheriting its test methods.
    setUp_base = base.NormalizationTests.setUp
    write_csv = base.NormalizationTests.write_csv
    write_source = base.NormalizationTests.write_source
    save_config = base.NormalizationTests.save_config
    exported = base.NormalizationTests.exported
    provenance = base.NormalizationTests.provenance
    run_normalize = base.NormalizationTests.run_normalize

    def setUp(self):
        self.setUp_base()
        self.cat = catalog(); self.catpath = self.root / 'catalog.json'; self.save_catalog()
        self.registration = self.root / 'registrations.sqlite3'
        db = sqlite3.connect(self.registration)
        db.execute('CREATE TABLE jobs(portal TEXT,campaign TEXT,applicant_key TEXT,state TEXT)')
        db.commit(); db.close()
        self.ledger = self.root / 'normalization.sqlite3'
        self.config['portal'].update(enabled=True, normalization_database=str(self.ledger), registration_database=str(self.registration))
        self.config['validation'].update(catalog=str(self.catpath), eligibility={'mode': 'catalog'})
        self.save_config()

    def save_catalog(self):
        self.cat['content_sha256'] = signature(self.cat['data'])
        self.catpath.write_text(json.dumps(self.cat))

    def portal_state(self, key, state):
        with closing(sqlite3.connect(self.registration)) as db, db:
            db.execute('DELETE FROM jobs WHERE applicant_key=?', (key,))
            db.execute('INSERT INTO jobs VALUES (?,?,?,?)', (PORTAL, self.config['portal']['campaign'], key, state))

    def test_age_counts_separate_complete_and_exact_uc(self):
        result = self.run_normalize(); counts = result['summary']
        self.assertEqual((counts['normalized'], counts['complete'], counts['age_eligible'], counts['age_ineligible'], counts['ready_to_register']), (2, 2, 1, 1, 1))
        ready = self.exported('Ready to Register.csv'); self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]['UC'], 'UC No. 178, Manawan Ram Pur Jageer')
        self.assertEqual(ready[0]['BForm'], '0000001234572')
        rejected = self.exported('Ineligible.csv')[0]
        self.assertEqual(rejected['Roll No'], '001234'); self.assertEqual(rejected['Minimum DOB'], '2009-01-01')
        self.assertEqual(rejected['Reason'], 'outside_category_dob_bounds')
        self.assertEqual(self.provenance()[1]['portal']['canonical_changes']['UC']['from'], 'UC No. 178, Manawan')

    def test_all_registration_protected_states_excluded_ready_but_not_age_eligible(self):
        for state in ('registered', 'already_registered_external', 'uncertain', 'submitting', 'filling', 'filled', 'rejected', 'blocked_input'):
            self.portal_state('0000001234572', state)
            result = self.run_normalize()
            self.assertEqual(result['summary']['age_eligible'], 1)
            self.assertEqual(result['summary']['ready_to_register'], 0, state)
        self.portal_state('0000001234572', 'ready')
        self.assertEqual(self.run_normalize()['summary']['ready_to_register'], 1)

    def test_unchanged_rerun_preserves_all_hashes_and_mtimes(self):
        self.run_normalize(); before = hashes(self.output)
        mtimes = {str(p): p.stat().st_mtime_ns for p in self.output.rglob('*') if p.is_file()}
        result = self.run_normalize()
        self.assertEqual(result['status'], 'already_up_to_date')
        self.assertEqual(result['cache']['transformed'], 0)
        self.assertEqual(hashes(self.output), before)
        self.assertEqual(mtimes, {str(p): p.stat().st_mtime_ns for p in self.output.rglob('*') if p.is_file()})

    def test_master_append_only_transforms_new_rows(self):
        self.run_normalize()
        self.rows.append(student('001238', bform='00000-0123460-1', **{'Date of Birth': '01/01/2010'})); self.write_source()
        result = self.run_normalize()
        self.assertEqual(result['cache'], {'reused': 2, 'transformed': 1})
        self.assertEqual(result['summary']['age_eligible'], 2)

    def test_changed_config_retransforms_but_catalog_only_reuses(self):
        self.run_normalize(); self.config['defaults']['Address'] = 'Confirmed Address'
        result = self.run_normalize(); self.assertEqual(result['cache']['transformed'], 2)
        self.cat['data']['participation']['dobEligibilityRulesByCategoryName']['Schools (Secondary)']['min_dob'] = '2008-01-01'; self.save_catalog()
        result = self.run_normalize(); self.assertEqual(result['cache'], {'reused': 2, 'transformed': 0})
        self.assertEqual(result['summary']['age_eligible'], 2)

    def test_changed_registration_state_is_read_only_and_reuses_transforms(self):
        self.run_normalize(); self.portal_state('0000001234572', 'registered')
        before = digest(self.registration)
        result = self.run_normalize()
        self.assertEqual(result['cache'], {'reused': 2, 'transformed': 0})
        self.assertEqual(result['summary']['ready_to_register'], 0)
        self.assertEqual(digest(self.registration), before)
        indexed = status(self.ledger, 'Lahore', bform='0000001234572')['published_snapshot_counts']
        self.assertEqual(indexed[0]['registration_state'], 'registered')

    def test_failed_publish_rolls_back_ledger_and_restores_output(self):
        self.run_normalize(); before = hashes(self.output)
        with closing(sqlite3.connect(self.ledger)) as db, db:
            old = db.execute('select * from runs').fetchall()
        self.rows.append(student('001238', bform='00000-0123460-1')); self.write_source()
        with patch('bise_normalize.publish.move_staging', side_effect=OSError('synthetic rename failure')):
            with self.assertRaises(OSError): self.run_normalize()
        self.assertEqual(hashes(self.output), before)
        with closing(sqlite3.connect(self.ledger)) as db, db:
            self.assertEqual(db.execute('select * from runs').fetchall(), old)
        self.assertEqual(self.run_normalize()['summary']['normalized'], 3)

    def test_age_unknown_preserved_and_partial_age_eligible_excluded_ready(self):
        self.rows[0]['Date of Birth'] = 'bad date'
        self.rows[1]['Student CNIC / B-Form'] = ''
        self.write_source(); counts = self.run_normalize()['summary']
        self.assertEqual((counts['normalized'], counts['age_unknown'], counts['age_eligible'], counts['eligible']), (2, 1, 1, 0))
        self.assertEqual(len(self.exported('Age Unknown.csv')), 1)
        self.assertEqual(len(self.exported('Age Eligible.csv')), 1)

    def test_catalog_stale_or_modified_and_missing_registration_db_fail_closed(self):
        self.cat['fetched_at'] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(); self.save_catalog()
        with self.assertRaisesRegex(ValueError, 'expired'): self.run_normalize()
        self.cat = catalog(); self.save_catalog(); self.cat['data']['genders'] = []; self.catpath.write_text(json.dumps(self.cat))
        with self.assertRaisesRegex(ValueError, 'hash mismatch'): self.run_normalize()
        self.cat = catalog(); self.save_catalog(); self.registration.unlink()
        with self.assertRaisesRegex(ValueError, 'database is missing'): self.run_normalize()

    def test_missing_ledger_recovers_without_republishing_output(self):
        self.run_normalize(); before = hashes(self.output); mtime = (self.output / 'Normalized.xlsx').stat().st_mtime_ns
        self.ledger.unlink(); result = self.run_normalize()
        self.assertEqual(result['summary']['normalized'], 2)
        self.assertEqual(hashes(self.output), before)
        self.assertEqual((self.output / 'Normalized.xlsx').stat().st_mtime_ns, mtime)

    def test_cross_profile_reuses_transforms_and_registration_identity(self):
        self.run_normalize(); self.config['profile'] = 'Second'
        self.config['output']['folder'] = str(self.root / 'normalized_output/Second')
        self.portal_state('0000001234572', 'registered')
        result = self.run_normalize()
        self.assertEqual(result['cache'], {'reused': 2, 'transformed': 0})
        self.assertEqual(result['summary']['ready_to_register'], 0)
        with closing(sqlite3.connect(self.ledger)) as db, db:
            self.assertEqual(db.execute('select count(*) from people').fetchone()[0], 2)
            self.assertEqual(db.execute('select count(*) from evaluations').fetchone()[0], 4)

    def test_filtered_csv_provenance_supported_by_uploader(self):
        from PIL import Image
        from youthgames_register.config import input_snapshot
        self.run_normalize()
        photo = self.root / 'photo.png'; Image.new('RGB', (2, 2)).save(photo)
        config = {'input': {'csv': str(self.output / 'Ready to Register.csv'), 'provenance': str(self.output / 'Data/ready-to-register-provenance.jsonl'), 'manifest': str(self.output / 'Data/manifest.json'), 'expected_csv_sha256': digest(self.output / 'Ready to Register.csv')},
                  'photos': {'shared_file': str(photo), 'max_bytes': 262144, 'expected_sha256': digest(photo)}}
        with input_snapshot(config, self.profile) as (rows, origins, *_):
            self.assertEqual(len(rows), 1); self.assertEqual(origins[0]['roll'], '001235')

    def test_registration_wal_snapshot_includes_only_committed_rows(self):
        writer = sqlite3.connect(self.registration)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('INSERT INTO jobs VALUES (?,?,?,?)', (PORTAL, self.config['portal']['campaign'], '0000001234572', 'registered')); writer.commit()
        writer.execute('INSERT INTO jobs VALUES (?,?,?,?)', (PORTAL, self.config['portal']['campaign'], '0000001234581', 'ready'))
        try:
            found = registration_snapshot(self.registration, self.config['portal']['campaign'])
            self.assertEqual(found, {'0000001234572': 'registered'})
        finally:
            writer.rollback(); writer.close()


class CatalogTests(unittest.TestCase):
    def test_inclusive_age_boundaries_and_unknown_dob(self):
        for dob, expected in [('2009-01-01', 'eligible'), ('2016-12-31', 'eligible'), ('2008-12-31', 'ineligible'), ('2017-01-01', 'ineligible'), ('', 'unknown')]:
            self.assertEqual(age_eligibility({'Category': 'Schools (Secondary)', 'DOB': dob}, catalog())['state'], expected)

    def test_unknown_option_or_wrong_alias_parent_never_fuzzy_matches(self):
        from test_youthgames import data
        row = data(); values, ids, issues, changes = validate_fields(row, catalog())
        self.assertFalse(issues); self.assertEqual(ids['UC'], '1')
        row['District'] = 'Kasur'
        issues = validate_fields(row, catalog())[2]
        self.assertIn('portal_alias_parent_scope:UC', issues); self.assertIn('portal_option:UC', issues)
        row = data(); row['UC'] = 'Manawan'
        self.assertIn('portal_option:UC', validate_fields(row, catalog())[2])
