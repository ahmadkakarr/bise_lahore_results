"""Explicit annual exam selectors; legacy collection defaults to SSC Part-II."""
EXAMS = {
    '1': {'grade': 9, 'part': 'I', 'endpoint': 'rollslip.aspx'},
    '2': {'grade': 10, 'part': 'II', 'endpoint': 'resultpart2.aspx'},
}


def course_for_grade(grade):
    if grade not in (9, 10, 12):
        raise ValueError('Supported classes are 9, 10 and 12 (Inter Part-II)')
    return 'HSSC' if grade == 12 else 'SSC'


def exam_type_for_grade(grade):
    course_for_grade(grade)
    return '1' if grade == 9 else '2'


def exam_details(exam_type, course='SSC'):
    if course == 'HSSC' and str(exam_type) == '2':
        return {'grade': 12, 'part': 'II', 'endpoint': 'resultpart2.aspx'}
    if course != 'SSC':
        raise ValueError('Supported HSSC exam is Part-II Annual')
    try:
        return EXAMS[str(exam_type)]
    except KeyError as exc:
        raise ValueError('Supported SSC exam types are 1 (9th) and 2 (10th)') from exc
