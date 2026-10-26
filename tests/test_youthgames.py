"""Synthetic registration inputs and intercepted browser submissions only."""

from copy import deepcopy
from contextlib import redirect_stdout, asynccontextmanager
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from PIL import Image
from playwright.async_api import async_playwright

from bise_normalize import COLUMNS
from youthgames_register import app, browser
from youthgames_register.config import sha, signature, load, input_snapshot
from youthgames_register.domain import SELECTS, validate, classify, option, uc_label, form_values
from youthgames_register.reports import export
from youthgames_register.store import opened, import_batch, jobs, begin, intent, dispatched, finish, check_receipt


def data():
    return dict(zip(COLUMNS, ['Schools (Secondary)', 'Open Category', '', 'Synthetic Student', 'Synthetic Father',
        '0000001234562', 'Female', '2010-05-09', 'Lahore', 'Lahore', 'Shalimar', 'UC No. 178, Manawan',
        '03211234567', 'LAHORE', 'Athletics', 'Medium', '38']))


def catalog():
    pairs = {'Category': [('2', 'Schools (Secondary)')], 'SubCategory': [('12', 'Open Category')],
        'Gender': [('Male', 'Male'), ('Female', 'Female'), ('Other', 'Other')], 'Division': [('6', 'Lahore')],
        'District': [('21', 'Lahore')], 'Tehsil': [('77', 'Shalimar')], 'UC': [('2253', 'UC No. 178, Manawan Ram Pur Jageer')],
        'Sport': [('1', 'Athletics')], 'KitSize': [('2', 'Medium')], 'JoggersSize': [('6', '38')]}
    options = {k: [{'value': v, 'text': t, 'disabled': False} for v, t in values] for k, values in pairs.items()}
    return {'options': options, 'sports_by_gender': {k: options['Sport'] for k in ('Male', 'Female', 'Other')},
            'dob': {'min': '2009-01-01', 'max': '2016-12-31'},
            'location': {'Division': 'Lahore', 'District': 'Lahore', 'Tehsil': 'Shalimar'}, 'schema': [],
            'photo_max_bytes': 262144, 'form_action': 'https://youthgames.punjab.gov.pk/applicant'}


def config():
    settings = json.loads(Path('youthgames_register/profile.template.json').read_text())
    settings['state']['write_reports'] = True  # Explicitly exercise the optional legacy report path.
    return settings


def applicant(photo):
    row = data()
    cat = catalog()
    ids = {field: option(cat['sports_by_gender']['Female'] if field == 'Sport' else cat['options'][field], uc_label(row, config()) if field == 'UC' else row[field]) for field in SELECTS}
    return {'roll': '001234', 'data': row, 'origins': {}, 'operator_confirmation': {},
        'photo': {'path': str(photo), 'sha256': sha(photo.read_bytes()), 'kind': 'operator_selected_shared_graphic'},
        'option_ids': ids, 'dob_rule': cat['dob'], 'catalog_hash': signature(cat), 'validation': validate(row, config(), cat)}


SUCCESS = '<form id="registration-form" data-registration-success="1"></form><div id="registrationSuccessModal"><span id="registration-success-tracking-id">TEST-YG-123</span></div>'
REJECTED = '<form id="registration-form" data-has-server-errors="1"></form><div class="invalid-feedback d-block">Invalid captcha answer.</div>'


class DomainTests(unittest.TestCase):
    def test_visible_addition_is_bounded_and_never_evaluates_code(self):
        for challenge, answer in [('5 + 8 = ?', '13'), ('\n 0 + 9 = ?\t', '9'), ('9+9=?', '18'), ('40 + 59 = ?', '99')]:
            self.assertEqual(browser.arithmetic_answer(challenge), answer)
        for challenge in ('5 - 8 = ?', '5 * 8 = ?', '2+3', '2+3=? extra', '__import__("os")', '100+0=?', '99+99=?'):
            with self.assertRaises(ValueError):
                browser.arithmetic_answer(challenge)

    def test_dob_boundaries_and_out_of_range_rows(self):
        row = data()
        for dob in ('2009-01-01', '2016-12-31'):
            row['DOB'] = dob
            self.assertEqual(validate(row, config(), catalog())['state'], 'ready')
        for dob in ('2008-12-31', '2017-01-01'):
            row['DOB'] = dob
            self.assertEqual(validate(row, config(), catalog())['state'], 'ineligible')

    def test_names_and_form_identifier_formats_are_preserved(self):
        values = form_values(data())
        self.assertEqual(values['FullName'], 'Synthetic Student')
        self.assertEqual(values['FatherName'], 'Synthetic Father')
        self.assertEqual(values['BForm'], '00000-0123456-2')
        self.assertEqual(values['Phone'], '0321-1234567')

    def test_uc_alias_is_exact_and_scoped(self):
        self.assertEqual(uc_label(data(), config()), 'UC No. 178, Manawan Ram Pur Jageer')
        row = data(); row['District'] = 'Other'
        with self.assertRaises(ValueError):
            uc_label(row, config())
        with self.assertRaises(ValueError):
            option(catalog()['options']['UC'] * 2, 'UC No. 178, Manawan Ram Pur Jageer')

    def test_success_requires_tracking_and_server_marker(self):
        self.assertEqual(classify(SUCCESS, 200)['state'], 'registered')
        self.assertEqual(classify('<p>OK</p>', 200)['state'], 'uncertain')
        self.assertEqual(classify('<script>registration-success-tracking-id</script>', 200)['state'], 'uncertain')
        self.assertEqual(classify(SUCCESS, 500)['state'], 'uncertain')

    def test_current_portal_flasher_tracking_without_visible_tracking_element(self):
        flash = '<script class="flasher-js">const options=[];options.push(' + json.dumps({'envelopes': [{'type': 'success', 'message': 'Registration submitted successfully. Your tracking ID is TEST-YG-456.'}]}) + ');</script>'
        html = '<form id="registration-form" data-registration-success="1"></form><div id="registrationSuccessModal">Registration Successful</div>' + flash
        result = classify(html, 200)
        self.assertEqual(result['state'], 'registered')
        self.assertEqual(result['tracking'], 'TEST-YG-456')
        self.assertEqual(classify(flash, 200)['state'], 'uncertain')
        self.assertEqual(classify(html.replace('flasher-js', 'unrelated-script'), 200)['state'], 'uncertain')
        self.assertEqual(classify(html + '<span id="registration-success-tracking-id">CONFLICT-123</span>', 200)['state'], 'uncertain')

    def test_arbitrary_tracking_text_cannot_register_an_applicant(self):
        html = '<form id="registration-form" data-registration-success="1"></form><p>Registration submitted successfully. Your tracking ID is TEST-123.</p>'
        self.assertEqual(classify(html, 200)['state'], 'uncertain')

    def test_explicit_captcha_and_duplicate_rejections_are_distinct(self):
        result = classify(REJECTED, 200)
        self.assertEqual(result['state'], 'rejected')
        self.assertTrue(result['retryable_captcha'])
        duplicate = REJECTED.replace('Invalid captcha answer.', 'This CNIC is already registered.')
        self.assertEqual(classify(duplicate, 200)['state'], 'already_registered_external')
        self.assertFalse(classify(duplicate, 200)['retryable_captcha'])
        generic = REJECTED.replace(' d-block', '')
        self.assertFalse(classify(generic, 200)['retryable_captcha'])
        phone = REJECTED.replace('Invalid captcha answer.', 'This mobile phone is already registered.')
        self.assertEqual(classify(phone, 200)['state'], 'rejected')

    def test_invalid_gender_options_or_new_institute_flow_blocks(self):
        row = data(); row['Gender'] = 'Unexpected'
        self.assertEqual(validate(row, config(), catalog())['state'], 'blocked_input')
        row = data(); row['SubCategory'] = 'Private'
        self.assertIn('unsupported_non_open_institute_flow', validate(row, config(), catalog())['issues'])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.photo = self.root / 'photo.jpeg'; Image.new('RGB', (3, 3), 'white').save(self.photo)
        self.database = self.root / 'registration.sqlite3'
        self.item = applicant(self.photo)

    def imported(self, db, item=None, profile=None):
        settings = config()
        if profile:
            settings['profile'] = profile
        return import_batch(db, settings, {'csv': 'fake'}, catalog(), self.root, [item or self.item])

    def test_registration_key_skips_reimport_and_other_profiles(self):
        with opened(self.database) as db:
            self.imported(db)
            job = jobs(db, 'Lahore')[0]
            attempt = begin(db, job); intent(db, job['id'], attempt); dispatched(db, job['id'], attempt)
            finish(db, job['id'], attempt, 'registered', tracking='TEST-123')
            self.imported(db, profile='Another profile')
            self.assertEqual(jobs(db, 'Another profile', ('ready',)), [])
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_crash_after_intent_is_uncertain_and_cannot_retry(self):
        with opened(self.database) as db:
            self.imported(db); job = jobs(db, 'Lahore')[0]
            attempt = begin(db, job); intent(db, job['id'], attempt)
        with opened(self.database) as db:
            self.assertEqual(jobs(db, 'Lahore')[0]['state'], 'uncertain')
            self.imported(db)
            self.assertEqual(jobs(db, 'Lahore', ('ready',)), [])
            with self.assertRaises(ValueError):
                finish(db, job['id'], attempt, 'ready')

    def test_crash_before_intent_returns_ready(self):
        with opened(self.database) as db:
            self.imported(db); begin(db, jobs(db, 'Lahore')[0])
        with opened(self.database) as db:
            self.assertEqual(jobs(db, 'Lahore')[0]['state'], 'ready')

    def test_dispatch_is_one_shot_per_attempt(self):
        with opened(self.database) as db:
            self.imported(db); job = jobs(db, 'Lahore')[0]
            attempt = begin(db, job)
            with self.assertRaises(ValueError):
                dispatched(db, job['id'], attempt)
            intent(db, job['id'], attempt); dispatched(db, job['id'], attempt)
            with self.assertRaises(ValueError):
                dispatched(db, job['id'], attempt)

    def test_changed_registered_snapshot_is_conflict_not_resubmission(self):
        with opened(self.database) as db:
            self.imported(db); job = jobs(db, 'Lahore')[0]
            attempt = begin(db, job); intent(db, job['id'], attempt)
            finish(db, job['id'], attempt, 'registered', tracking='TEST-123')
            changed = deepcopy(self.item); changed['data']['Sport'] = 'Changed'
            self.imported(db, changed)
            self.assertEqual(jobs(db, 'Lahore')[0]['payload_hash'], job['payload_hash'])
            self.assertEqual(jobs(db, 'Lahore', ('ready',)), [])
            self.assertEqual(db.execute('SELECT count(*) FROM conflicts').fetchone()[0], 1)

    def test_conflicting_bform_rows_are_blocked(self):
        changed = deepcopy(self.item); changed['data']['FullName'] = 'Different Synthetic Student'
        with opened(self.database) as db:
            import_batch(db, config(), {'csv': 'fake'}, catalog(), self.root, [self.item, changed])
            self.assertEqual(jobs(db, 'Lahore', ('ready',)), [])
            self.assertEqual(jobs(db, 'Lahore')[0]['state'], 'blocked_input')

    def test_profile_lock_and_export_preserve_receipt_folder(self):
        with opened(self.database) as db:
            with self.assertRaises(ValueError):
                with opened(self.database):
                    pass
            self.imported(db)
            destination = self.root / 'output'
            export(db, config(), destination)
            receipt = destination / 'Receipts/original.txt'; receipt.parent.mkdir(); receipt.write_text('keep')
            export(db, config(), destination)
            self.assertEqual(receipt.read_text(), 'keep')

    def test_input_confirmation_and_shared_image_are_pinned(self):
        row = data(); csv_path = self.root / 'Normalized.csv'
        with csv_path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=COLUMNS); writer.writeheader(); writer.writerow(row)
        prov = self.root / 'provenance.jsonl'; prov.write_text(json.dumps({'roll': '001234', 'data': row, 'origins': {}}) + '\n')
        csv_hash = sha(csv_path.read_bytes()); prov_hash = sha(prov.read_bytes())
        manifest = self.root / 'manifest.json'; manifest.write_text(json.dumps({'files': {'Normalized.csv': csv_hash, 'Data/provenance.jsonl': prov_hash}}))
        settings = config(); settings['input'] = {'csv': str(csv_path), 'manifest': str(manifest), 'provenance': str(prov), 'expected_csv_sha256': csv_hash}
        settings['confirmation']['input_sha256'] = csv_hash
        settings['photos'].update(shared_file=str(self.photo), expected_sha256=sha(self.photo.read_bytes()))
        path = self.root / 'profile.json'; path.write_text(json.dumps(settings))
        loaded = load(path)
        with input_snapshot(loaded, path) as (rows, *_):
            self.assertEqual(len(rows), 1)
        self.photo.write_bytes(b'damaged')
        with self.assertRaises(ValueError):
            with input_snapshot(loaded, path):
                pass

    def test_reconcile_captured_success_preserves_original_receipt_and_never_replays(self):
        html = self.root / 'response.html'; html.write_text(SUCCESS)
        receipt_path = self.root / 'receipt.json'
        with opened(self.database) as db:
            self.imported(db); job = jobs(db, 'Lahore')[0]
            attempt = begin(db, job); intent(db, job['id'], attempt); dispatched(db, job['id'], attempt)
            receipt = {'payload_hash': job['payload_hash'], 'post_dispatched': True, 'http_status': 200, 'attempt': attempt,
                'outcome': {'state': 'uncertain'}, 'files': {'response.html': {'path': str(html), 'sha256': sha(html.read_bytes())}}}
            receipt_path.write_text(json.dumps(receipt)); before = sha(receipt_path.read_bytes())
            finish(db, job['id'], attempt, 'uncertain', receipt=receipt_path)
            result = app.reconcile(db, config(), job['id'])
            self.assertEqual(result['state'], 'registered')
            stored = jobs(db, 'Lahore')[0]
            self.assertNotEqual(stored['receipt'], str(receipt_path))
            self.assertTrue(check_receipt(stored['receipt']))
            self.assertEqual(sha(receipt_path.read_bytes()), before)
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)
            self.assertEqual(jobs(db, 'Lahore', ('ready',)), [])


def fixture():
    cat = catalog()
    selects = []
    names = {'Category': 'category_id', 'SubCategory': 'sub_category_id', 'Gender': 'gender', 'Division': 'division_id',
             'District': 'district_id', 'Tehsil': 'tehsil_id', 'UC': 'uc_id', 'Sport': 'sport_id', 'KitSize': 'kit_size_id', 'JoggersSize': 'jogger_size_id'}
    for field, ident in SELECTS.items():
        options = ''.join(f'<option value="{o["value"]}">{o["text"]}</option>' for o in cat['options'][field])
        selects.append(f'<select id="{ident}" name="{names[field]}" required>{options}</select>')
    inputs = '<input id="first-name" name="first_name" required><input id="last-name" name="last_name" required>'
    inputs += '<input id="cnic" name="cnic" required><input id="dob" name="date_of_birth" type="date" min="2009-01-01" max="2016-12-31" required>'
    inputs += '<input id="mobile-phone" name="mobile_phone" required><input id="address" name="address" required>'
    return '''<!doctype html><form id="registration-form" action="https://youthgames.punjab.gov.pk/applicant" method="post" enctype="multipart/form-data">
      <input type="hidden" name="_token" value="synthetic-csrf"><input id="disclaimer_accepted" type="hidden" name="disclaimer_accepted" value="">
      <input type="hidden" name="institution_id" value=""><input type="hidden" name="institute_name" value="">
      ''' + ''.join(selects) + inputs + '''
      <input id="profile-photo-input" name="profile_photo" type="file" required>
      <div id="number-captcha-display">2 + 3 = ?</div><input id="captcha" name="captcha" required>
      <button id="registration-submit-btn" type="button" onclick="document.getElementById('disclaimerModal').style.display='block'">Submit</button></form>
      <div id="disclaimerModal" style="display:none"><input type="checkbox" id="disclaimer-checkbox">
      <button type="button" id="disclaimer-agree-button" onclick="if(document.getElementById('disclaimer-checkbox').checked){document.getElementById('disclaimer_accepted').value='1';document.getElementById('registration-form').submit()}">Agree</button></div>
      <script>const maxProfilePhotoSize = 256 * 1024;</script>'''


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.photo = self.root / 'photo.jpeg'; Image.new('RGB', (3, 3), 'white').save(self.photo)
        self.item = applicant(self.photo)
        self.manager = await async_playwright().start()
        self.browser = await self.manager.chromium.launch(headless=True)
        self.addAsyncCleanup(self.manager.stop)
        self.addAsyncCleanup(self.browser.close)
        self.posts = []
        self.post_bodies = []

    async def test_form_load_does_not_wait_for_an_unrelated_hanging_image(self):
        import asyncio
        context = await self.browser.new_context()
        page = await context.new_page()
        waiting = asyncio.Event()
        async def route(request):
            if request.request.url.endswith('/slow-image.png'):
                await waiting.wait()
                await request.abort()
            else:
                await request.fulfill(status=200,content_type='text/html',body=fixture()+'<img src="/slow-image.png">')
        await context.route('**/*',route)
        try:
            form = await asyncio.wait_for(browser.load_form(page,config()),timeout=5)
            self.assertEqual(await form.get_attribute('id'),'registration-form')
            self.assertFalse(waiting.is_set())
        finally:
            waiting.set()
            await context.close()

    async def intercepted(self, context, gate, response=SUCCESS, status=200, mutate=None, abort_post=False):
        owner = self

        class Route:
            def __init__(self, real):
                self.real, self.request = real, real.request

            async def abort(self):
                await self.real.abort()

            async def continue_(self):
                if self.request.method == 'POST':
                    owner.posts.append(self.request.url)
                    owner.post_bodies.append(self.request.post_data or '')
                    if abort_post:
                        await self.real.abort()
                    else:
                        await self.real.fulfill(status=status, content_type='text/html', body=response)
                else:
                    html = fixture()
                    if mutate:
                        html = mutate(html)
                    await self.real.fulfill(status=200, content_type='text/html', body=html)

        await context.route('**/*', lambda route: gate.route(Route(route)))

    async def test_real_multipart_gate_checks_image_fields_and_allows_one_post(self):
        context = await self.browser.new_context()
        gate = browser.PostGate(); await self.intercepted(context, gate)
        page = await context.new_page()
        form, expected, _ = await browser.fill(page, config(), self.item)
        await form.locator('#captcha').fill('5')
        marks = []
        try:
            outcome, _, _ = await browser.send(page, form, expected, gate, lambda: marks.append('intent'), lambda: marks.append('dispatch'), self.item['photo']['sha256'])
        except Exception:
            self.fail(gate.error or 'Browser navigation failed')
        self.assertEqual(outcome['state'], 'registered', gate.error)
        self.assertEqual(marks, ['intent', 'dispatch'])
        self.assertTrue(gate.sent)
        self.assertEqual(len(self.posts), 1)
        await context.close()

    async def test_auto_mode_submits_intercepted_form_without_prompt(self):
        database = self.root / 'auto-jobs.sqlite3'
        settings = config(); settings['captcha']['mode'] = 'auto'
        with opened(database) as db:
            import_batch(db, settings, {'csv': 'fake'}, catalog(), self.root, [self.item])
            with patch('youthgames_register.browser.assisted_answer', AsyncMock(side_effect=AssertionError('must not prompt'))):
                result = await app.process_one(db, self.proxy_browser(), settings, jobs(db, 'Lahore')[0], self.root / 'auto-reports')
            self.assertEqual(result['state'], 'registered')
            self.assertEqual(len(self.posts), 1)
            self.assertTrue(check_receipt(jobs(db, 'Lahore')[0]['receipt']))

    async def test_two_students_share_one_tab_with_new_gates_and_correct_payloads(self):
        second = deepcopy(self.item)
        second['roll'] = '001235'
        second['data'].update(FullName='Second Synthetic Student', FatherName='Second Synthetic Father', BForm='0000001234572', Phone='03221234567', DOB='2011-06-10')
        photo = self.root / 'second.png'; Image.new('RGB', (4, 4), 'blue').save(photo)
        second['photo'].update(path=str(photo), sha256=sha(photo.read_bytes()))
        proxy = self.proxy_browser()
        database = self.root / 'reuse-jobs.sqlite3'
        async with browser.form_session(proxy) as session:
            with opened(database) as db:
                import_batch(db, config(), {'csv': 'fake'}, catalog(), self.root, [self.item, second])
                selected = jobs(db, 'Lahore')
                result = await app.process_one(db, proxy, config(), selected[0], self.root / 'reuse-reports', session=session)
                page, context = session.page, session.context
                first_receipt = jobs(db, 'Lahore')[0]['receipt']
                self.assertEqual(result['state'], 'registered')
                self.assertFalse(page.is_closed())
                self.assertFalse(session.gate.armed)
                result = await app.process_one(db, proxy, config(), selected[1], self.root / 'reuse-reports', session=session)
                self.assertEqual(result['state'], 'registered')
                self.assertIs(session.page, page); self.assertIs(session.context, context)
                self.assertEqual(len(context.real.pages), 1)
                self.assertEqual(len(self.posts), 2)
                self.assertIn('Second Synthetic Student', self.post_bodies[1])
                self.assertIn('Second Synthetic Father', self.post_bodies[1])
                self.assertIn('00000-0123457-2', self.post_bodies[1])
                self.assertNotIn('00000-0123456-2', self.post_bodies[1])
                self.assertIn('second.png', self.post_bodies[1])
                self.assertNotIn('photo.jpeg', self.post_bodies[1])
                self.assertTrue(check_receipt(first_receipt))
                self.assertTrue(all(check_receipt(j['receipt']) for j in jobs(db, 'Lahore')))
            self.assertFalse(page.is_closed())
        self.assertTrue(page.is_closed())

    async def test_reused_tab_stays_unarmed_between_attempts_and_preview(self):
        proxy = self.proxy_browser()
        database = self.root / 'reuse-preview.sqlite3'
        async with browser.form_session(proxy) as session:
            with opened(database) as db:
                import_batch(db, config(), {'csv': 'fake'}, catalog(), self.root, [self.item])
                result = await app.process_one(db, proxy, config(), jobs(db, 'Lahore')[0], self.root / 'reuse-preview', preview=True, session=session)
                self.assertEqual(result['state'], 'preview_verified')
                self.assertFalse(session.page.is_closed())
                self.assertFalse(session.gate.armed)
                try:
                    async with session.page.expect_navigation(timeout=1000):
                        await session.page.locator('#registration-form').evaluate('f=>f.submit()')
                except Exception:
                    pass
                self.assertEqual(self.posts, [])
                self.assertIn('Unarmed', session.gate.error)
                self.assertEqual(jobs(db, 'Lahore')[0]['state'], 'ready')

    async def test_reused_tab_reloads_fresh_captcha_for_each_student(self):
        proxy = self.proxy_browser()
        proxy_context = await proxy.new_context()
        get_count = 0
        def mutate(html):
            nonlocal get_count
            get_count += 1
            return html.replace('2 + 3 = ?', '5 + 8 = ?' if get_count == 1 else '9 + 9 = ?')
        session = browser.FormSession(proxy)
        # Install a custom intercepted route once; start_attempt reuses it.
        session.context = proxy_context
        await self.intercepted(proxy_context.real, session, mutate=mutate)
        session.page = await proxy_context.new_page()
        try:
            for expected in ('13', '18'):
                page, gate = await session.start_attempt()
                form, _, _ = await browser.fill(page, config(), self.item)
                self.assertEqual(await browser.automatic_answer(form, 1), expected)
                self.assertFalse(gate.sent)
                session.disarm()
            self.assertEqual(get_count, 2)
            self.assertEqual(self.posts, [])
        finally:
            await session.close()

    async def test_reused_session_closes_after_cancel_and_keeps_pre_intent_job_ready(self):
        import asyncio
        async def cancel(form, job):
            raise asyncio.CancelledError()
        proxy = self.proxy_browser()
        database = self.root / 'reuse-cancel.sqlite3'
        with opened(database) as db:
            import_batch(db, config(), {'csv': 'fake'}, catalog(), self.root, [self.item])
            with self.assertRaises(asyncio.CancelledError):
                async with browser.form_session(proxy) as session:
                    await app.process_one(db, proxy, config(), jobs(db, 'Lahore')[0], self.root / 'reuse-cancel', answer_provider=cancel, session=session)
            self.assertIsNone(session.context)
            self.assertEqual(jobs(db, 'Lahore')[0]['state'], 'ready')
            self.assertEqual(self.posts, [])

    async def test_auto_answer_matches_visible_challenge_without_dispatch(self):
        context = await self.browser.new_context()
        gate = browser.PostGate(); await self.intercepted(context, gate)
        page = await context.new_page(); form, _, _ = await browser.fill(page, config(), self.item)
        await form.locator('#number-captcha-display').evaluate('e=>e.textContent="5 + 8 = ?"')
        self.assertEqual(await browser.automatic_answer(form, 1), '13')
        self.assertEqual(await form.locator('#captcha').input_value(), '13')
        self.assertFalse(gate.sent); self.assertEqual(self.posts, [])
        await form.locator('#number-captcha-display').evaluate('e=>e.textContent="Unknown image"')
        with self.assertRaisesRegex(ValueError, 'Unsupported visible CAPTCHA'):
            await browser.automatic_answer(form, 1)
        self.assertEqual(self.posts, [])
        await context.close()

    async def test_cancel_while_waiting_for_manual_answer_leaves_ready_without_post(self):
        async def cancelled(prompt):
            raise __import__('asyncio').CancelledError()
        database = self.root / 'cancel-jobs.sqlite3'
        settings = config(); settings['captcha']['mode'] = 'assist'
        with opened(database) as db:
            import_batch(db, settings, {'csv': 'fake'}, catalog(), self.root, [self.item])
            with patch('youthgames_register.browser.terminal_line', cancelled):
                with self.assertRaises(__import__('asyncio').CancelledError):
                    await app.process_one(db, self.proxy_browser(), settings, jobs(db, 'Lahore')[0], self.root / 'cancel-reports')
            self.assertEqual(jobs(db, 'Lahore')[0]['state'], 'ready')
            self.assertEqual(self.posts, [])
            self.assertIsNone(db.execute('SELECT intent_at FROM attempts').fetchone()[0])

    async def test_preview_cannot_submit_even_if_page_script_tries(self):
        context = await self.browser.new_context()
        gate = browser.PostGate(); await self.intercepted(context, gate)
        page = await context.new_page(); await browser.fill(page, config(), self.item)
        try:
            async with page.expect_navigation(timeout=3000):
                await page.locator('#registration-form').evaluate('f=>f.submit()')
        except Exception:
            pass
        self.assertEqual(self.posts, [])
        self.assertFalse(gate.sent)
        self.assertIn('Unarmed', gate.error)
        await context.close()

    async def test_wrong_outgoing_photo_is_aborted_before_dispatch(self):
        context = await self.browser.new_context()
        gate = browser.PostGate(); await self.intercepted(context, gate)
        page = await context.new_page(); form, expected, _ = await browser.fill(page, config(), self.item)
        await form.locator('#captcha').fill('5')
        try:
            await browser.send(page, form, expected, gate, lambda: None, lambda: self.fail('must not dispatch'), 'wrong hash')
        except Exception:
            pass
        self.assertEqual(self.posts, [])
        self.assertFalse(gate.sent)
        await context.close()

    async def test_invalid_required_field_stops_before_intent(self):
        context = await self.browser.new_context()
        gate = browser.PostGate(); await self.intercepted(context, gate)
        page = await context.new_page(); form, expected, _ = await browser.fill(page, config(), self.item)
        with self.assertRaises(ValueError):
            await browser.send(page, form, expected, gate, lambda: self.fail('must not arm'), lambda: None, self.item['photo']['sha256'])
        self.assertEqual(self.posts, [])
        await context.close()

    def proxy_browser(self, response=SUCCESS, abort_post=False):
        owner = self

        class Context:
            def __init__(self, real):
                self.real = real

            async def route(self, pattern, handler):
                await owner.intercepted(self.real, handler.__self__, response=response, abort_post=abort_post)

            async def new_page(self):
                return await self.real.new_page()

            async def close(self):
                await self.real.close()

        class Browser:
            async def new_context(self, **kwargs):
                return Context(await owner.browser.new_context(**kwargs))

        return Browser()

    async def answer(self, form, job):
        await form.locator('#captcha').fill('5')
        return '5'

    async def test_full_preview_has_no_post_then_submission_persists_verified_receipt(self):
        database = self.root / 'jobs.sqlite3'
        destination = self.root / 'reports'
        with opened(database) as db:
            import_batch(db, config(), {'csv': 'fake'}, catalog(), self.root, [self.item])
            job = jobs(db, 'Lahore')[0]
            result = await app.process_one(db, self.proxy_browser(), config(), job, destination, preview=True)
            self.assertEqual(result['state'], 'preview_verified')
            self.assertEqual(self.posts, [])
            self.assertEqual(jobs(db, 'Lahore')[0]['state'], 'ready')
            result = await app.process_one(db, self.proxy_browser(), config(), job, destination, answer_provider=self.answer)
            self.assertEqual(result['state'], 'registered')
            self.assertEqual(len(self.posts), 1)
            stored = jobs(db, 'Lahore')[0]
            self.assertTrue(check_receipt(stored['receipt']))
            self.assertEqual(stored['tracking'], 'TEST-YG-123')
            self.assertEqual(jobs(db, 'Lahore', ('ready',)), [])
            self.assertTrue((destination / 'Registration Results.xlsx').is_file())

    async def test_transport_failure_after_dispatch_becomes_uncertain_without_retry(self):
        database = self.root / 'jobs.sqlite3'
        with opened(database) as db:
            import_batch(db, config(), {'csv': 'fake'}, catalog(), self.root, [self.item])
            result = await app.process_one(db, self.proxy_browser(abort_post=True), config(), jobs(db, 'Lahore')[0], self.root / 'reports', answer_provider=self.answer)
            self.assertEqual(result['state'], 'uncertain')
            self.assertEqual(len(self.posts), 1)
            import_batch(db, config(), {'csv': 'fake'}, catalog(), self.root, [self.item])
            self.assertEqual(jobs(db, 'Lahore', ('ready',)), [])

    async def test_explicit_rejection_is_saved_as_rejection_not_success(self):
        database = self.root / 'jobs.sqlite3'
        with opened(database) as db:
            import_batch(db, config(), {'csv': 'fake'}, catalog(), self.root, [self.item])
            result = await app.process_one(db, self.proxy_browser(response=REJECTED), config(), jobs(db, 'Lahore')[0], self.root / 'reports', answer_provider=self.answer)
            self.assertEqual(result['state'], 'rejected')
            self.assertTrue(result['retryable_captcha'])
            self.assertEqual(len(self.posts), 1)

    async def test_changed_form_option_aborts_before_dispatch(self):
        changed = deepcopy(self.item); changed['option_ids']['UC'] = 'bad option'
        database = self.root / 'jobs.sqlite3'
        with opened(database) as db:
            import_batch(db, config(), {'csv': 'fake'}, catalog(), self.root, [changed])
            result = await app.process_one(db, self.proxy_browser(), config(), jobs(db, 'Lahore')[0], self.root / 'reports', answer_provider=self.answer)
            self.assertEqual(result['state'], 'blocked_input')
            self.assertFalse(result['post_dispatched'])
            self.assertEqual(self.posts, [])

    async def test_skip_before_declaration_leaves_job_ready(self):
        async def skip(form, job):
            return None
        database = self.root / 'jobs.sqlite3'
        with opened(database) as db:
            import_batch(db, config(), {'csv': 'fake'}, catalog(), self.root, [self.item])
            result = await app.process_one(db, self.proxy_browser(), config(), jobs(db, 'Lahore')[0], self.root / 'reports', answer_provider=skip)
            self.assertEqual(result['state'], 'ready')
            self.assertEqual(self.posts, [])

    async def test_existing_uncertain_job_stops_the_whole_profile_before_new_post(self):
        database = self.root / 'jobs.sqlite3'
        with opened(database) as db:
            import_batch(db, config(), {'csv': 'fake'}, catalog(), self.root, [self.item])
            job = jobs(db, 'Lahore')[0]; attempt = begin(db, job); intent(db, job['id'], attempt)
            finish(db, job['id'], attempt, 'uncertain')

        @asynccontextmanager
        async def fake_browser(**kwargs):
            yield object()

        settings = config(); settings['state'] = {'database': str(database), 'output': str(self.root / 'reports')}
        with patch('youthgames_register.app.load', return_value=settings), patch('youthgames_register.app.web.visible_browser', fake_browser), patch('youthgames_register.app.prepare_in', AsyncMock(return_value={'counts': {'uncertain': 1}})), patch('youthgames_register.app.process_one', AsyncMock(side_effect=AssertionError('must not attempt another applicant'))):
            result = await app.run(self.root / 'profile.json', 'submit')
        self.assertEqual(result['attempted_now'], 0)
        self.assertEqual(result['blocked'], 'uncertain_submission_requires_reconciliation')


if __name__ == '__main__':
    unittest.main()
