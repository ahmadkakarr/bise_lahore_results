"""One friendly, stable exam master with institution reports and reversible archives."""

from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import time
import zipfile
import os

from .store import check_receipt, jobs_for, now, retained_in_database
from .exams import exam_details

RESULT_COLUMNS = [
    "roll", "institution", "name", "father_name", "date_of_birth",
    "student_cnic", "father_cnic", "registration_number", "group",
    "obtained", "maximum", "percentage", "result_statement", "total_raw",
    "declared_on", "fetched_at", "result_status", "data_completeness", "data_notes", "state", "error", "result_card",
]
TEXT_COLUMNS = {"roll", "date_of_birth", "student_cnic", "father_cnic", "registration_number"}


class _HashedZipStream:
    """Append-only ZIP output: SHA-256 is calculated while writing, without reread."""
    def __init__(self, stream):
        self.stream, self.sha, self.position = stream, hashlib.sha256(), 0

    def write(self, value):
        count = self.stream.write(value)
        if count != len(value):
            raise OSError('Incomplete ZIP write')
        self.sha.update(value)
        self.position += count
        return count

    def tell(self):
        return self.position

    def seek(self, *args):
        raise io.UnsupportedOperation('Append-only ZIP stream')

    def flush(self):
        self.stream.flush()


def safe_text(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


FORMAT_VERSION = 5


def master_name(year, exam_type='2', course='SSC'):
    if course=='HSSC':return f'Inter {year} - 12th Master Results'
    return f"Matric {year} - 9th Master Results" if str(exam_type) == '1' else f"Matric {year} - Master Results"


def workbook(path, records, summary, title="Student Results", institutions=(), queue_summary=None):
    from .fast_excel import build_workbook
    build_workbook(path, records, summary, title, RESULT_COLUMNS, safe_text, TEXT_COLUMNS, institutions, queue_summary)


def write_group(folder, records, include_cards=True, report_name="results", title="Student Results", institutions=(), card_archive_name=None, queue_summary=None, card_sources=None, digests=None):
    from .presentation import LABELS, display
    folder.mkdir(parents=True, exist_ok=True)
    summary = dict(Counter(record["state"] for record in records))
    summary["total"] = len(records)
    workbook(folder / f"{report_name}.xlsx", records, summary, title, institutions, queue_summary)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=[LABELS[column] for column in RESULT_COLUMNS])
    writer.writeheader()
    for record in records:
        writer.writerow({LABELS[column]: safe_text(display(record, column)) for column in RESULT_COLUMNS})
    (folder / f"{report_name}.csv").write_text(stream.getvalue(), encoding="utf-8-sig", newline="")
    data = folder / 'Data'
    data.mkdir(exist_ok=True)
    public_records = [{key: value for key, value in record.items() if not key.startswith('_')} for record in records]
    (data / 'results.json').write_text(json.dumps(public_records, indent=2, ensure_ascii=False), encoding="utf-8")
    if include_cards:
        zip_name = card_archive_name or (f"{title} Result Cards.zip" if report_name != 'results' else 'Result Cards.zip')
        # PNG is already compressed. Deflating thousands of PNGs again costs CPU
        # with little size benefit; ZIP CRCs still verify every original byte.
        zip_path = folder / zip_name
        with zip_path.open('wb') as stream:
            hashed = _HashedZipStream(stream)
            with zipfile.ZipFile(hashed, 'w', zipfile.ZIP_STORED) as archive:
                for record in records:
                    _write_card(record, folder, archive, card_sources)
        if digests is not None:
            digests.record_written(zip_path, hashed.sha.hexdigest())
    return summary


def _write_card(record, folder, archive, card_sources):
    if record['state'] == 'verified' and not record.get('_database_only'):
        source = (Path(card_sources) / f"{record['roll']}.png") if card_sources is not None else Path(record['_receipt']['card.png']['path'])
        card = folder / 'Result Cards' / f"{record['roll']}.png"
        card.parent.mkdir(exist_ok=True)
        if card_sources is not None:
            # Share only within this new report generation. Never
            # link original evidence or a previous archived report.
            try:
                os.link(source, card)
            except OSError:
                shutil.copyfile(source, card)
        else:
            shutil.copyfile(source, card)
        archive.write(card, f'Result Cards/{card.name}')


def _manifest_is_current(destination, signature, database, digest=None):
    from .publishing import file_hashes
    path = destination / 'Data/manifest.json'
    if not path.is_file():
        return False
    try:
        manifest = json.loads(path.read_text())
        if manifest.get('database') != database:
            raise RuntimeError(f'Report folder belongs to another database: {destination}')
        if manifest.get('content_signature') != signature:
            return False
        actual = file_hashes(destination, digest=digest)
        actual.pop('Data/manifest.json', None)
        return manifest.get('content_signature') == signature and actual == manifest.get('files')
    except (ValueError, OSError):
        return False


def export_batch(db, batch, output, include_pending=True, progress=None, digests=None):
    from .publishing import archive_legacy, file_hashes, publish, unique_names, ArtifactDigests
    from .store import reparse_cached_results
    batch_row = db.execute('SELECT * FROM batches WHERE id=?', (batch,)).fetchone()
    if batch_row is None:
        raise ValueError('Unknown batch id')
    started = time.perf_counter()
    progress = progress or (lambda message: None)
    progress('Export: verifying saved records (no result refetch)...')
    timings = {}
    digests = digests if digests is not None else ArtifactDigests()
    verifier = lambda receipt: check_receipt(receipt, digests)
    # One exam master combines lists while the unique job key removes overlap.
    scope = (batch_row['year'], batch_row['course'], batch_row['exam_type'])
    exam = exam_details(batch_row['exam_type'], batch_row['course'])
    batches = db.execute('SELECT * FROM batches WHERE year=? AND course=? AND exam_type=? ORDER BY id', scope).fetchall()
    seen = set()
    for member in batches:
        reparse_cached_results(db, member['id'], receipt_checker=verifier, seen=seen)
    queue_summary = {row['state']: row['count'] for row in db.execute("SELECT state,count(*) AS count FROM jobs WHERE year=? AND course=? AND exam_type=? GROUP BY state", scope)}
    queue_summary['total'] = sum(queue_summary.values())
    condition = "" if include_pending else " AND state NOT IN ('pending','running')"
    rows = db.execute("SELECT * FROM jobs WHERE year=? AND course=? AND exam_type=?" + condition + " ORDER BY roll", scope)
    records = []
    for row in rows:
        state, error = row['state'], row['error']
        receipt = json.loads(row['receipt_json'] or '{}')
        database_only = retained_in_database(db,row)
        if state == 'verified' and not database_only and not verifier(receipt):
            state, error = 'needs_review', 'Saved card unavailable; rerun collection to recover'
        record = json.loads(row['result_json']) if state == 'verified' else {'roll': row['roll']}
        record.update({'state': state, 'error': error, '_receipt': receipt, '_database_only': database_only})
        records.append(record)
    timings['load_and_verify_seconds'] = round(time.perf_counter() - started, 3)
    summary = dict(Counter(record['state'] for record in records))
    summary['total'] = len(records)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"{'Inter' if batch_row['course']=='HSSC' else 'Matric'} {batch_row['year']} - Part-{exam['part']} Annual"
    archive_root = output.resolve().parent / 'artifacts/report-archive'
    database = db.execute('PRAGMA database_list').fetchone()[2]
    signature = hashlib.sha256(json.dumps({'format': FORMAT_VERSION, 'year': batch_row['year'], 'records': records, 'include_pending': include_pending, 'queue_summary': queue_summary,
                                           'inputs': [(row['id'], row['input_sha256']) for row in batches]}, sort_keys=True).encode()).hexdigest()
    batch_ids = [row['id'] for row in batches]
    if _manifest_is_current(destination, signature, database, digest=digests):
        archive_legacy(output, batch_ids, archive_root)
        progress(f'Export: already up to date ({time.perf_counter() - started:.1f}s).')
        return destination, summary
    if destination.exists() and not (destination / 'Data/manifest.json').is_file():
        raise RuntimeError(f'Report destination contains unmanaged files: {destination}')
    staging = Path(tempfile.mkdtemp(prefix='.building-', dir=output))
    try:
        groups = defaultdict(list)
        for record in records:
            if record['state'] == 'verified':
                groups[record.get('institution', '')].append(record)
        folder_names = unique_names(groups, fallback='Institution not provided')
        index = []
        for institution, members in sorted(groups.items()):
            label = folder_names[institution]
            relative = Path('Institutions') / label
            report_name = f'{label} Results'
            index.append({'institution_district': institution, 'records': len(members),
                          'folder': relative.as_posix(), 'workbook': f'{report_name}.xlsx', 'csv': f'{report_name}.csv'})
        main_name = master_name(batch_row['year'], batch_row['exam_type'], batch_row['course'])
        progress(f'Export: writing master Excel/CSV and cards for {len(records):,} records...')
        phase = time.perf_counter()
        write_group(staging, records, report_name=main_name, title=main_name, institutions=index, card_archive_name='All Result Cards.zip', queue_summary=queue_summary, digests=digests)
        timings['master_seconds'] = round(time.perf_counter() - phase, 3)
        phase = time.perf_counter()
        for number, entry in enumerate(index, 1):
            if len(index) <= 20 or number == 1 or number % 10 == 0 or number == len(index):
                progress(f'Export: institution reports {number}/{len(index)}...')
            write_group(staging / entry['folder'], groups[entry['institution_district']],
                        report_name=Path(entry['workbook']).stem, title=Path(entry['folder']).name,
                        card_sources=staging / 'Result Cards', digests=digests)
        timings['institutions_seconds'] = round(time.perf_counter() - phase, 3)
        # Cards are shared as folders/archives; Excel has no per-card URLs.
        (staging / 'Read Me.txt').write_text(
            f"START HERE: {main_name}.xlsx\n\n"
            "Overview: counts and clickable institution navigation.\n"
            "All Students: every student, DOB, identity numbers and available marks. No result-card URLs in Excel.\n"
            "Institution tabs: students filtered by the board's Institution/District.\n"
            "Subject Marks: theory/practical breakdown.\n"
            "Institutions folder: separate reports and card ZIPs for sharing.\n"
            "Result Cards folder: original card images, named by roll number.\n"
            "Blank marks mean the board did not provide an unambiguous numeric total.\n"
            "Partial/absent/not-improved records still retain student details.\n"
            "This folder updates in place on rerun. Previous versions are retained under artifacts/report-archive.\n"
            "Data contains structured exports and verification metadata.\n", encoding='utf-8')
        (staging / 'Data/institutions.json').write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding='utf-8')
        progress('Export: checking report hashes and publishing...')
        phase = time.perf_counter()
        report_hashes = file_hashes(staging, digest=digests)
        timings['report_hashes_seconds'] = round(time.perf_counter() - phase, 3)
        manifest = {'format_version': FORMAT_VERSION, 'batch': batch, 'included_batches': batch_ids,
                    'database': database, 'year': batch_row['year'], 'course': batch_row['course'], 'exam_type': batch_row['exam_type'],
                    'input_sha256': batch_row['input_sha256'], 'exported_at': now(),
                    'summary': summary, 'queue_summary': queue_summary, 'include_pending': include_pending,
                    'group_by': 'board Institution/District', 'content_signature': signature,
                    'complete_collection': queue_summary.get('verified', 0) == queue_summary['total'] and all(record['state'] == 'verified' for record in records),
                    'master_workbook': f'{main_name}.xlsx', 'export_timings': timings, 'files': report_hashes}
        (staging / 'Data/manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        publish(staging, destination, archive_root)
        archive_legacy(output, batch_ids, archive_root)
        progress(f'Export complete: {len(records):,} records in {time.perf_counter() - started:.1f}s.')
        return destination, summary
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def print_export_summary(destination, summary):
    manifest = json.loads((destination / 'Data/manifest.json').read_text())
    queue = manifest.get('queue_summary', summary)
    print(f"\nSaved student records: {summary.get('verified', 0)} / {queue['total']} imported rolls")
    print(f"Rows in master: {summary['total']}; not yet saved: {queue['total'] - queue.get('verified', 0)}")
    print(f"Open master file: {(destination / manifest['master_workbook']).resolve()}")
    print(f"Institution reports: {(destination / 'Institutions').resolve()}")
