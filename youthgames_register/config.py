"""Strict, pinned operator profiles and immutable local input snapshots."""

from contextlib import contextmanager
from contextlib import ExitStack
import fcntl
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
from urllib.parse import urlparse

from PIL import Image
from . import MAX_WORKERS
from bise_normalize import COLUMNS
from bise_normalize.config import reject_unknown
from bise_normalize.sources import snapshots, digest


def sha(value):
    return hashlib.sha256(value).hexdigest()


def signature(value):
    return sha(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())


def resolved(profile_path, value):
    return (Path(profile_path).resolve().parent / value).resolve()


def validate_browser(browser):
    if (set(browser) != {'headless', 'workers', 'fresh_context_per_applicant'}
            or type(browser['headless']) is not bool
            or type(browser['workers']) is not int or not 1 <= browser['workers'] <= MAX_WORKERS
            or type(browser['fresh_context_per_applicant']) is not bool):
        raise ValueError(f'Use boolean headless/fresh_context_per_applicant settings and 1 to {MAX_WORKERS} workers')


def selection_accepts(config,key,value):
    selected=config['selection'][key]
    return value in selected if key=='sport' and isinstance(selected,list) else value==selected


def load(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    config.get('state', {}).setdefault('write_reports', False)
    shape = json.loads(Path(__file__).with_name('profile.template.json').read_text())
    reject_unknown(config, shape)
    if set(config) != set(shape) or config['schema_version'] != 1:
        raise ValueError('Use the complete schema-1 registration profile')
    portal = urlparse(config['portal'])
    if portal.scheme != 'https' or portal.netloc != 'youthgames.punjab.gov.pk' or portal.path not in ('', '/'):
        raise ValueError('Profile must target the official Youth Games registration page')
    if not config['profile'].strip() or not config['campaign'].strip():
        raise ValueError('Profile and campaign must be nonblank')
    for key in ('category','subcategory','sport'):
        selected=config['selection'][key]
        labels=selected if key=='sport' and isinstance(selected,list) else [selected]
        if not labels or any(not isinstance(v,str) or not v.strip() for v in labels) or len(labels)!=len(set(labels)):
            raise ValueError('Selection needs an explicit category/subcategory and sport label or unique sport allow-list')
    browser = config['browser']
    validate_browser(browser)
    if type(config['state']['write_reports']) is not bool:
        raise ValueError('state.write_reports must be boolean')
    from .photos import original,files
    if config['photos']['mode'] not in ('operator_selected_shared_graphic','original_sqlite_reference','original_file_reference') or config['photos']['individual_overrides'] is not None:
        raise ValueError('Unsupported photo provider')
    if files(config):
        if (not config['photos'].get('source_media_root') or config['photos'].get('source_exam')!={'year':2026,'course':'SSC','exam_type':'1'} or config['photos']['shared_file'] is not None):
            raise ValueError('File photos require a trusted media root and the explicit SSC 2026 Part-I scope')
        if not Path(config['photos']['source_media_root']).is_absolute():raise ValueError('Media root must be absolute')
    elif original(config) and (not config['photos'].get('source_database') or not config['photos'].get('source_sha256') or config['photos']['shared_file'] is not None):
        raise ValueError('Original-photo mode requires a pinned source DB and no shared fallback')
    pattern=config['selection'].get('dispatch_pattern',[])
    if pattern and (pattern!=['Raiwind','Model Town'] or not config['selection'].get('dispatch_key')):
        raise ValueError('Alternating dispatch requires Raiwind/Model Town and a durable key')
    if config['recovery'] != {'retry_ambiguous_post': False, 'stop_on_uncertain': True}:
        raise ValueError('Ambiguous submission retry is forbidden; stop_on_uncertain must be true')
    confirm = config['confirmation']
    if not all(confirm.get(k) is True for k in ('operator_reports_parent_student_consent', 'operator_confirmed_input_defaults', 'retain_original_provenance')):
        raise ValueError('The profile must record the existing operator confirmation')
    if confirm['input_sha256'] != config['input']['expected_csv_sha256']:
        raise ValueError('Operator confirmation must match the pinned CSV fingerprint')
    if not config['inspection']['refresh_rules_before_submit']:
        raise ValueError('Live rules must be refreshed before submit')
    if config['pacing']['submission_interval_seconds'] < 3 or not config['pacing']['honor_retry_after']:
        raise ValueError('Use at least three seconds spacing and honor Retry-After')
    if config['captcha']['mode'] not in ('auto', 'assist') or not 0 <= config['captcha']['max_explicit_rejection_retries'] <= 2:
        raise ValueError('CAPTCHA mode must be auto or assist, with at most two explicit-rejection retries')
    for label, alias in config['location_aliases'].items():
        if set(alias) != {'division', 'district', 'target_label', 'observed_value'} or not label or not alias['target_label']:
            raise ValueError('UC aliases need exact label, division, district and observed value')
    db = resolved(path, config['state']['database'])
    output = resolved(path, config['state']['output'])
    inputs = [path] + [resolved(path, config['input'][k]) for k in ('csv', 'manifest', 'provenance')]
    inputs += [resolved(path,config['photos']['source_media_root'] if files(config) else config['photos']['source_database'] if original(config) else config['photos']['shared_file'])]
    if any(p == db or p.is_relative_to(output) for p in inputs) or db.is_relative_to(output):
        raise ValueError('Registration output/state must be separate from inputs')
    return config


@contextmanager
def input_snapshot(config, profile_path, allow_empty=False):
    paths = {k: resolved(profile_path, config['input'][k]) for k in ('csv', 'manifest', 'provenance')}
    from .photos import original,files
    if not original(config):paths['photo'] = resolved(profile_path, config['photos']['shared_file'])
    with ExitStack() as locks,snapshots(paths) as copies:
        hashes = {k: v[1] for k, v in copies.items()}
        if files(config):
            hashes['photo']=config['photos']['expected_sha256']
        elif original(config):
            source=resolved(profile_path,config['photos']['source_database'])
            lock=locks.enter_context(source.with_suffix('.lock').open('rb'))
            fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
            if digest(source)!=config['photos']['source_sha256']:
                raise ValueError('Frozen original photo database changed')
            hashes['photo']=config['photos']['expected_sha256']
        if hashes['csv'] != config['input']['expected_csv_sha256']:
            raise ValueError('Normalized CSV changed; update and confirm the profile snapshot before preparing new applicants')
        manifest = json.loads(copies['manifest'][0].read_text())
        legacy = paths['manifest'].parent.name != 'Data'
        root = paths['manifest'].parent if legacy else paths['manifest'].parent.parent
        try:
            csv_key = paths['csv'].relative_to(root).as_posix()
            provenance_key = paths['provenance'].relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError('CSV/provenance must belong to the published manifest folder') from exc
        if legacy and paths['provenance'].name == 'provenance.jsonl':
            provenance_key = 'Data/provenance.jsonl'
        if manifest.get('files', {}).get(csv_key) != hashes['csv'] or manifest.get('files', {}).get(provenance_key) != hashes['provenance']:
            raise ValueError('Normalized CSV/provenance do not match their published manifest')
        if not original(config) and hashes['photo'] != config['photos']['expected_sha256']:
            raise ValueError('Configured shared image has changed')
        if not original(config):
            photo = copies['photo'][0]
            if photo.stat().st_size > min(config['photos']['max_bytes'], 262144):
                raise ValueError('Profile image exceeds the 256 KiB limit')
            with Image.open(photo) as image:
                if image.format not in ('JPEG', 'PNG'):
                    raise ValueError('Profile image must be a valid JPEG/PNG')
                image.verify()
        reader = csv.DictReader(io.StringIO(copies['csv'][0].read_text(encoding='utf-8-sig')))
        if reader.fieldnames != COLUMNS:
            raise ValueError('Input must use the exact normalized 17-column schema')
        rows = list(reader)
        provenance = [json.loads(line) for line in copies['provenance'][0].read_text().splitlines() if line.strip()]
        if len(rows) != len(provenance):
            raise ValueError('Normalized provenance row count differs from CSV')
        for row, origin in zip(rows, provenance):
            if None in row or any(v is None for v in row.values()) or any((origin['data'].get(k) or '') != row[k] for k in COLUMNS):
                raise ValueError('Normalized CSV row differs from its provenance; repair normalization first')
        if not rows and not allow_empty:
            raise ValueError('Normalized source is empty')
        yield rows, provenance, copies, hashes
