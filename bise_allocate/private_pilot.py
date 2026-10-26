"""Explicit bounded live Private pilot; immutable input prevents replacement on rerun."""
from copy import deepcopy
from contextlib import closing
import csv
import io
import json
import os
from pathlib import Path
import sqlite3

from bise_normalize.publish import locked
from youthgames_register.config import load, resolved, input_snapshot, sha
from youthgames_register.store import check_receipt
from . import app
from .store import opened, now


def read_jobs(database, campaign, keys):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return {r['applicant_key']: dict(r) for r in db.execute(
            "SELECT * FROM jobs WHERE campaign=? AND portal=? AND applicant_key IN (" + ','.join('?' for _ in keys) + ')',
            (campaign, 'https://youthgames.punjab.gov.pk', *keys))} if keys else {}


def verify(profile):
    config = load(profile)
    with input_snapshot(config, profile) as (rows, origins, *_):
        keys = [r['BForm'] for r in rows]
        database=resolved(profile, config['state']['database'])
        jobs = read_jobs(database, config['campaign'], keys)
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
            attempts={job['id']:db.execute('SELECT count(*) FROM attempts WHERE job=? AND dispatched_at IS NOT NULL',(job['id'],)).fetchone()[0] for job in jobs.values()}
        checks = []
        for row, origin in zip(rows, origins):
            job = jobs.get(row['BForm'], {})
            valid = bool(job.get('state') == 'registered' and job.get('tracking') and check_receipt(job.get('receipt')))
            if valid:
                from youthgames_register.domain import classify
                receipt = json.loads(Path(job['receipt']).read_text())
                response = Path(receipt['files']['response.html']['path']).read_text()
                outcome = classify(response, receipt['http_status'])
                valid = outcome['state'] == 'registered' and outcome['tracking'] == job['tracking']
                proof=json.loads(Path(receipt['files']['http-request-proof.json']['path']).read_text())
                valid = valid and proof.get('institute_submission') == {'mode':'private_manual','institution_id':'','institute_name':row['Institute']}
                payload = json.loads(job['payload'])
                valid = valid and payload['data'] == row and payload['administrative_allocation'] == origin['administrative_allocation']
            checks.append({'roll': origin['roll'], 'uc': row['UC'], 'institute': row['Institute'],
                           'sport': row['Sport'], 'state': job.get('state', 'not_prepared'),
                           'tracking': job.get('tracking'), 'receipt_valid': valid, 'prior_dispatches': attempts.get(job.get('id'),0)})
        return {'checks': checks, 'pilot_passed': len(checks) == 3 and all(c['receipt_valid'] for c in checks)}


def prepare(allocation_profile):
    c = app.load(allocation_profile)
    if not c.get('source_pool') or c['subcategory'] != 'Private':
        raise ValueError('Private pilot requires the frozen Private allocation profile')
    p = app.paths(c, allocation_profile)
    pilot_profile = p['registration_profile'].with_name(p['registration_profile'].stem + '_pilot.json')
    # Lock covers selection + publication, preventing simultaneous operators from
    # pinning different students. Original input is never regenerated after POST.
    with locked(pilot_profile):
        with opened(p['allocation_database']) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS private_registration_pilots(
                scheme TEXT NOT NULL, position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 3),
                roll TEXT NOT NULL, applicant_key TEXT NOT NULL, profile TEXT NOT NULL,
                profile_sha256 TEXT NOT NULL, selected_at TEXT NOT NULL,
                PRIMARY KEY(scheme,position), UNIQUE(scheme,applicant_key))''')
            for action in ('UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS protect_private_pilot_{action.lower()} BEFORE {action} ON private_registration_pilots BEGIN SELECT RAISE(ABORT,'Private pilot selection is immutable'); END")
            db.execute("CREATE TRIGGER IF NOT EXISTS protect_private_pilot_replace BEFORE INSERT ON private_registration_pilots WHEN EXISTS(SELECT 1 FROM private_registration_pilots p WHERE p.scheme=NEW.scheme AND p.position=NEW.position) BEGIN SELECT RAISE(ABORT,'Private pilot selection already exists'); END")
            pins = list(db.execute('SELECT * FROM private_registration_pilots WHERE scheme=? ORDER BY position', (c['scheme'],)))
            if pins:
                if len(pins) != 3 or not pilot_profile.exists() or any(r['profile_sha256'] != sha(pilot_profile.read_bytes()) for r in pins):
                    raise ValueError('Pilot pins/profile integrity mismatch; preserve original selection')
                config = load(pilot_profile)
                with input_snapshot(config, pilot_profile) as (rows, origins, *_):
                    if [(o['roll'], r['BForm']) for r, o in zip(rows, origins)] != [(r['roll'], r['applicant_key']) for r in pins]:
                        raise ValueError('Pilot selection differs from immutable database pins')
                return pilot_profile
        if p['registration_profile'] in (Path(allocation_profile).resolve(),p['normalization_profile']):
            raise ValueError('Allocation, normalization and registration profiles must be separate files')
        app.sync(allocation_profile)
        config = load(p['registration_profile'])
        with input_snapshot(config, p['registration_profile']) as (rows, origins, *_):
            prior = read_jobs(p['registration_database'], config['campaign'], [r['BForm'] for r in rows])
            chosen, ucs, schools = [], set(), set()
            for row, origin in sorted(zip(rows, origins), key=lambda pair: (pair[1]['administrative_allocation']['uc_number'], pair[1]['roll'])):
                if row['BForm'] in prior or row['UC'] in ucs or row['Institute'] in schools:
                    continue
                chosen.append((row, origin)); ucs.add(row['UC']); schools.add(row['Institute'])
                if len(chosen) == 3: break
            if len(chosen) != 3:
                raise ValueError('Need three fresh students in distinct UCs/institutes; no POST was sent')
            root = p['registration_profile'].parent.parent / 'data/registration_inputs' / (p['registration_profile'].stem + '_private_pilot')
            (root / 'Data').mkdir(parents=True, exist_ok=True)
            text = io.StringIO(newline=''); writer = csv.DictWriter(text, fieldnames=list(chosen[0][0])); writer.writeheader(); writer.writerows(r for r, _ in chosen)
            files = {'Pilot.csv': text.getvalue(), 'Data/provenance.jsonl': ''.join(json.dumps(o, ensure_ascii=False) + '\n' for _, o in chosen)}
            manifest = {'files': {name: sha(value.encode()) for name, value in files.items()}, 'pilot_count': 3}
            files['Data/manifest.json'] = json.dumps(manifest, indent=2)
            for name, value in files.items():
                target = root / name
                # Crash before DB pin can leave files. Reuse only identical bytes.
                if target.exists() and target.read_text() != value:
                    raise ValueError('Existing unpinned pilot input differs; inspect before replacing')
                if not target.exists(): _write_once(target, value)
            config = deepcopy(config); config['profile'] = c['scheme'] + ' Private Pilot'
            config['input'] = {k: os.path.relpath(root / v, pilot_profile.parent) for k, v in [('csv','Pilot.csv'), ('manifest','Data/manifest.json'), ('provenance','Data/provenance.jsonl')]}
            config['input']['expected_csv_sha256'] = manifest['files']['Pilot.csv']
            config['confirmation']['input_sha256'] = manifest['files']['Pilot.csv']
            config['selection']['rolls'] = [o['roll'] for _, o in chosen]
            config['state']['output'] = os.path.relpath(root.parent.parent.parent / 'registration_output' / config['profile'], pilot_profile.parent)
            config['captcha']['max_explicit_rejection_retries'] = 0
            app.write_profile(pilot_profile, config)
            # Full input validation is mandatory before pin/any registration import.
            with input_snapshot(load(pilot_profile), pilot_profile): pass
            with opened(p['allocation_database']) as db, db:
                for position, (row, origin) in enumerate(chosen, 1):
                    db.execute('INSERT INTO private_registration_pilots VALUES (?,?,?,?,?,?,?)',
                               (c['scheme'], position, origin['roll'], row['BForm'], str(pilot_profile), sha(pilot_profile.read_bytes()), now()))
            return pilot_profile


def _write_once(path, text):
    with path.open('x') as stream:
        stream.write(text); stream.flush(); os.fsync(stream.fileno())


async def run(allocation_profile, connections=None):
    profile = prepare(allocation_profile)
    verified = verify(profile)
    if verified['pilot_passed']:
        return {'status': 'already_verified', 'applicant_posts': 0, 'profile': str(profile), **verified}
    # Unknown pilot attempts are retained; never substitute or replay them.
    if any(c['state'] not in ('not_prepared', 'ready', 'registered') or (c.get('prior_dispatches',0) and c['state']!='registered') for c in verified['checks']):
        return {'status': 'held', 'applicant_posts': 0, 'profile': str(profile), **verified}
    from youthgames_register.http_async import run as submit
    result = await submit(profile, workers=2 if connections else 1, limit=3, connections=connections,
                          prepare_ahead=1, max_in_flight=1, dispatch_interval=6,
                          auto_recover=True, retry_uncertain=True, uncertain_retries=0)
    verified = verify(profile)
    return {'status': result['status'], 'profile': str(profile), 'submission': result, **verified}
