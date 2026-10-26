"""Versioned profiles; paths are relative to their JSON file."""

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path

from . import COLUMNS


def template():
    return json.loads(Path(__file__).with_name('profile.template.json').read_text())


def reject_unknown(value, shape, path='config'):
    if not isinstance(value, dict):
        raise ValueError(f'{path} must be an object')
    unknown = set(value) - set(shape)
    if unknown:
        raise ValueError(f'Unknown setting at {path}: {sorted(unknown)}')
    dynamic = {'mapping', 'defaults', 'participation.school_name_mappings'}
    for key, item in value.items():
        child = f'{path}.{key}'
        local = child.removeprefix('config.')
        if isinstance(shape.get(key), dict) and shape[key] and local not in dynamic:
            reject_unknown(item, shape[key], child)


def merged(base, incoming):
    result = deepcopy(base)
    for key, value in incoming.items():
        result[key] = merged(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return result


def validate(config):
    if type(config['schema_version']) is not int or config['schema_version'] != 1:
        raise ValueError('schema_version must be 1')
    if not isinstance(config['profile'], str) or not config['profile'].strip():
        raise ValueError('profile must be a nonblank string')
    if config['target']['columns'] != COLUMNS:
        raise ValueError('target.columns must match the exact 17-column sample contract')
    for section, fields in [('mapping', config['mapping']), ('defaults', config['defaults'])]:
        if set(fields) - set(COLUMNS):
            raise ValueError(f'Unknown target field in {section}')
        if any(v is not None and not isinstance(v, str) for v in fields.values()):
            raise ValueError(f'{section} values must be strings or null')
    if config['source']['format'] not in ('auto', 'csv', 'xlsx', 'sqlite'):
        raise ValueError('source.format must be auto, csv, xlsx or sqlite')
    allocation = config['source']['allocation_scheme']
    if allocation is not None and (not isinstance(allocation,str) or not allocation.strip() or config['source']['format'] != 'sqlite'):
        raise ValueError('source.allocation_scheme needs a nonblank scheme and explicit SQLite source')
    if type(config['output']['write_excel']) is not bool:
        raise ValueError('output.write_excel must be boolean')
    if type(config['source']['header_row']) is not int or config['source']['header_row'] < 1:
        raise ValueError('source.header_row must be a positive row number')
    selection = config['selection']
    for key in ('institutions', 'rolls', 'collection_states', 'result_statuses'):
        if not isinstance(selection[key], list) or any(not isinstance(v, str) for v in selection[key]):
            raise ValueError(f'selection.{key} must be a list of strings')
    if selection['institution_match'] not in ('exact', 'exact_case_insensitive'):
        raise ValueError('Only exact institution selection is supported')
    if selection['limit'] is not None and (type(selection['limit']) is not int or selection['limit'] < 1):
        raise ValueError('selection.limit must be null or a positive integer')
    if not isinstance(selection['row_ranges'], list):
        raise ValueError('selection.row_ranges must be a list')
    for bounds in selection['row_ranges']:
        if not isinstance(bounds, list) or len(bounds) != 2 or any(type(n) is not int for n in bounds) or not 1 <= bounds[0] <= bounds[1]:
            raise ValueError('Each row range must be [first_row, last_row]')
    bounds = selection['roll_bounds']
    if bounds is not None and (not isinstance(bounds, list) or len(bounds) != 2 or any(not isinstance(v, str) or not v.isascii() or not v.isdigit() for v in bounds) or int(bounds[0]) > int(bounds[1])):
        raise ValueError('roll_bounds must be two ordered digit strings')
    identity = {'FullName', 'FatherName', 'BForm', 'DOB'}
    for rule in config['rules']:
        reject_unknown(rule, {'when': {}, 'set': {}}, 'rule')
        if not isinstance(rule.get('when'), dict) or not rule['when'] or not isinstance(rule.get('set'), dict):
            raise ValueError('Rules need nonempty when and set objects')
        if set(rule['when']) - {'institutions', 'rolls'} or set(rule['set']) - set(COLUMNS) or set(rule['set']) & identity:
            raise ValueError('Rules may select institutions/rolls and set nonidentity target fields')
        if any(not isinstance(v, list) or not v or any(not isinstance(i, str) for i in v) for v in rule['when'].values()):
            raise ValueError('Rule selection values must be nonempty string lists')
        if any(v is not None and not isinstance(v, str) for v in rule['set'].values()):
            raise ValueError('Rule values must be strings or null')
    for field in config['validation']['completeness_required']:
        if field not in COLUMNS:
            raise ValueError(f'Unknown completeness field: {field}')
    for rule in config['validation']['conditional_required']:
        if rule.get('field') not in COLUMNS or rule.get('when') != {'SubCategory': {'not_equals': 'Open Category'}}:
            raise ValueError('conditional_required supports fields outside Open Category')
    size = config['size_estimation']
    date.fromisoformat(size['as_of_date'])
    previous = None
    if not isinstance(size['rules'], list) or not size['rules']:
        raise ValueError('size_estimation.rules must be a nonempty list')
    for rule in size['rules']:
        reject_unknown(rule, {'min_age': None, 'max_age': None, 'Male': {}, 'Female': {}}, 'size rule')
        if type(rule.get('min_age')) is not int or type(rule.get('max_age')) is not int or not 0 <= rule['min_age'] <= rule['max_age']:
            raise ValueError('Size rule age bounds are invalid')
        if previous is not None and rule['min_age'] != previous + 1:
            raise ValueError('Size age rules must be ordered without gaps or overlaps')
        previous = rule['max_age']
        for gender in ('Male', 'Female'):
            values = rule.get(gender, {})
            if set(values) != {'KitSize', 'JoggersSize'} or any(not isinstance(v, str) or not v for v in values.values()):
                raise ValueError('Size rules need string KitSize/JoggersSize for Male and Female')
    # These flags document the implemented contract; unsupported modes fail rather
    # than silently pretending to honor them.
    fixed = {
        ('participation', 'category_mode'): 'profile',
        ('participation', 'subcategory_mode'): 'profile_with_missing_institute_fallback',
        ('participation', 'missing_institute_subcategory'): 'Open Category',
        ('participation', 'institute_required_for_open_category'): False,
        ('participation', 'unclassified_board_label'): 'preserve_in_audit',
        ('normalization', 'institute_mode'): 'explicit_only',
        ('normalization', 'name_case'): 'preserve',
        ('normalization', 'target_dob_format'): '%Y-%m-%d',
        ('normalization', 'bform_format'): '13_digits',
        ('normalization', 'missing_values'): 'blank',
        ('gender', 'allow_missing_board_field'): True,
        ('size_estimation', 'mode'): 'age_gender_lookup',
        ('size_estimation', 'only_when_missing'): True,
        ('size_estimation', 'age_unit'): 'completed_years',
        ('size_estimation', 'leap_day_birthday_in_non_leap_year'): 'march_1',
        ('size_estimation', 'invalid_or_unhandled_input'): 'leave_blank',
        ('enrichment', 'ambiguous_matches'): 'report_conflict',
        ('enrichment', 'identity_changes'): 'explicit_individual_correction_only',
        ('validation', 'duplicate_bform'): 'exclude_conflicting_group_from_complete_export',
        ('output', 'include_partial_records'): True,
        ('output', 'encoding'): 'utf-8-sig',
    }
    for (section, key), expected in fixed.items():
        if config[section][key] != expected:
            raise ValueError(f'Unsupported {section}.{key}; expected {expected!r}')
    parity = config['gender']['student_cnic_parity']
    if parity['mode'] not in ('draft_export_fallback', 'suggestion_only', 'disabled'):
        raise ValueError('Unsupported gender.student_cnic_parity.mode')
    if (parity['odd'], parity['even'], parity['require_complete_13_digits'], parity['preserve_explicit_values']) != ('Male', 'Female', True, True):
        raise ValueError('Parity supports only the requested valid-student-ID mapping')
    if parity['source_column'] != config['mapping'].get('BForm'):
        raise ValueError('Gender fallback must use the mapped student BForm column')
    for key in ('write_complete_subset', 'write_provenance'):
        if type(config['output'][key]) is not bool:
            raise ValueError(f'output.{key} must be true/false')
    if not isinstance(config['participation']['school_name_mappings'], dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in config['participation']['school_name_mappings'].items()):
        raise ValueError('school_name_mappings must map board labels to school name strings')
    for field, fallback in config['contact_fallbacks'].items():
        if fallback['only_when_missing'] is not True:
            raise ValueError(f'{field} fallback must preserve supplied values')
    for source in config['enrichment']['sources']:
        reject_unknown(source, {'path': None, 'key': None}, 'enrichment source')
        if source.get('key') not in ('BForm', 'Roll No') or not isinstance(source.get('path'), str):
            raise ValueError('Enrichment sources need path and key (BForm or Roll No)')
    portal = config['portal']
    if type(portal['enabled']) is not bool or type(portal['max_catalog_age_hours']) not in (int, float) or portal['max_catalog_age_hours'] <= 0:
        raise ValueError('portal.enabled must be boolean and max_catalog_age_hours positive')
    if any(not isinstance(portal[k], str) or not portal[k].strip() for k in ('campaign', 'normalization_database', 'registration_database')):
        raise ValueError('Portal campaign/database paths must be nonblank strings')
    if portal['enabled']:
        if not isinstance(config['validation']['catalog'], str) or config['validation']['eligibility'] != {'mode': 'catalog'}:
            raise ValueError('Portal validation requires a catalog path and eligibility mode catalog')
        if config['output']['write_provenance'] is not True:
            raise ValueError('Portal handoff requires write_provenance=true')
    elif config['validation']['catalog'] is not None or config['validation']['eligibility'] is not None:
        raise ValueError('Enable portal to use catalog/eligibility validation')
    if not isinstance(config['source']['path'], str) or not isinstance(config['output']['folder'], str):
        raise ValueError('Source path and output folder must be strings')


def load(path):
    path = Path(path).resolve()
    incoming = json.loads(path.read_text(encoding='utf-8'))
    base = template()
    reject_unknown(incoming, base)
    config = merged(base, incoming)
    validate(config)
    return config


def resolve(config_path, value):
    return (Path(config_path).resolve().parent / value).resolve()


def initialize(config_path, profile, source, sample=None, output=None):
    path = Path(config_path).resolve()
    if path.exists():
        load(path)
        return False
    config = template()
    config['profile'] = profile
    config['source']['path'] = os.path.relpath(Path(source).resolve(), path.parent)
    config['target']['template'] = os.path.relpath(Path(sample).resolve(), path.parent) if sample else None
    target = Path(output).resolve() if output else path.parent.parent / 'normalized_output' / profile
    config['output']['folder'] = os.path.relpath(target, path.parent)
    validate(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation makes repeated/concurrent init preserve operator edits.
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(config, indent=2, ensure_ascii=False) + '\n')
    except FileExistsError:
        return False
    return True
