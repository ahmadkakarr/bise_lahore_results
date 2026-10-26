"""Deterministic field transforms with disk-backed joins and deduplication."""

from collections import Counter, defaultdict
from datetime import date, datetime
import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import COLUMNS
from .sources import selected, table

IDENTITY = {'FullName', 'FatherName', 'BForm', 'DOB'}


def identifier(value):
    value = re.sub(r'[-\s]', '', value or '')
    return value if re.fullmatch(r'[0-9]{13}', value) else ''


def born_on(value, source_format):
    if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        return date.fromisoformat(value)
    return datetime.strptime(value, source_format).date()


def normalized_gender(value):
    return {'m': 'Male', 'male': 'Male', 'f': 'Female', 'female': 'Female'}.get(value.casefold(), value)


def clean_phone(value):
    value = re.sub(r'[- ()\s]', '', value)
    if value.startswith('+92'):
        value = '0' + value[3:]
    elif value.startswith('92') and len(value) == 12:
        value = '0' + value[2:]
    return value if re.fullmatch(r'0[0-9]{10}', value) else ''


class Prepared:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE records (roll TEXT PRIMARY KEY, bform TEXT, complete INTEGER, payload TEXT);
            CREATE INDEX records_bform ON records(bform);
            CREATE TABLE links (source TEXT, key TEXT, payload TEXT);
            CREATE INDEX links_lookup ON links(source,key);
            CREATE TABLE excluded (source_row INTEGER, roll TEXT, reason TEXT);
            CREATE TABLE duplicates (kind TEXT, key TEXT, roll TEXT, source_row INTEGER);
        ''')
        self.summary = Counter()
        self.origins = Counter()

    def records(self, only_complete=False, only_missing=False):
        where = ' WHERE complete=1' if only_complete else ' WHERE complete=0' if only_missing else ''
        for (payload,) in self.db.execute('SELECT payload FROM records' + where + ' ORDER BY roll'):
            yield json.loads(payload)

    def close(self):
        self.db.close()


def read_links(prepared, snapshots, config):
    result = []
    for index, entry in enumerate(config['enrichment']['sources']):
        result.append((f'enrichment:{index}', entry['key'], False))
    if config['enrichment']['overrides']:
        result.append(('overrides', None, True))
    for label, key, is_override in result:
        path = snapshots[label][0]
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)) or not fields:
                raise ValueError(f'{label} must have unique headers')
            if key is None:
                key = 'Roll No' if 'Roll No' in fields else 'BForm'
            if key not in fields:
                raise ValueError(f'{label} lacks matching column {key}')
            if set(fields) - set(COLUMNS) - {'Roll No', 'IdentityCorrection'}:
                raise ValueError(f'{label} has unsupported columns; use target sample column names')
            for row in reader:
                if None in row:
                    raise ValueError(f'{label} has a malformed CSV row')
                row = {k: (v or '').strip() for k, v in row.items()}
                value = identifier(row[key]) if key == 'BForm' else row[key]
                if not value:
                    raise ValueError(f'{label} has a blank/invalid matching key')
                prepared.db.execute('INSERT INTO links VALUES (?,?,?)', (label, value, json.dumps(row)))
        yield label, key, is_override


def transform(raw, number, config, prepared, link_sources):
    data = {field: '' for field in COLUMNS}
    origins, warnings, issues, suggestions = {}, [], [], {}
    for target, column in config['mapping'].items():
        data[target] = raw.get(column, '')
        if data[target]:
            origins[target] = 'board:' + column
    # Optional source target fields take priority over profile defaults.
    for field in COLUMNS:
        if field not in IDENTITY and raw.get(field):
            data[field], origins[field] = raw[field], 'source:' + field
    gender_field = config['gender']['explicit_board_field']
    if raw.get(gender_field):
        data['Gender'], origins['Gender'] = raw[gender_field], 'board_explicit'
    board_label = raw.get('Institution / District', '')
    for field, value in config['defaults'].items():
        # The contact fallback has a more precise, separately recorded origin.
        if field == 'Phone' and value == config['contact_fallbacks']['Phone']['value']:
            continue
        if not data[field] and value:
            data[field], origins[field] = value, 'profile_default'
    school_map = config['participation']['school_name_mappings']
    schools = [v for k, v in school_map.items() if k.casefold() == board_label.casefold()]
    if len(set(schools)) > 1:
        issues.append('conflicting_school_name_mapping')
    elif schools and not data['Institute']:
        data['Institute'], origins['Institute'] = schools[0], 'profile_school_mapping'
    district_only = {s.casefold() for s in config['participation']['district_only_values']}
    if board_label and board_label.casefold() not in district_only and not schools and not data['Institute']:
        warnings.append('unclassified_board_institution_label')
    assignments = defaultdict(set)
    for rule in config['rules']:
        when = rule['when']
        matches = ('institutions' not in when or board_label.casefold() in {v.casefold() for v in when['institutions']})
        matches &= 'rolls' not in when or raw['Roll No'] in when['rolls']
        if matches:
            for field, value in rule['set'].items():
                assignments[field].add(value or '')
    for field, values in assignments.items():
        if len(values) > 1:
            issues.append('conflicting_profile_rules:' + field)
        else:
            data[field], origins[field] = next(iter(values)), 'profile_rule'
    lookup_bform = identifier(data['BForm'])
    for label, key, is_override in link_sources:
        lookup = lookup_bform if key == 'BForm' else raw['Roll No']
        matches = prepared.db.execute('SELECT payload FROM links WHERE source=? AND key=?', (label, lookup)).fetchall()
        if len(matches) > 1:
            issues.append('enrichment_conflict:' + label)
            continue
        if not matches:
            continue
        extra = json.loads(matches[0][0])
        correction = is_override and extra.get('IdentityCorrection', '').casefold() in ('true', 'yes', '1')
        for field in COLUMNS:
            # A matching key is not itself an identity correction.
            if field == key or not extra.get(field):
                continue
            value = extra[field]
            equal = (identifier(value) == identifier(data[field])) if field == 'BForm' else (value == data[field])
            if field in IDENTITY and data[field] and not equal and not correction:
                issues.append('identity_conflict:' + field)
                continue
            if is_override or field not in IDENTITY or not data[field]:
                data[field], origins[field] = value, 'operator_correction' if correction and field in IDENTITY else label
    if not data['Institute']:
        data['SubCategory'] = config['participation']['missing_institute_subcategory']
        origins['SubCategory'] = 'missing_institute_open_category'
    for field in ('FullName', 'FatherName'):
        from youthgames_shared.names import canonical_name
        original=data[field]
        try:
            data[field]=canonical_name(original)
            if data[field]!=original:
                origins[field]='portal_name_separator_formatting'
                warnings.append('name_formatting:'+field)
        except ValueError:
            data[field]=re.sub(r'\s+',' ',original).strip()
            issues.append('invalid_portal_name:'+field)
    raw_bform = data['BForm']
    data['BForm'] = identifier(raw_bform)
    if raw_bform and not data['BForm']:
        issues.append('invalid_bform')
    as_of = date.fromisoformat(config['size_estimation']['as_of_date'])
    age = None
    if data['DOB']:
        try:
            birth = born_on(data['DOB'], config['normalization']['source_dob_format'])
            if birth > as_of:
                raise ValueError('DOB is after profile reference date')
            age = as_of.year - birth.year - ((as_of.month, as_of.day) < (birth.month, birth.day))
            data['DOB'] = birth.isoformat()
        except ValueError:
            data['DOB'] = ''
            issues.append('invalid_dob')
    data['Gender'] = normalized_gender(data['Gender'])
    parity = config['gender']['student_cnic_parity']
    # The config enforces the mapped student column; validated individual
    # corrections/enrichment must be used consistently with the exported ID.
    parity_id = data['BForm']
    if parity_id and parity['mode'] != 'disabled':
        value = parity['odd'] if int(parity_id[-1]) % 2 else parity['even']
        if data['Gender'] and data['Gender'] != value:
            warnings.append('explicit_gender_conflicts_with_requested_parity')
        elif not data['Gender']:
            if parity['mode'] == 'draft_export_fallback':
                data['Gender'], origins['Gender'] = value, parity['provenance']
            else:
                suggestions['Gender'] = value
    for field, fallback in config['contact_fallbacks'].items():
        if not data[field]:
            value = fallback.get('value', raw.get(fallback.get('source_column'), ''))
            if value:
                data[field], origins[field] = value, fallback['provenance']
    if data['Phone']:
        phone = clean_phone(data['Phone'])
        if not phone:
            issues.append('invalid_phone')
        data['Phone'] = phone
    size = config['size_estimation']
    if age is not None:
        for rule in size['rules']:
            if rule['min_age'] <= age <= rule['max_age']:
                values = rule.get(data['Gender'], {})
                for field in ('KitSize', 'JoggersSize'):
                    if not data[field] and values.get(field):
                        data[field], origins[field] = values[field], size['provenance']
                break
    required = list(config['validation']['completeness_required'])
    if data['SubCategory'] != 'Open Category':
        required += [r['field'] for r in config['validation']['conditional_required']]
    issues += ['missing:' + field for field in required if not data[field]]
    allocation = None
    if raw.get('Administrative Allocation'):
        allocation = json.loads(raw['Administrative Allocation'])
        for field in ('Division','District','Tehsil','UC'):
            if data[field] != raw[field]:
                issues.append('administrative_allocation_conflict:' + field)
            origins[field] = 'operator_confirmed_administrative_allocation:' + allocation['scheme']
    result = {'roll': raw['Roll No'], 'source_rows': [number], 'raw': raw, 'data': data,
            'origins': origins, 'age': age, 'issues': sorted(set(issues)),
            'warnings': warnings, 'suggestions': suggestions}
    if allocation:
        result['administrative_allocation'] = allocation
    if raw.get('Sport Allocation'):
        sport=json.loads(raw['Sport Allocation'])
        if sport['applicant_key']!=data['BForm'] or sport['sport']!=data['Sport'] or sport['gender']!=data['Gender']:
            result['issues']=sorted(set(result['issues']+['sport_allocation_conflict']))
        origins['Sport']='operator_confirmed_sports_ratio:'+sport['batch']
        result['sport_allocation']=sport
    if raw.get('Photo Reference'):result['photo_reference']=json.loads(raw['Photo Reference'])
    if raw.get('Provisional DOB Metadata'):
        metadata=json.loads(raw['Provisional DOB Metadata'])
        if metadata['value']!=data['DOB'] or metadata.get('operator_confirmed_admin_exception') is not True:
            raise ValueError('Provisional DOB policy differs from normalized value/confirmation')
        result['provisional_dob']=metadata
        if metadata.get('is_estimate'):
            origins['DOB']='operator_provisional_randomized_birth_year'
            warnings.append('estimated_dob_requires_admin_correction')
    return result


def save(prepared, record):
    prepared.db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)',
                        (record['roll'], record['data']['BForm'], int(not record['issues']), json.dumps(record, ensure_ascii=False)))


def prepare(path, config, snapshot_files, database_path, cache=None):
    prepared = Prepared(database_path)
    try:
        link_sources = list(read_links(prepared, snapshot_files, config))
        reader = iter(table(path, config['source']))
        _, headers = next(reader)
        needed = {'Roll No', 'Collection Status', 'Institution / District', *config['mapping'].values()}
        if set(needed) - set(headers):
            raise ValueError(f'Student table missing columns: {sorted(set(needed) - set(headers))}')
        selected_count = 0
        for number, raw in reader:
            prepared.summary['source_rows'] += 1
            reason = selected(raw, number, config['selection'])
            if not reason and config['selection']['limit'] and selected_count >= config['selection']['limit']:
                reason = 'selection_limit'
            if not reason and (not raw['Roll No'] or not raw.get(config['mapping']['FullName'])):
                reason = 'missing_source_identity'
            if reason:
                prepared.db.execute('INSERT INTO excluded VALUES (?,?,?)', (number, raw.get('Roll No', ''), reason))
                prepared.summary['excluded:' + reason] += 1
                continue
            selected_count += 1
            compute = lambda: transform(raw, number, config, prepared, link_sources)
            record = cache.transform(raw, number, compute) if cache else compute()
            previous = prepared.db.execute('SELECT payload FROM records WHERE roll=?', (record['roll'],)).fetchone()
            if previous:
                old = json.loads(previous[0])
                old['source_rows'].append(number)
                prepared.db.execute('INSERT INTO duplicates VALUES (?,?,?,?)', ('source_identity', record['roll'], record['roll'], number))
                if old['raw'] != raw:
                    old['issues'] = sorted(set(old['issues'] + ['conflicting_source_identity']))
                    old.setdefault('source_variants', []).append({'source_row': number, 'raw': raw})
                save(prepared, old)
            else:
                save(prepared, record)
        if not selected_count:
            raise ValueError('Selection has no saved student identities; previous output was preserved')
        keys = prepared.db.execute("SELECT bform FROM records WHERE bform!='' GROUP BY bform HAVING count(*)>1").fetchall()
        for (key,) in keys:
            for (payload,) in prepared.db.execute('SELECT payload FROM records WHERE bform=?', (key,)).fetchall():
                record = json.loads(payload)
                record['issues'] = sorted(set(record['issues'] + ['duplicate_target_bform']))
                for number in record['source_rows']:
                    prepared.db.execute('INSERT INTO duplicates VALUES (?,?,?,?)', ('target_bform', key, record['roll'], number))
                save(prepared, record)
        prepared.summary['selected_source_rows'] = selected_count
        prepared.summary['normalized'] = prepared.db.execute('SELECT count(*) FROM records').fetchone()[0]
        prepared.summary['complete'] = prepared.db.execute('SELECT count(*) FROM records WHERE complete=1').fetchone()[0]
        prepared.summary['missing_details'] = prepared.summary['normalized'] - prepared.summary['complete']
        prepared.summary['duplicate_entries'] = prepared.db.execute('SELECT count(*) FROM duplicates').fetchone()[0]
        for record in prepared.records():
            prepared.origins.update(record['origins'].values())
            prepared.summary['rows_with_warnings'] += bool(record['warnings'])
        prepared.db.commit()
        return prepared
    except BaseException:
        prepared.close()
        raise
