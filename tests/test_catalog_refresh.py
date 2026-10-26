"""Expired catalog recovery: verified GET only; preserve strict integrity checks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from youthgames_shared import catalog


class CatalogRefreshTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'catalog.json'
        self.value = json.loads(Path('data/youthgames_catalog.json').read_text())
        self.value['fetched_at'] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.value))

    def mocks(self):
        client = patch('httpx.Client').start()
        self.addCleanup(patch.stopall)
        response = client.return_value.__enter__.return_value.get.return_value
        response.text = 'synthetic form'
        parser = patch('youthgames_register.http_pilot.form_catalog',
                       return_value=(None, None, deepcopy(self.value['data']))).start()
        return client, response, parser

    def test_expired_catalog_refreshes_once_then_reuses_without_network(self):
        client, _, _ = self.mocks()
        refreshed = catalog.ensure_fresh(self.path)
        self.assertEqual(refreshed['content_sha256'], self.value['content_sha256'])
        self.assertEqual(catalog.load(self.path), refreshed)
        self.assertEqual(catalog.ensure_fresh(self.path), refreshed)
        api = client.return_value.__enter__.return_value
        client.assert_called_once_with(timeout=30, verify=True, trust_env=False, follow_redirects=False)
        api.get.assert_called_once_with(catalog.PORTAL + '/')
        api.post.assert_not_called()
        self.assertEqual(refreshed['data']['aliases'], self.value['data']['aliases'])

    def test_strict_load_keeps_expired_error_for_offline_validation(self):
        with self.assertRaises(catalog.CatalogExpired):
            catalog.load(self.path)

    def test_corrupt_catalog_is_not_silently_repaired(self):
        self.value['data']['genders'] = []
        self.save()
        with patch('httpx.Client') as client, self.assertRaisesRegex(ValueError, 'hash mismatch'):
            catalog.ensure_fresh(self.path)
        client.assert_not_called()

    def test_future_timestamp_does_not_trigger_refresh(self):
        self.value['fetched_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.save()
        with patch('httpx.Client') as client, self.assertRaisesRegex(ValueError, 'future'):
            catalog.ensure_fresh(self.path)
        client.assert_not_called()

    def test_network_failure_retains_previous_catalog(self):
        client, response, _ = self.mocks()
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            'unavailable', request=httpx.Request('GET', catalog.PORTAL), response=httpx.Response(503))
        original = self.path.read_bytes()
        with self.assertRaises(httpx.HTTPStatusError):
            catalog.ensure_fresh(self.path)
        self.assertEqual(original, self.path.read_bytes())
        client.return_value.__enter__.return_value.post.assert_not_called()

    def test_changed_form_contract_retains_original_and_no_temporary_files(self):
        _, _, parser = self.mocks()
        parser.side_effect = ValueError('Live form contract changed')
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'contract changed'):
            catalog.ensure_fresh(self.path)
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_missing_age_rules_are_rejected_without_replacing_catalog(self):
        _, _, parser = self.mocks()
        changed = deepcopy(self.value['data'])
        changed['participation'].pop('dobEligibilityRulesByCategoryName')
        parser.return_value = None, None, changed
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'participation catalog changed'):
            catalog.ensure_fresh(self.path)
        self.assertEqual(original, self.path.read_bytes())

    def test_allocation_sync_refreshes_before_normalization(self):
        from bise_allocate import app
        self.mocks()
        registration = self.root / 'registration.json'
        registration.write_text('{}')
        paths = {'catalog': self.path, 'registration_profile': registration,
                 'normalization_profile': self.root / 'normal.json',
                 'registration_settings': self.root / 'settings.json'}
        def handoff(*args):
            catalog.load(self.path)
            return {'applicant_posts': 0}
        with patch.object(app, 'load', return_value={}), patch.object(app, 'paths', return_value=paths), \
                patch.object(app, 'normalization_profile'), \
                patch('youthgames_register.handoff.sync', side_effect=handoff):
            self.assertEqual(app.sync(self.root / 'allocation.json'), {'applicant_posts': 0})


if __name__ == '__main__':
    unittest.main()
