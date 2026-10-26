"""Verified optional Open Category and explicit Private manual institute flow."""
from bise_results.parser import Document


def private_manual_supported(html):
    doc = Document(html)
    expected = {'institution-id': ('institution_id', 'hidden'),
                'institute-name-text': ('institute_name', 'text')}
    form = doc.ids.get('registration-form')
    if form is None:
        return False
    nodes = {n.attrs.get('id'): n for tag in ('input', 'button') for n in form.elements(tag)}
    for ident, (name, kind) in expected.items():
        node = nodes.get(ident)
        if node is None or node.attrs.get('name') != name or node.attrs.get('type') != kind:
            return False
    button = nodes.get('use-manual-institute-btn')
    return button is not None and button.attrs.get('type') == 'button'


def institute_fields(data, supported=True):
    subcategory = data.get('SubCategory')
    if subcategory == 'Open Category':
        return {'institution_id': '', 'institute_name': ''}
    if subcategory != 'Private' or not supported:
        raise ValueError('Unsupported institute flow; verified Private manual entry required')
    name = data.get('Institute', '')
    if not isinstance(name, str) or not name.strip() or name != name.strip() or any(ord(c) < 32 for c in name):
        raise ValueError('Private Institute must contain the complete actual school name')
    return {'institution_id': '', 'institute_name': name}
