"""Bounded one-applicant HTTP pilot using the existing registration ledger."""

import argparse
import ast
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
import http.cookiejar
import io
import json
from pathlib import Path
import re
import secrets
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlparse

from PIL import Image
from bise_results.parser import Document
from youthgames_shared.catalog import validate_fields, age_eligibility, signature
from .app import attempt_folder, seal_snapshot
from .browser import arithmetic_answer
from .config import load, resolved, input_snapshot, sha, selection_accepts
from .domain import SELECTS, TEXT, form_values, classify, redact_tokens, uc_label
from .reports import output_folder, export
from .institutes import institute_fields, private_manual_supported
from .store import opened, jobs, begin, intent, dispatched, finish, now, import_batch
from .photos import applicant_photo

BASE = 'https://youthgames.punjab.gov.pk'


class Literal:
    """Read data-only JS literals; expressions, calls and execution are forbidden."""
    def __init__(self, source):
        self.source, self.pos = source, 0

    def space(self):
        while self.pos < len(self.source) and self.source[self.pos].isspace(): self.pos += 1

    def value(self, depth=0):
        if depth > 20: raise ValueError('Catalog nesting exceeds limit')
        self.space()
        char = self.source[self.pos:self.pos + 1]
        if char in ('"', "'"):
            match = re.match(r'''"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*' ''', self.source[self.pos:], re.X)
            if not match: raise ValueError('Invalid catalog string')
            self.pos += len(match[0])
            return json.loads(match[0]) if char == '"' else ast.literal_eval(match[0].replace(r'\/', '/'))
        if char in ('{', '['):
            self.pos += 1; result = {} if char == '{' else []; end = '}' if char == '{' else ']'
            while True:
                self.space()
                if self.source[self.pos:self.pos + 1] == end: self.pos += 1; return result
                if char == '{':
                    if self.source[self.pos:self.pos + 1] in ('"', "'"): key = self.value(depth + 1)
                    else:
                        match = re.match(r'[A-Za-z_][A-Za-z0-9_]*', self.source[self.pos:])
                        if not match: raise ValueError('Invalid catalog key')
                        key = match[0]; self.pos += len(key)
                    if key in result: raise ValueError('Duplicate catalog key')
                    self.space()
                    if self.source[self.pos:self.pos + 1] != ':': raise ValueError('Expected catalog colon')
                    self.pos += 1; result[key] = self.value(depth + 1)
                else: result.append(self.value(depth + 1))
                self.space(); token = self.source[self.pos:self.pos + 1]
                if token == end: self.pos += 1; return result
                if token != ',': raise ValueError('Catalog contains a non-literal expression')
                self.pos += 1
        for token, value in [('true', True), ('false', False), ('null', None)]:
            if re.match(token + r'\b', self.source[self.pos:]): self.pos += len(token); return value
        match = re.match(r'-?\d+(?:\.\d+)?\b', self.source[self.pos:])
        if match: self.pos += len(match[0]); return json.loads(match[0])
        raise ValueError('Catalog contains unsupported executable syntax')


def literal_assignment(html, name):
    matches = list(re.finditer(r'(?:window\.|const\s+|let\s+|var\s+)' + re.escape(name) + r'\s*=\s*', html))
    if len(matches) != 1: raise ValueError('Missing or ambiguous live catalog: ' + name)
    reader = Literal(html[matches[0].end():]); value = reader.value(); reader.space()
    if reader.source[reader.pos:reader.pos + 1] != ';': raise ValueError('Non-literal catalog assignment')
    return value


def form_catalog(html):
    doc = Document(html); form = doc.ids.get('registration-form')
    if form is None or {k: form.attrs.get(k, '').lower() for k in ('action', 'method', 'enctype')} != {
        'action': BASE + '/applicant', 'method': 'post', 'enctype': 'multipart/form-data'}:
        raise ValueError('Live form contract changed')
    nodes = list(form.elements('input')) + list(form.elements('select')) + list(form.elements('textarea'))
    named = {n.attrs['name']: n for n in nodes if n.attrs.get('name')}
    if len(named) != sum(bool(n.attrs.get('name')) for n in nodes): raise ValueError('Duplicate form field')
    def choices(ident):
        node = doc.ids[ident]
        return [{'id': str(o.attrs.get('value', '')), 'name': o.text()} for o in node.elements('option') if o.attrs.get('value') and 'disabled' not in o.attrs]
    root = {key: choices(ident) for key, ident in [('categories', 'participation-category'), ('divisions', 'division'),
        ('genders', 'gender'), ('kit_sizes', 'kit-size-id'), ('jogger_sizes', 'jogger-size-id')]}
    root['participation'] = literal_assignment(html, 'participationCatalog')
    for key, variable in [('districts_by_division', 'districtsByDivision'), ('tehsils_by_district', 'tehsilsByDistrict'), ('ucs_by_district', 'ucsByDistrict')]:
        root[key] = literal_assignment(html, variable)
    return doc, named, root


def prepare_form(html, config, item):
    doc, named, root = form_catalog(html)
    submitted = {**item['data'], 'UC': uc_label(item['data'], config)}
    canonical, ids, issues, _ = validate_fields(submitted, {'data': root})
    age = age_eligibility(canonical, {'data': root})
    if issues or age['state'] != 'eligible': raise ValueError('Live field/eligibility validation failed: ' + ','.join(issues))
    rule = age['rule']
    if {'min': rule['min_dob'], 'max': rule['max_dob']} != item['dob_rule']:
        raise ValueError('Live DOB rule differs from prepared applicant')
    if rule['min_dob'] != config['inspection']['observed_secondary_dob_min'] or rule['max_dob'] != config['inspection']['observed_secondary_dob_max']:
        raise ValueError('Live DOB rule differs from operator profile')
    if ids != item['option_ids']: raise ValueError('Live option IDs differ from prepared applicant')
    if not re.search(r'maxProfilePhotoSize\s*=\s*256\s*\*\s*1024', html): raise ValueError('Live photo limit changed')
    institutes = institute_fields(item['data'], private_manual_supported(html))
    fields = {}
    for key, ident in SELECTS.items():
        fields[doc.ids[ident].attrs['name']] = ids.get(key,'') if key=='UC' and not root['participation'].get('showUcByCategoryName',{}).get(canonical['Category'],True) else ids[key]
    for key, value in form_values(item['data']).items(): fields[doc.ids[TEXT[key]].attrs['name']] = value
    if not named.get('_token') or not named['_token'].attrs.get('value'): raise ValueError('Fresh CSRF token missing')
    for key in ('institution_id', 'institute_name'):
        if key in named:
            if named[key].attrs.get('value', ''): raise ValueError('Unexpected institute value')
            fields[key] = institutes[key]
        elif institutes[key]:
            raise ValueError('Required Private institute field missing')
    fields.update(_token=named['_token'].attrs['value'], disclaimer_accepted='1', captcha=arithmetic_answer(doc.text('number-captcha-display')))
    if set(named) != set(fields) | {'profile_photo'}: raise ValueError('Unexpected live submission fields')
    return fields, signature(root)


def multipart(fields, photo, expected_hash):
    if isinstance(photo,dict) and photo.get('kind')=='original_sqlite_reference':
        if re.sub(r'[-\s]','',fields.get('cnic',''))!=photo['applicant_key']:
            raise ValueError('Portrait owner differs from submitted BForm')
    from .photos import payload
    data = payload(photo,expected_hash)
    if len(data) > 262144 or sha(data) != expected_hash: raise ValueError('Photo size/hash changed')
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in ('JPEG', 'PNG'): raise ValueError('Invalid image format')
        mime = 'image/jpeg' if image.format == 'JPEG' else 'image/png'; image.verify()
    boundary = '----YouthGames' + secrets.token_hex(16)
    chunks = []
    for name, value in fields.items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name): raise ValueError('Invalid form field name')
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + value.encode() + b'\r\n')
    filename = 'profile.jpeg' if mime == 'image/jpeg' else 'profile.png'
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="profile_photo"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + data + b'\r\n')
    chunks.append(f'--{boundary}--\r\n'.encode())
    body = b''.join(chunks); content_type = 'multipart/form-data; boundary=' + boundary
    message = BytesParser(policy=default).parsebytes(b'Content-Type: ' + content_type.encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + body)
    actual = {}; images = []
    for part in message.iter_parts():
        key = part.get_param('name', header='content-disposition'); raw = part.get_payload(decode=True) or b''
        if key == 'profile_photo': images.append(sha(raw))
        else:
            if key in actual: raise ValueError('Duplicate multipart field')
            actual[key] = raw.decode()
    if actual != fields or images != [expected_hash]: raise ValueError('Multipart payload verification failed')
    return body, content_type


@dataclass
class Response:
    status: int
    html: str


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if (parsed.scheme, parsed.netloc) != ('https', 'youthgames.punjab.gov.pk'):
            raise ValueError('Redirect left the official portal')
        if req.method == 'POST' and code not in (301, 302, 303):
            raise ValueError('POST-preserving redirect blocked; no automatic replay')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), Redirects())
        self.posts = 0

    def request(self, request):
        try: response = self.opener.open(request, timeout=45)
        except urllib.error.HTTPError as error: response = error
        with response:
            raw = response.read(6 * 1024 * 1024 + 1)
            if len(raw) > 6 * 1024 * 1024: raise ValueError('Unexpectedly large server response')
            return Response(response.status, raw.decode('utf-8'))

    def get(self):
        result = self.request(urllib.request.Request(BASE + '/', headers={'Accept': 'text/html', 'User-Agent': 'YouthGames-HTTP-Pilot/1.0'}))
        if result.status != 200: raise ValueError('Fresh form GET failed: HTTP ' + str(result.status))
        if not list(self.cookies): raise ValueError('Portal did not establish a cookie session')
        return result

    def post(self, body, content_type):
        if self.posts: raise ValueError('This pilot allows only one POST; no replay')
        self.posts += 1
        return self.request(urllib.request.Request(BASE + '/applicant', data=body, method='POST', headers={
            'Content-Type': content_type, 'Accept': 'text/html', 'Origin': BASE, 'Referer': BASE + '/', 'User-Agent': 'YouthGames-HTTP-Pilot/1.0'}))


def receipt(folder, job, attempt, outcome, response, proof):
    paths = {'response.html': redact_tokens(response.html), 'http-request-proof.json': json.dumps(proof, indent=2)}
    files = {}
    for name, content in paths.items():
        p = folder / name; p.write_text(content); files[name] = {'path': str(p.resolve()), 'sha256': sha(p.read_bytes())}
    item = {'application': 'youthgames_register', 'transport': 'http', 'job': job['id'], 'attempt': attempt,
        'payload_hash': job['payload_hash'], 'post_dispatched': True, 'http_status': response.status,
        'outcome': outcome, 'files': files, 'captured_at': now()}
    temporary = folder / 'receipt.tmp'; temporary.write_text(json.dumps(item, indent=2)); target = folder / 'receipt.json'; temporary.replace(target)
    return target


def prepare_applicants(config, profile_path, selected, copies, hashes, response):
    _, _, root = form_catalog(response.html)
    snapshot = seal_snapshot(config, profile_path, copies, hashes)
    photo = snapshot / ('photo' + copies['photo'][0].suffix) if 'photo' in copies else None
    applicants = []
    contract_checked = False
    private_supported = private_manual_supported(response.html) if any(data.get('SubCategory') == 'Private' for data, _ in selected) else False
    catalog_hash = signature(root)
    for data, origin in selected:
        canonical, ids, issues, _ = validate_fields({**data, 'UC': uc_label(data, config)}, {'data': root})
        age = age_eligibility(canonical, {'data': root})
        row_issues=list(issues)
        try: institute_fields(data, private_supported)
        except ValueError: row_issues.append('unsupported_or_missing_institute')
        if age['state'] != 'eligible':row_issues.append('outside_category_dob_bounds')
        for field, key in [('Category','category'),('SubCategory','subcategory'),('Sport','sport')]:
            if not selection_accepts(config,key,data[field]):
                raise ValueError('HTTP source differs from the confirmed selection')
        item = {'roll': origin['roll'], 'data': data, 'origins': origin['origins'],
            'operator_confirmation': config['confirmation'],
            'photo': applicant_photo(config,origin,photo,hashes['photo']),
            'option_ids': ids, 'dob_rule': {'min': age.get('rule',{}).get('min_dob'), 'max': age.get('rule',{}).get('max_dob')},
            'catalog_hash': catalog_hash, 'validation': {'state': 'ineligible' if age['state']!='eligible' else 'blocked_input' if row_issues else 'ready', 'issues': row_issues}}
        if origin.get('administrative_allocation'):
            item['administrative_allocation'] = origin['administrative_allocation']
        if origin.get('sport_allocation'):
            item['sport_allocation']=origin['sport_allocation']
        if origin.get('provisional_dob'):item['provisional_dob']=origin['provisional_dob']
        # Parse the common form contract once, then validate live again at each POST.
        if not row_issues and not contract_checked:
            form_values(data)
            prepare_form(response.html, config, item)
            contract_checked = True
        applicants.append(item)
    return root, applicants, snapshot


def prepare(profile_path, client=None):
    """Prepare only the pinned source using a read-only form GET, never a browser."""
    config = load(profile_path)
    with input_snapshot(config, profile_path) as (rows, origins, copies, hashes):
        rolls = set(config['selection']['rolls'])
        selected = [(r, p) for r, p in zip(rows, origins) if not rolls or p['roll'] in rolls]
        if not selected:
            raise ValueError('HTTP profile has no selected input rows')
        response = (client or Client()).get()
        root, applicants, snapshot = prepare_applicants(config, profile_path, selected, copies, hashes, response)
        with opened(resolved(profile_path, config['state']['database'])) as db:
            batch = import_batch(db, config, hashes, root, snapshot, applicants)
            return {'batch': batch, 'prepared': len(applicants), 'applicant_posts': 0, 'transport': 'http'}


def run(profile_path, client=None, roll=None):
    config = load(profile_path)
    client = client or Client()
    with opened(resolved(profile_path, config['state']['database'])) as db:
        if jobs(db, config['profile'], ('uncertain',)):
            raise ValueError('Existing uncertain submission requires reconciliation before pilot')
        selected = jobs(db, config['profile'], ('ready',), rolls=(roll,) if roll is not None else ())
        if not selected: return {'status': 'nothing_ready', 'post_attempts': 0}
        job = selected[0]; item = json.loads(job['payload'])
        if signature(item) != job['payload_hash']: raise ValueError('Prepared applicant payload hash changed')
        with input_snapshot(config, profile_path) as (rows, *_):
            matches = [r for r in rows if r['BForm'] == item['data']['BForm']]
            if matches != [item['data']] or item['operator_confirmation'] != config['confirmation']:
                raise ValueError('Prepared job differs from pinned operator-confirmed input')
            from .photos import matches_profile
            if not matches_profile(config,item['photo']):
                raise ValueError('Prepared photo differs from operator profile')
            if item['validation']['state'] != 'ready': raise ValueError('Prepared validation is not ready')
            form = client.get()
            fields, catalog_hash = prepare_form(form.html, config, item)
            body, content_type = multipart(fields, item['photo'], item['photo']['sha256'])
            destination = output_folder(resolved(profile_path, config['state']['output']), config['profile'])
            attempt = begin(db, job)
            folder = attempt_folder(destination, job['id'], attempt)
            proof = {'transport': 'http', 'method': 'POST', 'endpoint': BASE + '/applicant', 'multipart_sha256': sha(body),
                'payload_hash': job['payload_hash'], 'photo_sha256': item['photo']['sha256'], 'live_catalog_sha256': catalog_hash,
                'field_names': sorted(fields), 'captcha_read_from_fresh_form': True, 'maximum_post_attempts': 1}
            if item['data']['SubCategory']=='Private':
                proof['institute_submission']={'mode':'private_manual','institution_id':fields['institution_id'],'institute_name':fields['institute_name']}
            with db: db.execute("UPDATE jobs SET state='filled',updated_at=? WHERE id=?", (now(), job['id']))
            intent(db, job['id'], attempt)
            dispatched(db, job['id'], attempt)
            try:
                response = client.post(body, content_type)
                outcome = classify(response.html, response.status)
            except BaseException as error:
                response = Response(0, '')
                outcome = {'state': 'uncertain', 'tracking': None, 'detail': 'HTTP transport interrupted after durable dispatch: ' + type(error).__name__}
                proof['transport_error'] = type(error).__name__
                path = receipt(folder, job, attempt, outcome, response, proof)
                finish(db, job['id'], attempt, 'uncertain', outcome['detail'], path)
                export(db, config, destination)
                if isinstance(error, (KeyboardInterrupt, SystemExit)): raise
                return {'job': job['id'], 'state': 'uncertain', 'post_attempts': client.posts, 'receipt': str(path)}
            path = receipt(folder, job, attempt, outcome, response, proof)
            finish(db, job['id'], attempt, outcome['state'], outcome['detail'], path, outcome.get('tracking'))
            counts = export(db, config, destination)
            return {'job': job['id'], 'state': outcome['state'], 'post_attempts': client.posts, 'http_status': response.status,
                'receipt': str(path), 'counts': counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--roll', help='Only this roll; never substitute another ready applicant')
    args = parser.parse_args()
    result = run(args.config, roll=args.roll)
    print(json.dumps(result, indent=2))
    return 0 if result.get('state') == 'registered' or result.get('status') == 'nothing_ready' else 2


if __name__ == '__main__': raise SystemExit(main())
