"""Explicit, repeatable handoff from a normalized ready master to registration."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile

from bise_normalize.app import execute
from bise_normalize.config import load as normalization_config, resolve
from bise_normalize.publish import locked
from .config import load, resolved, input_snapshot, selection_accepts
from .domain import form_values


def sync(profile_path, normalization_path, settings_path):
    """Refresh the ready master and bind a profile; never import jobs or send POSTs.

    Invoking sync selects the current normalization scope with the existing
    operator settings. Only input paths/hashes change on subsequent syncs.
    """
    profile_path, normalization_path = Path(profile_path).resolve(), Path(normalization_path).resolve()
    normalized = normalization_config(normalization_path)
    if not normalized['portal']['enabled']:
        raise ValueError('Registration handoff requires portal-aware normalization')
    destination = resolve(normalization_path, normalized['output']['folder'])
    if profile_path.is_relative_to(destination):
        raise ValueError('Registration profile must be outside normalization output')
    with locked(profile_path):
        existing = profile_path.exists()
        base_path = profile_path if existing else Path(settings_path).resolve()
        if base_path == profile_path and not existing:
            raise ValueError('Initial registration settings profile is missing')
        config = deepcopy(load(base_path))
        if not existing:
            config['profile'] = 'Master'
            # Rebase existing operator settings; preserve shared DB/campaign/photo.
            for section, field in [('photos', 'shared_file'), ('state', 'database')]:
                config[section][field] = os.path.relpath(resolved(base_path, config[section][field]), profile_path.parent)
            config['state']['output'] = os.path.relpath(destination.parent.parent / 'registration_output/Master', profile_path.parent)
            config['selection']['rolls'] = []
        if config['selection']['rolls']:
            raise ValueError('Master handoff requires an unrestricted roll selection')
        if config['campaign'] != normalized['portal']['campaign'] or resolved(profile_path, config['state']['database']) != resolve(normalization_path, normalized['portal']['registration_database']):
            raise ValueError('Normalizer and registration must share campaign and registration database')
        result = execute(normalization_path)
        manifest_path = destination / 'Data/manifest.json'
        manifest = json.loads(manifest_path.read_text())
        if not manifest.get('live_rules_checked'):
            raise ValueError('Normalized master has no portal rule validation')
        csv_path = destination / 'Ready to Register.csv'
        csv_hash = manifest['files'][csv_path.name]
        config['input'] = {
            'csv': os.path.relpath(csv_path, profile_path.parent),
            'manifest': os.path.relpath(manifest_path, profile_path.parent),
            'provenance': os.path.relpath(destination / 'Data/ready-to-register-provenance.jsonl', profile_path.parent),
            'expected_csv_sha256': csv_hash,
        }
        config['confirmation']['input_sha256'] = csv_hash
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix='.sync-master-', suffix='.json', dir=profile_path.parent)
        candidate = Path(temporary)
        try:
            with os.fdopen(handle, 'w') as stream:
                stream.write(json.dumps(config, indent=2, ensure_ascii=False) + '\n')
            load(candidate)
            with input_snapshot(config, candidate, allow_empty=True) as (rows, origins, _, _):
                seen, scopes = set(), set()
                for row, origin in zip(rows, origins):
                    if not origin.get('portal', {}).get('ready') or origin['portal']['age']['state'] != 'eligible' or origin.get('issues') or origin['portal'].get('issues'):
                        raise ValueError('Ready master contains a blocked/ineligible row')
                    form_values(row)
                    if row['BForm'] in seen:
                        raise ValueError('Ready master contains duplicate BForms')
                    seen.add(row['BForm'])
                    for field, key in [('Category', 'category'), ('SubCategory', 'subcategory'), ('Sport', 'sport')]:
                        if not selection_accepts(config,key,row[field]):
                            raise ValueError('Registration settings differ from normalized master: ' + field)
                    from .institutes import institute_fields
                    institute_fields(row)
                    scopes.add(tuple(row[k] for k in ('Division', 'District', 'Tehsil')))
                if len(scopes) > 1:
                    raise ValueError('Current registration adapter requires one location scope per master')
                count = len(rows)
            unchanged = existing and load(profile_path) == config
            if not unchanged:
                candidate.replace(profile_path)
            return {'status': 'already_up_to_date' if unchanged else 'synced',
                    'normalization_status': result['status'], 'ready_records': count,
                    'ready_master': str(csv_path), 'registration_config': str(profile_path),
                    'csv_sha256': csv_hash, 'applicant_posts': 0}
        finally:
            candidate.unlink(missing_ok=True)
