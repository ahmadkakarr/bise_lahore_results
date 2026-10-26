"""Immediate registration reports; receipt folders are never replaced or deleted."""

from collections import Counter
import csv
import json
from pathlib import Path
import tempfile

from openpyxl import Workbook
from bise_normalize import COLUMNS
from bise_normalize.publish import csv_safe

from .config import signature, sha
from .store import jobs, check_receipt


def output_folder(path, profile):
    path = Path(path)
    marker = path / 'Data/owner.json'
    if path.exists() and not marker.exists() and any(path.iterdir()):
        raise ValueError('Registration output contains unmanaged files')
    if marker.exists() and json.loads(marker.read_text()) != {'application': 'youthgames_register', 'profile': profile}:
        raise ValueError('Registration output belongs to another profile/application')
    (path / 'Data').mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(json.dumps({'application': 'youthgames_register', 'profile': profile}))
    return path


def export(db, config, destination):
    destination = output_folder(destination, config['profile'])
    if not config['state'].get('write_reports', False):
        counts = dict(db.execute('''SELECT j.state,count(*) FROM jobs j JOIN members m ON m.job=j.id
            JOIN profiles p ON p.batch=m.batch WHERE p.name=? GROUP BY j.state''', (config['profile'],)))
        counts['total'] = sum(counts.values())
        counts['input_conflicts'] = db.execute('SELECT count(*) FROM conflicts c JOIN profiles p ON p.batch=c.batch WHERE p.name=?', (config['profile'],)).fetchone()[0]
        # Receipt bytes are checked by prepare/verification, not rehashed for
        # every student's completion. Tracking and receipt paths stay in SQLite.
        return counts
    rows = jobs(db, config['profile'])
    counts = dict(Counter(r['state'] for r in rows))
    counts['total'] = len(rows)
    conflicts = db.execute('SELECT c.job,c.detail FROM conflicts c JOIN profiles p ON p.batch=c.batch WHERE p.name=? ORDER BY c.job,c.detail', (config['profile'],)).fetchall()
    counts['input_conflicts'] = len(conflicts)
    integrity = {str(r['id']): check_receipt(r['receipt']) for r in rows if r['state'] == 'registered'}
    counts['registered_receipt_integrity_failures'] = sum(not ok for ok in integrity.values())
    report_signature = signature({'rows': [dict(r) for r in rows], 'conflicts': [dict(r) for r in conflicts], 'integrity': integrity})
    manifest_path = destination / 'Data/manifest.json'
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text())
        if old.get('signature') == report_signature and all((destination / p).is_file() and sha((destination / p).read_bytes()) == h for p, h in old['files'].items()):
            return counts
    with tempfile.TemporaryDirectory(prefix='.registration-report-', dir=destination) as work:
        staging = Path(work)
        columns = ['Job', 'Roll No', 'State', 'Tracking ID', 'Detail', 'Receipt Valid', *COLUMNS]
        values = []
        for row in rows:
            item = json.loads(row['payload'])
            values.append([row['id'], row['roll'], row['state'], row['tracking'] or '', row['detail'] or '', integrity.get(str(row['id']), ''), *[item['data'][k] for k in COLUMNS]])

        def xlsx(filename, sheets):
            book = Workbook(write_only=True)
            from bise_normalize.publish import sheet, excel_row
            for title, headings, contents in sheets:
                page = sheet(book, title, headings)
                for line in contents:
                    excel_row(page, line)
            book.save(staging / filename)

        xlsx('Registration Results.xlsx', [('Overview', ['State', 'Count'], sorted(counts.items())),
             ('All Applicants', columns, values), ('Input Conflicts', ['Job', 'Detail'], [tuple(r) for r in conflicts])])
        xlsx('Missing Details.xlsx', [('Missing Details', columns, [v for v in values if v[2] == 'blocked_input'])])
        xlsx('Uncertain Submissions.xlsx', [('Uncertain', columns, [v for v in values if v[2] == 'uncertain'])])
        for filename, state in [('Registered.csv', 'registered'), ('Ready.csv', 'ready'), ('Ineligible.csv', 'ineligible')]:
            with (staging / filename).open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(columns)
                writer.writerows([[csv_safe(value) for value in line] for line in values if line[2] == state])
        (staging / 'Summary.txt').write_text('Youth Games registration: ' + config['profile'] + '\n' + '\n'.join(f'{k}: {v}' for k, v in sorted(counts.items())) + '\nRegistered requires a captured server receipt; uncertain is never auto-retried.\n')
        files = {p.name: sha(p.read_bytes()) for p in staging.iterdir()}
        for p in list(staging.iterdir()):
            p.replace(destination / p.name)
        tmp = destination / 'Data/manifest.tmp'
        tmp.write_text(json.dumps({'application': 'youthgames_register', 'signature': report_signature, 'counts': counts, 'files': files}, indent=2))
        tmp.replace(manifest_path)
    return counts
