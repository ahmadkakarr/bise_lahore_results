"""BISE master -> normalized ready file -> pinned games input contracts."""
from contextlib import contextmanager, asynccontextmanager
import json
from pathlib import Path
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from PIL import Image
import test_portal_normalization as fixtures
from test_normalization import student
from test_youthgames import config as registration_template
from bise_normalize.sources import digest
from youthgames_register.config import load, input_snapshot
from youthgames_register.handoff import sync
from youthgames_register.app import prepare_in


class MasterHandoffTests(unittest.TestCase):
    setUp_base = fixtures.PortalNormalizationTests.setUp
    write_csv = fixtures.PortalNormalizationTests.write_csv
    write_source = fixtures.PortalNormalizationTests.write_source
    save_config = fixtures.PortalNormalizationTests.save_config
    save_catalog = fixtures.PortalNormalizationTests.save_catalog
    portal_state = fixtures.PortalNormalizationTests.portal_state
    # The shared fixture's setUp method calls its own base initializer.
    setUp_base_source = fixtures.PortalNormalizationTests.setUp_base

    def setUp(self):
        # Bind the fixture's expected base setup without inheriting its tests.
        self.setUp_base = self.setUp_base_source
        fixtures.PortalNormalizationTests.setUp(self)
        photo = self.root / 'photo.png'; Image.new('RGB', (3, 3)).save(photo)
        settings = registration_template()
        settings['photos'].update(shared_file=str(photo), expected_sha256=digest(photo))
        settings['state'].update(database=str(self.registration), output=str(self.root / 'old-registration-output'))
        self.settings = self.root / 'settings.json'; self.settings.write_text(json.dumps(settings))
        self.master_profile = self.root / 'profiles/master.json'

    def sync(self):
        self.save_config()
        return sync(self.master_profile, self.profile, self.settings)

    def test_sync_refreshes_master_and_binds_filtered_provenance_and_shared_db(self):
        result = self.sync(); self.assertEqual(result['ready_records'], 1)
        self.assertEqual(result['applicant_posts'], 0)
        settings = load(self.master_profile)
        self.assertEqual(settings['profile'], 'Master')
        self.assertEqual(Path(settings['input']['csv']).name, 'Ready to Register.csv')
        self.assertEqual(settings['confirmation']['input_sha256'], digest(self.output / 'Ready to Register.csv'))
        with input_snapshot(settings, self.master_profile) as (rows, origins, *_):
            self.assertEqual(len(rows), 1); self.assertEqual(origins[0]['roll'], '001235')
        self.assertEqual((self.master_profile.parent / settings['state']['database']).resolve(), self.registration)

    def test_unchanged_sync_preserves_profile_mtime_and_ready_hash(self):
        self.sync(); before = digest(self.master_profile); mtime = self.master_profile.stat().st_mtime_ns
        result = self.sync()
        self.assertEqual(result['status'], 'already_up_to_date')
        self.assertEqual(result['normalization_status'], 'already_up_to_date')
        self.assertEqual(digest(self.master_profile), before)
        self.assertEqual(self.master_profile.stat().st_mtime_ns, mtime)

    def test_appended_master_updates_pin_and_new_registered_row_is_removed(self):
        self.sync(); old = load(self.master_profile)['input']['expected_csv_sha256']
        self.rows.append(student('001238', bform='00000-0123460-1', **{'Date of Birth': '01/01/2010'})); self.write_source()
        self.assertEqual(self.sync()['ready_records'], 2)
        self.assertNotEqual(load(self.master_profile)['input']['expected_csv_sha256'], old)
        self.portal_state('0000001234572', 'registered')
        self.assertEqual(self.sync()['ready_records'], 1)
        with input_snapshot(load(self.master_profile), self.master_profile) as (rows, origins, *_):
            self.assertEqual(origins[0]['roll'], '001238')

    def test_operator_settings_are_preserved_and_selection_mismatch_keeps_old_profile(self):
        self.sync(); settings = load(self.master_profile); settings['pacing']['submission_interval_seconds'] = 7
        self.master_profile.write_text(json.dumps(settings))
        self.sync(); self.assertEqual(load(self.master_profile)['pacing']['submission_interval_seconds'], 7)
        before = self.master_profile.read_bytes()
        self.config['defaults']['Sport'] = 'Badminton'
        self.cat['data']['participation']['sports'].append({'id': '2', 'name': 'Badminton'})
        self.cat['data']['participation']['sportsByCategoryName']['Schools (Secondary)'].append('Badminton')
        self.save_catalog()
        with self.assertRaisesRegex(ValueError, 'settings differ'): self.sync()
        self.assertEqual(self.master_profile.read_bytes(), before)

    def test_campaign_and_db_mismatch_fail_before_handoff(self):
        self.sync(); settings = load(self.master_profile); settings['campaign'] = 'other-campaign'
        self.master_profile.write_text(json.dumps(settings)); before = self.master_profile.read_bytes()
        with self.assertRaisesRegex(ValueError, 'share campaign'): self.sync()
        self.assertEqual(self.master_profile.read_bytes(), before)

    def test_changed_photo_cannot_repin_profile(self):
        self.sync(); settings = load(self.master_profile)
        photo = (self.master_profile.parent / settings['photos']['shared_file']).resolve(); photo.write_bytes(b'changed')
        before = self.master_profile.read_bytes()
        with self.assertRaisesRegex(ValueError, 'image has changed'): self.sync()
        self.assertEqual(self.master_profile.read_bytes(), before)

    def test_empty_ready_master_is_pinned_without_reusing_old_queue(self):
        self.sync(); self.portal_state('0000001234572', 'registered')
        result = self.sync(); self.assertEqual(result['ready_records'], 0)
        with input_snapshot(load(self.master_profile), self.master_profile, allow_empty=True) as (rows, *_):
            self.assertEqual(rows, [])


class EmptyPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_stale_batch_or_browser_catalog_is_used_for_empty_input(self):
        from contextlib import contextmanager
        @contextmanager
        def empty(*args, **kwargs):
            self.assertTrue(kwargs['allow_empty'])
            yield [], [], {}, {}
        with patch('youthgames_register.app.input_snapshot', empty), patch('youthgames_register.app.web.catalog') as catalog:
            result = await prepare_in(None, None, {}, Path('unused.json'))
            self.assertTrue(result['empty_input']); self.assertEqual(result['counts'], {'total': 0})
            catalog.assert_not_called()

    async def test_empty_input_cannot_fall_back_to_previous_ready_batch(self):
        from youthgames_register.app import run
        @contextmanager
        def database(*args):
            db=MagicMock()
            db.execute.return_value.fetchone.return_value=(0,)
            yield db
        @asynccontextmanager
        async def browser(**kwargs):
            yield object()
        with patch('youthgames_register.app.load', return_value=registration_template()), patch('youthgames_register.app.opened', database), patch('youthgames_register.app.web.visible_browser', browser), patch('youthgames_register.app.prepare_in', AsyncMock(return_value={'counts': {'total': 0}, 'empty_input': True})), patch('youthgames_register.app.jobs', side_effect=AssertionError('must not reuse old batch')), patch('youthgames_register.app.process_one') as process:
            result = await run(Path('/tmp/master.json'), 'submit', 0)
            self.assertEqual(result['status'], 'nothing_to_register')
            self.assertEqual(result['attempted_now'], 0)
            process.assert_not_called()
