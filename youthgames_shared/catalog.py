"""Portable official catalog; no applicant data or POST is required to refresh."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

PORTAL = 'https://youthgames.punjab.gov.pk'


class CatalogExpired(ValueError):
    """Valid local catalog whose freshness window has elapsed."""


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def load(path, max_age_hours=48):
    catalog = json.loads(Path(path).read_text())
    if catalog.get('schema_version') != 1 or catalog.get('source_url') != PORTAL + '/#registration':
        raise ValueError('Unknown portal catalog source/schema')
    if signature(catalog['data']) != catalog.get('content_sha256'):
        raise ValueError('Portal catalog content hash mismatch')
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(catalog['fetched_at'])).total_seconds() / 3600
    if age < -1:
        raise ValueError('Portal catalog timestamp is in the future')
    if age > max_age_hours:
        raise CatalogExpired('Portal catalog expired; run bise_normalize catalog --refresh')
    return catalog


def ensure_fresh(path, max_age_hours=48):
    """Refresh only an expired, otherwise valid catalog; never send applicants.

    Offline, corrupt or changed-contract failures retain the last file intact.
    Strict load() remains available for immutable snapshots and offline audits.
    """
    try:
        return load(path, max_age_hours)
    except CatalogExpired:
        previous = load(path, float('inf'))
    import httpx
    from youthgames_register.http_pilot import form_catalog
    with httpx.Client(timeout=30, verify=True, trust_env=False, follow_redirects=False) as client:
        response = client.get(PORTAL + '/')
        response.raise_for_status()
    _, _, data = form_catalog(response.text)
    if not data.get('participation', {}).get('dobEligibilityRulesByCategoryName'):
        raise ValueError('Official participation catalog changed')
    data.update({key: previous['data'][key] for key in ('aliases', 'formats') if key in previous['data']})
    value = {'schema_version': 1, 'source_url': PORTAL + '/#registration',
             'fetched_at': datetime.now(timezone.utc).isoformat(), 'content_sha256': signature(data), 'data': data}
    target = Path(path).resolve()
    handle, name = tempfile.mkstemp(prefix='.portal-catalog-', suffix='.json', dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, 'w') as stream:
            stream.write(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        load(temporary, max_age_hours)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def exact(options, label):
    matches = [v for v in options if v['name'].strip().casefold() == label.strip().casefold()]
    if len(matches) != 1:
        raise ValueError('Missing/ambiguous portal label')
    return matches[0]


def options(values):
    return [{'id': str(v['value']), 'name': v['text'].strip()} for v in values if v['value'] and not v.get('disabled')]


async def refresh(path):
    from playwright.async_api import async_playwright
    from youthgames_register.browser import PostGate
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        try:
            context = await browser.new_context()
            gate = PostGate()
            await context.route('**/*', gate.route)
            page = await context.new_page()
            response = await page.goto(PORTAL + '/#registration', wait_until='load', timeout=60000)
            if response is None or response.status != 200:
                raise ValueError('Official portal catalog did not load')
            form = page.locator('#registration-form')
            await form.wait_for(state='attached')
            participation = await page.evaluate('() => window.participationCatalog')
            if not participation or not participation.get('dobEligibilityRulesByCategoryName'):
                raise ValueError('Official participation catalog changed')
            html = await page.content()
            maps = {}
            for name in ('districtsByDivision', 'tehsilsByDistrict', 'ucsByDistrict'):
                match = re.search(r'const\s+' + name + r'\s*=\s*', html)
                if not match:
                    raise ValueError('Official location catalog changed: ' + name)
                maps[name], _ = json.JSONDecoder().raw_decode(html[match.end():])
            captured = {}
            for field, ident in [('categories', 'participation-category'), ('divisions', 'division'), ('genders', 'gender'), ('kit_sizes', 'kit-size-id'), ('jogger_sizes', 'jogger-size-id')]:
                captured[field] = options(await form.locator('#' + ident).evaluate('e=>[...e.options].map(o=>({value:o.value,text:o.text,disabled:o.disabled}))'))
            data = {**captured, 'participation': participation,
                    'districts_by_division': maps['districtsByDivision'], 'tehsils_by_district': maps['tehsilsByDistrict'], 'ucs_by_district': maps['ucsByDistrict'],
                    'aliases': {'UC': {'UC No. 178, Manawan': {'Division': 'Lahore', 'District': 'Lahore', 'target': 'UC No. 178, Manawan Ram Pur Jageer'}}},
                    'formats': {'canonical_bform': '13 digits', 'form_cnic': '5-7-1 hyphenated', 'canonical_phone': '11 digits', 'form_mobile_phone': '4-7 hyphenated', 'DOB': 'YYYY-MM-DD', 'photo_max_bytes': 262144}}
            result = {'schema_version': 1, 'source_url': PORTAL + '/#registration', 'fetched_at': datetime.now(timezone.utc).isoformat(), 'content_sha256': signature(data), 'data': data}
            target = Path(path).resolve(); target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix('.tmp'); tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n'); tmp.replace(target)
            return {'catalog': str(target), 'content_sha256': result['content_sha256'], 'categories': len(data['categories']), 'districts': sum(len(v) for v in data['districts_by_division'].values()), 'applicant_posts': 0}
        finally:
            await browser.close()


def age_eligibility(data, catalog):
    from datetime import date
    rules = catalog['data']['participation']['dobEligibilityRulesByCategoryName']
    match = [v for k, v in rules.items() if k.casefold() == data['Category'].casefold()]
    if len(match) != 1:
        return {'state': 'unknown', 'reason': 'category_age_rule_unavailable'}
    rule = match[0]
    try:
        dob = date.fromisoformat(data['DOB'])
    except (ValueError, TypeError):
        return {'state': 'unknown', 'reason': 'invalid_or_missing_dob', 'rule': rule}
    eligible = date.fromisoformat(rule['min_dob']) <= dob <= date.fromisoformat(rule['max_dob'])
    return {'state': 'eligible' if eligible else 'ineligible', 'reason': '' if eligible else 'outside_category_dob_bounds', 'rule': rule}


def validate_fields(data, catalog):
    """Return exact accepted values/IDs; never fuzzy-match neighboring UCs."""
    values, ids, issues, changes = dict(data), {}, [], {}
    root = catalog['data']; participation = root['participation']
    from .names import canonical_name
    for field in ('FullName','FatherName'):
        try:
            value=canonical_name(values.get(field,''))
            if value!=values[field]:changes[field]={'from':values[field],'to':value};values[field]=value
        except ValueError:issues.append('portal_name_format:'+field)
    def choose(field, choices, label=None):
        try:
            picked = exact(choices, values[field] if label is None else label)
            ids[field] = str(picked['id'])
            if values[field] != picked['name']:
                changes[field] = {'from': values[field], 'to': picked['name']}
                values[field] = picked['name']
            return picked
        except (ValueError, KeyError, TypeError):
            issues.append('portal_option:' + field)
            return None
    choose('Category', root['categories'])
    allowed_sub = participation['subCategoriesByCategoryName'].get(values['Category'], [])
    choose('SubCategory', [v for v in participation['subCategories'] if v['name'] in allowed_sub])
    choose('Gender', root['genders'])
    division = choose('Division', root['divisions'])
    district = choose('District', root['districts_by_division'].get(str(division['id']), []) if division else [])
    choose('Tehsil', root['tehsils_by_district'].get(str(district['id']), []) if district else [])
    uc_required = participation.get('showUcByCategoryName', {}).get(values['Category'], True)
    if uc_required or values['UC']:
        alias = root.get('aliases', {}).get('UC', {}).get(values['UC'])
        label = values['UC']
        if alias:
            if alias['Division'].casefold() != values['Division'].casefold() or alias['District'].casefold() != values['District'].casefold():
                issues.append('portal_alias_parent_scope:UC')
            else:
                label = alias['target']
        choose('UC', root['ucs_by_district'].get(str(district['id']), []) if district else [], label)
    allowed_sports = participation['sportsByCategoryName'].get(values['Category'], [])
    if values['Category'] == 'Special Persons':
        allowed_sports = participation.get('sportsBySpecialPersonSubCategoryName', {}).get(values['SubCategory'], allowed_sports)
    sports = [v for v in participation['sports'] if v['name'] in allowed_sports]
    if values['Gender'] == 'Female':
        sports = [v for v in sports if 'boys only' not in v['name'].casefold()]
    choose('Sport', sports)
    choose('KitSize', root['kit_sizes']); choose('JoggersSize', root['jogger_sizes'])
    return values, ids, sorted(set(issues)), changes
