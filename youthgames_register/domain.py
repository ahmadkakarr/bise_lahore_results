"""Field resolution, frontend eligibility and conservative response classification."""

from datetime import date
import json
import re

from bise_results.parser import Document
from .config import selection_accepts

SELECTS = {'Category': 'participation-category', 'SubCategory': 'participation-subcategory',
           'Gender': 'gender', 'Division': 'division', 'District': 'district', 'Tehsil': 'tehsil',
           'UC': 'union-council', 'Sport': 'sport-id', 'KitSize': 'kit-size-id', 'JoggersSize': 'jogger-size-id'}
TEXT = {'FullName': 'first-name', 'FatherName': 'last-name', 'BForm': 'cnic', 'DOB': 'dob',
        'Phone': 'mobile-phone', 'Address': 'address'}


def option(options, label):
    matches = [o for o in options if o['text'].strip().casefold() == label.strip().casefold() and o['value'] and not o.get('disabled')]
    if len(matches) != 1:
        raise ValueError('Missing or ambiguous option: ' + label)
    return matches[0]['value']


def uc_label(data, config):
    alias = config['location_aliases'].get(data['UC'])
    if alias:
        if alias['division'].casefold() != data['Division'].casefold() or alias['district'].casefold() != data['District'].casefold():
            raise ValueError('UC alias is scoped to a different division/district')
        return alias['target_label']
    return data['UC']


def form_values(data):
    bform, phone = data['BForm'], data['Phone']
    if not re.fullmatch('[0-9]{13}', bform) or not re.fullmatch('03[0-9]{9}', phone):
        raise ValueError('Invalid canonical student ID/mobile phone')
    result = {field: data[field] for field in TEXT}
    from youthgames_shared.names import canonical_name
    for field in ('FullName','FatherName'):result[field]=canonical_name(data[field])
    result['BForm'] = bform[:5] + '-' + bform[5:12] + '-' + bform[12]
    result['Phone'] = phone[:4] + '-' + phone[4:]
    return result


def validate(data, config, catalog):
    issues = []
    required = set(TEXT) | set(SELECTS)
    for field in sorted(required):
        if not data.get(field):
            issues.append('missing:' + field)
    try:
        form_values(data)
    except ValueError:
        issues.append('invalid_student_id_phone_or_name')
    ineligible = False
    try:
        birth = date.fromisoformat(data['DOB'])
        lo, hi = date.fromisoformat(catalog['dob']['min']), date.fromisoformat(catalog['dob']['max'])
        ineligible = not lo <= birth <= hi
        if ineligible:
            issues.append('outside_category_dob_bounds')
    except (ValueError, TypeError):
        issues.append('invalid_dob_or_live_rule')
    for field in ('Category', 'SubCategory', 'Sport'):
        if not selection_accepts(config,{'Category': 'category', 'SubCategory': 'subcategory', 'Sport': 'sport'}[field],data[field]):
            issues.append('profile_selection_mismatch:' + field)
    from .institutes import institute_fields
    try: institute_fields(data, catalog.get('private_manual_supported', False))
    except ValueError: issues.append('unsupported_non_open_institute_flow')
    for field in SELECTS:
        try:
            label = uc_label(data, config) if field == 'UC' else data[field]
            choices = catalog['sports_by_gender'].get(data['Gender'], []) if field == 'Sport' else catalog['options'][field]
            option(choices, label)
        except (ValueError, KeyError):
            issues.append('unresolved_option:' + field)
    for field in ('Division', 'District', 'Tehsil'):
        if data[field].casefold() != catalog['location'][field].casefold():
            issues.append('catalog_parent_scope_mismatch:' + field)
    state = 'ineligible' if ineligible else 'blocked_input' if issues else 'ready'
    return {'state': state, 'issues': sorted(set(issues))}


def classify(html, status):
    doc = Document(html)
    tracking = doc.text('registration-success-tracking-id')
    form = doc.ids.get('registration-form')
    success = (form is not None and form.attrs.get('data-registration-success') == '1') or any(k in doc.ids for k in ('registrationSuccessModal', 'registration-success', 'registration-success-banner'))
    envelopes = []
    for script in doc.root.elements('script'):
        if 'flasher-js' not in script.attrs.get('class', '').split():
            continue
        text = script.text()
        for match in re.finditer(r'options\.push\(\s*(?=\{)', text):
            try:
                payload, _ = json.JSONDecoder().raw_decode(text[match.end():])
                envelopes.extend(payload.get('envelopes', []))
            except (ValueError, AttributeError):
                continue
    flash_tracking = set()
    for envelope in envelopes:
        if isinstance(envelope, dict) and envelope.get('type') == 'success':
            match = re.fullmatch(r'Registration submitted successfully\. Your tracking ID is ([A-Za-z0-9][A-Za-z0-9_/-]{2,100})\.', envelope.get('message', ''))
            if match:
                flash_tracking.add(match.group(1))
    if tracking:
        flash_tracking.add(tracking)
    if len(flash_tracking) > 1:
        return {'state': 'uncertain', 'tracking': None, 'detail': 'Conflicting server tracking IDs; reconcile manually', 'retryable_captcha': False}
    if not tracking and len(flash_tracking) == 1:
        tracking = next(iter(flash_tracking))
    if form is not None and form.attrs.get('data-has-server-errors') == '1' and (tracking or success):
        return {'state':'uncertain','tracking':None,'detail':'Conflicting validation and success evidence; reconcile before retry','retryable_captcha':False,'rate_limited':False}
    if 200 <= status < 400 and success and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_/-]{2,100}', tracking):
        return {'state': 'registered', 'tracking': tracking, 'detail': 'Server registration receipt captured', 'retryable_captcha': False}
    if form is not None and form.attrs.get('data-has-server-errors') == '1' and 200 <= status < 500:
        messages = []
        banner = doc.ids.get('registration-errors')
        if banner is not None:
            # This is the server's validation summary. Navigation/declaration
            # lists and hidden generic client feedback are not server errors.
            entries = [node.text() for node in banner.elements('li') if node.text()]
            messages.extend(entries or ([banner.text()] if banner.text() else []))
        for node in doc.root.elements('div'):
            classes = node.attrs.get('class', '').split()
            if ('alert-danger' in classes or ('invalid-feedback' in classes and 'd-block' in classes)) and node.text():
                messages.append(node.text())
        messages += [e['message'] for e in envelopes if isinstance(e, dict) and e.get('type') == 'error' and isinstance(e.get('message'), str)]
        messages = list(dict.fromkeys(messages))
        detail = '; '.join(dict.fromkeys(messages))[:4000] or 'Portal returned explicit form validation errors'
        duplicate = bool(re.search(r'cnic.{0,50}already|b.form.{0,50}already|(?:applicant|you|student).{0,30}already\s+(?:been\s+)?registered', detail, re.I))
        retry = bool(not duplicate and messages and all(re.search(r'(?:captcha|challenge)', m, re.I) and re.search(r'incorrect|invalid|mismatch|wrong', m, re.I) for m in messages))
        waits = [re.fullmatch(r'Too many registration attempts\. Please wait ([0-9]{1,5}) seconds? and try again\.', m, re.I) for m in messages]
        rate_limited = bool(messages and all(waits) and not tracking and not success)
        return {'state': 'already_registered_external' if duplicate else 'rejected', 'tracking': None, 'detail': detail, 'retryable_captcha': retry,
                'rate_limited': rate_limited, 'retry_after_seconds': max(int(m.group(1)) for m in waits) if rate_limited else None}
    return {'state': 'uncertain', 'tracking': None, 'detail': f'No unambiguous receipt/validation outcome (HTTP {status}); reconcile before retry', 'retryable_captcha': False}


def redact_tokens(html):
    # Preserve response identity/receipt locally without retaining transient CSRF.
    html = re.sub(r'(<input\b[^>]*\bname=["\'](?:_token|csrf_token)["\'][^>]*\bvalue=["\'])[^"\']*', r'\1[REDACTED]', html, flags=re.I)
    html = re.sub(r'(<meta\b[^>]*\bname=["\']csrf-token["\'][^>]*\bcontent=["\'])[^"\']*', r'\1[REDACTED]', html, flags=re.I)
    return html
