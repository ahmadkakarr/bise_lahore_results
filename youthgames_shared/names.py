"""Portal name formatting: preserve every letter, canonicalize separators only."""

import re

NAME_POLICY = 'portal-name-separators-v1'
SEPARATORS = " .'-"
TRANSLATE = str.maketrans({'\u2018':"'",'\u2019':"'",
    '\u2010':'-','\u2011':'-','\u2012':'-','\u2013':'-','\u2014':'-','\u2212':'-'})


def valid_name(value):
    if not isinstance(value,str) or not value or not value[0].isalpha() or not value[-1].isalpha():return False
    previous_letter=False
    for char in value:
        if char.isalpha():previous_letter=True
        elif char in SEPARATORS and previous_letter:previous_letter=False
        else:return False
    return previous_letter


def canonical_name(value):
    if not isinstance(value,str):raise ValueError('Name must be text')
    text=re.sub(r'\s+',' ',value.translate(TRANSLATE)).strip()
    if any(not char.isalpha() and char not in SEPARATORS for char in text):
        raise ValueError('Name contains characters requiring an individual correction')
    def separator(match):
        run=match[0]
        # Space before/after punctuation is formatting, not another name token.
        punctuation={ch for ch in run if ch!=' '}
        if len(punctuation)>1:raise ValueError('Ambiguous mixed punctuation in name')
        return next(iter(punctuation)) if punctuation else ' '
    text=re.sub(r"[ .'-]+",separator,text).strip(SEPARATORS)
    if not valid_name(text):raise ValueError('Name does not match portal letter/separator groups')
    if ''.join(c for c in value if c.isalpha())!=''.join(c for c in text if c.isalpha()):
        raise ValueError('Name formatting must preserve every original letter')
    return text


def name_changes(data):
    return {field:{'from':data[field],'to':canonical_name(data[field]),'policy':NAME_POLICY}
        for field in ('FullName','FatherName') if canonical_name(data[field])!=data[field]}
