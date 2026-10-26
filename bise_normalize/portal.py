"""Portal compatibility and eligibility, independent of the source marks layout."""

from youthgames_shared.catalog import age_eligibility, validate_fields
from .engine import save

BLOCKED = {'registered', 'already_registered_external', 'uncertain', 'submitting', 'filling', 'filled', 'rejected', 'blocked_input'}
READY_STATES = {'not_uploaded', 'ready', 'ineligible', 'imported'}
SUBSETS = {
    'Age Eligible': lambda r: r['portal']['age']['state'] == 'eligible',
    'Eligible': lambda r: r['portal']['eligible'],
    'Ready to Register': lambda r: r['portal']['ready'],
    'Ineligible': lambda r: r['portal']['age']['state'] == 'ineligible',
    'Age Unknown': lambda r: r['portal']['age']['state'] == 'unknown',
    'Already Registered': lambda r: r['portal']['registration_state'] in {'registered', 'already_registered_external'},
    'Registration Blocked': lambda r: r['portal']['registration_state'] not in READY_STATES | {'registered', 'already_registered_external'},
}


def evaluate(prepared, catalog, states):
    keys = ('age_eligible', 'age_ineligible', 'age_unknown', 'eligible', 'ready_to_register', 'already_registered', 'registration_blocked', 'portal_invalid')
    for key in keys:
        prepared.summary[key] = 0
    prepared.origins.clear()
    # Materialize one page at a time; replacing rows under an active SELECT can revisit them.
    last = ''
    while True:
        page = prepared.db.execute('SELECT roll,payload FROM records WHERE roll>? ORDER BY roll LIMIT 500', (last,)).fetchall()
        if not page:
            break
        import json
        for roll, payload in page:
            row = json.loads(payload)
            values, ids, issues, changes = validate_fields(row['data'], catalog)
            row['data'] = values
            for field in changes:
                row['origins'][field] = 'portal_name_separator_formatting' if field in ('FullName','FatherName') else 'portal_catalog_canonical_label'
            age = age_eligibility(values, catalog)
            if row.get('provisional_dob',{}).get('is_estimate'):
                age.update(basis='operator_provisional_dob',individually_verified=False,admin_correction_required=True)
            state = states.get(values['BForm'], 'not_uploaded')
            eligible = age['state'] == 'eligible' and not row['issues'] and not issues
            row['portal'] = {'age': age, 'issues': issues, 'option_ids': ids, 'canonical_changes': changes,
                             'registration_state': state, 'eligible': eligible,
                             'ready': eligible and state in READY_STATES}
            prepared.summary['age_' + age['state']] += 1
            prepared.summary['eligible'] += eligible
            prepared.summary['ready_to_register'] += row['portal']['ready']
            prepared.summary['already_registered'] += state in {'registered', 'already_registered_external'}
            prepared.summary['registration_blocked'] += state not in READY_STATES | {'registered', 'already_registered_external'}
            prepared.summary['portal_invalid'] += bool(issues)
            prepared.origins.update(row['origins'].values())
            save(prepared, row)
            last = roll
    prepared.db.commit()
    prepared.catalog = catalog


def subset(prepared, name):
    return (r for r in prepared.records() if SUBSETS[name](r))
