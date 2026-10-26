"""Read an applicant's original portrait without copying source photo tables."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from bise_results.photos import inspect_photo


def original(config):return config.get('photos',{}).get('mode')=='original_sqlite_reference'


def applicant_photo(config,origin,shared_path=None,shared_hash=None):
    if not original(config):
        return {'path':str(shared_path),'sha256':shared_hash,'kind':config['photos']['mode']}
    ref=dict(origin['photo_reference'])
    if ref['applicant_key']!=origin['data']['BForm'] or ref['roll']!=origin['roll']:
        raise ValueError('Original photo reference belongs to another applicant')
    if ref['source_database']!=config['photos']['source_database'] or ref['source_sha256']!=config['photos']['source_sha256']:
        raise ValueError('Original photo reference belongs to a different source')
    return {**ref,'kind':'original_sqlite_reference'}


def matches_profile(config,photo):
    if original(config):
        return (photo.get('kind')=='original_sqlite_reference'
                and photo.get('source_database')==config['photos']['source_database']
                and photo.get('source_sha256')==config['photos']['source_sha256'])
    return photo.get('sha256')==config['photos']['expected_sha256']


def payload(photo,expected_hash):
    if not isinstance(photo,dict):return Path(photo).read_bytes()
    if photo.get('kind')!='original_sqlite_reference':return Path(photo['path']).read_bytes()
    path=Path(photo['source_database']).resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        row=db.execute('''SELECT j.result_json,p.sha256,p.payload FROM student_photos p
            JOIN jobs j ON j.id=p.job_id WHERE p.job_id=?''',(photo['job_id'],)).fetchone()
    if not row:raise ValueError('Original student photo missing')
    record=json.loads(row[0]);key=re.sub(r'[-\s]','',record.get('student_cnic',''))
    if (key!=photo['applicant_key'] or record.get('roll')!=photo['roll'] or record.get('course')!='HSSC'
            or record.get('year')!=2024 or str(record.get('exam_type'))!='2'
            or row[1]!=expected_hash or hashlib.sha256(row[2]).hexdigest()!=expected_hash):
        raise ValueError('Original photo identity/checksum differs from applicant')
    inspect_photo(row[2])
    return row[2]
