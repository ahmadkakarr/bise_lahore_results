"""Original board student photos, hash bound to the exam/roll in SQLite."""
import hashlib
from io import BytesIO
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image

MAX_BYTES = 5 * 1024 ** 2


def photo_url(source, page_url):
    from .parser import Document
    node = Document(source).ids.get('imgDisplay')
    if node is None or node.tag != 'img' or not node.attrs.get('src'):
        raise ValueError('Student photo imgDisplay is absent; board logos are not student photos')
    url = urljoin(page_url, node.attrs['src'])
    parts = urlsplit(url)
    if (parts.scheme not in ('http', 'https') or parts.hostname != 'result.biselahore.com'
            or parts.username or parts.password or parts.port not in (None, 80, 443)
            or parts.path.lower() != '/imageloader.aspx' or not parts.query or parts.fragment):
        raise ValueError('Unexpected student-photo source')
    return url


def inspect_photo(payload):
    if not payload or len(payload) > MAX_BYTES:
        raise ValueError('Student photo is empty or exceeds 5 MiB')
    with Image.open(BytesIO(payload)) as image:
        if image.format not in ('JPEG', 'PNG') or image.width < 30 or image.height < 30:
            raise ValueError('Student photo is not a supported portrait image')
        if image.width * image.height > 20_000_000:
            raise ValueError('Student photo dimensions exceed limit')
        fmt, width, height = image.format, image.width, image.height
        image.verify()
    return {'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload),
            'mime_type': 'image/jpeg' if fmt == 'JPEG' else 'image/png',
            'extension': 'jpg' if fmt == 'JPEG' else 'png', 'width': width, 'height': height}


async def download_photo(page, source):
    url = photo_url(source, page.url)
    response = await page.context.request.get(url, timeout=30000, max_redirects=0)
    try:
        if not response.ok or response.status != 200:
            raise ValueError(f'Student photo unavailable (HTTP {response.status})')
        if int(response.headers.get('content-length', '0')) > MAX_BYTES:
            raise ValueError('Student photo exceeds 5 MiB')
        payload = await response.body()
        metadata = inspect_photo(payload)
        return payload, {**metadata, 'source_url': url, 'source': 'board_imgDisplay',
                         'status': 'available', 'storage': 'sqlite:student_photos',
                         'portal_upload_size_ok': len(payload) <= 256 * 1024}
    finally:
        await response.dispose()


def save_photo(db, job_id, parsed, folder):
    metadata = parsed.get('student_photo')
    if not metadata:
        return
    payload = (Path(folder) / ('student-photo.' + metadata['extension'])).read_bytes()
    actual = inspect_photo(payload)
    if any(metadata.get(k) != v for k, v in actual.items()):
        raise ValueError('Student photo checksum or image metadata differs from capture')
    db.execute('''INSERT INTO student_photos(job_id,sha256,mime_type,width,height,source_url,payload)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET sha256=excluded.sha256,
        mime_type=excluded.mime_type,width=excluded.width,height=excluded.height,
        source_url=excluded.source_url,payload=excluded.payload''',
        (job_id, actual['sha256'], actual['mime_type'], actual['width'], actual['height'],
         metadata['source_url'], payload))


def export_photos(db, destination):
    """Materialize portable originals for future per-applicant photo mapping."""
    from .runner import atomic_write
    root = Path(destination).resolve(); root.mkdir(parents=True, exist_ok=True)
    count = written = 0; mappings = []
    for row in db.execute('''SELECT j.roll,j.year,j.course,j.exam_type,j.result_json,p.*
            FROM student_photos p JOIN jobs j ON j.id=p.job_id WHERE j.state='verified'
            ORDER BY j.year,j.course,j.exam_type,j.roll'''):
        metadata = inspect_photo(row['payload'])
        if metadata['sha256'] != row['sha256']:
            raise ValueError('Stored student photo failed checksum verification')
        # Identity is rechecked before producing an applicant-photo mapping.
        parsed = json.loads(row['result_json'])
        if (parsed.get('roll') != row['roll'] or parsed.get('year') != row['year']
                or parsed.get('course', 'SSC') != row['course']
                or str(parsed.get('exam_type', '2')) != row['exam_type']
                or parsed.get('student_photo', {}).get('sha256') != row['sha256']):
            raise ValueError('Stored photo does not match parsed student identity')
        if not str(row['roll']).isdigit() or not str(row['year']).isdigit() or row['course'] not in ('SSC','HSSC') or row['exam_type'] not in ('1','2'):
            raise ValueError('Unsafe exam/roll photo export key')
        relative = Path(str(row['year'])) / row['course'] / ('Part-' + row['exam_type']) / (row['roll'] + '.' + metadata['extension'])
        path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Student photo destination escapes export directory')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            atomic_write(path, row['payload']); written += 1
        mappings.append({'roll':row['roll'],'year':row['year'],'course':row['course'],
                         'exam_type':row['exam_type'],'photo':str(relative),'sha256':row['sha256'],
                         'student_cnic':parsed['student_cnic'],
                         'portal_upload_size_ok':len(row['payload']) <= 256*1024})
        count += 1
    atomic_write(root/'Photo Mapping.json', json.dumps(mappings, ensure_ascii=False, indent=2))
    return {'photos':count,'written':written,'skipped':count-written,'output':str(root)}
