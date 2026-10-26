"""Keep an operator's cohort estimate separate from a student's official DOB."""
from datetime import date


def apply_age_policy(parsed, policy='require', age_at_exam=18):
    if policy not in ('require', 'estimate') or not 1 <= age_at_exam <= 100:
        raise ValueError('Invalid missing-DOB policy or estimated exam age')
    if parsed.get('date_of_birth'):
        return
    if parsed.get('course') != 'HSSC' or policy != 'estimate':
        from .parser import ReviewRequired
        raise ReviewRequired('Required DOB is absent from the HSSC result card; verified DOB source needed')
    parsed['dob_status'] = 'missing'
    parsed['age_estimate'] = {
        'reference_year': int(parsed['year']), 'age_at_reference_year': age_at_exam,
        'source': 'operator_cohort_assumption', 'precision': 'approximate_years',
        'individual_verified': False,
    }
    parsed['sports_registration_ready'] = False
    parsed['sports_registration_blockers'] = ['verified_date_of_birth_required']


def estimated_age(parsed, year=None):
    estimate = parsed.get('age_estimate')
    if not estimate:
        return None
    return estimate['age_at_reference_year'] + (year or date.today().year) - estimate['reference_year']


def explicitly_estimated_hssc(parsed):
    """Collection retention permission, never sports-eligibility permission."""
    estimate = parsed.get('age_estimate', {})
    return (parsed.get('course') == 'HSSC' and parsed.get('dob_status') == 'missing'
            and not parsed.get('date_of_birth')
            and estimate.get('source') == 'operator_cohort_assumption'
            and estimate.get('precision') == 'approximate_years'
            and estimate.get('individual_verified') is False
            and type(estimate.get('reference_year')) is int
            and estimate['reference_year'] == parsed.get('year')
            and type(estimate.get('age_at_reference_year')) is int
            and 1 <= estimate['age_at_reference_year'] <= 100
            and parsed.get('sports_registration_ready') is False
            and 'verified_date_of_birth_required' in parsed.get('sports_registration_blockers', []))
