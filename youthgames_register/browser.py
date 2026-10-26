"""Browser adapter; one armed, verified applicant POST per worker attempt."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from .config import sha
from .institutes import institute_fields, private_manual_supported
from .domain import SELECTS, TEXT, classify, form_values, uc_label, option


class PostGate:
    def __init__(self):
        self.armed = False
        self.sent = False
        self.fields = None
        self.photo_hash = None
        self.verified_file = None
        self.before_dispatch = None
        self.error = None

    async def route(self, route):
        request = route.request
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            await route.continue_()
            return
        try:
            url = urlparse(request.url)
            if not self.armed or self.sent or request.method != 'POST' or url.scheme != 'https' or url.netloc != 'youthgames.punjab.gov.pk' or url.path != '/applicant':
                raise ValueError('Unarmed or unexpected mutating request blocked')
            message = BytesParser(policy=default).parsebytes(b'Content-Type: ' + request.headers.get('content-type', '').encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + (request.post_data_buffer or b''))
            fields, photo, token = {}, None, False
            photo_filename, photo_size = None, None
            for part in message.iter_parts():
                key = part.get_param('name', header='content-disposition')
                body = part.get_payload(decode=True) or b''
                if key == '_token':
                    token = bool(body)
                elif key == 'profile_photo':
                    if photo is not None:
                        raise ValueError('Duplicate image field')
                    photo = sha(body)
                    photo_filename, photo_size = part.get_filename(), len(body)
                else:
                    if key in fields:
                        raise ValueError('Duplicate applicant field')
                    fields[key] = body.decode('utf-8')
            # The site's form includes optional institute fields. All other fields
            # must match the snapshot; no CSRF token is persisted or fabricated.
            optional = {'institution_id', 'institute_name'}
            for key in optional:
                if key not in (self.fields or {}) and fields.get(key, ''):
                    raise ValueError('Open Category contains stale institute data')
            actual = {k: v for k, v in fields.items() if k not in optional or k in (self.fields or {})}
            if not token:
                raise ValueError('Outgoing CSRF token is absent')
            if actual != self.fields:
                differences = sorted(k for k in set(actual) | set(self.fields or {}) if actual.get(k) != (self.fields or {}).get(k))
                raise ValueError('Outgoing multipart fields differ: ' + ', '.join(differences))
            if not self.verified_file or self.verified_file['sha256'] != self.photo_hash or photo_filename != self.verified_file['name']:
                raise ValueError('Outgoing image field does not match the verified browser File')
            # Chromium may omit uploaded file bytes from Network postData while
            # retaining the file-part filename. Verify the actual File's bytes
            # in the browser before intent; compare wire bytes when available.
            if photo_size is None or (photo_size > 0 and photo != self.photo_hash):
                raise ValueError('Outgoing multipart image differs from the attached snapshot')
            if self.before_dispatch is None:
                raise ValueError('No durable submission intent callback')
            self.before_dispatch()
            self.sent = True
            self.armed = False
            await route.continue_()
        except BaseException as exc:
            self.error = type(exc).__name__ + ': ' + str(exc)
            await route.abort()


@asynccontextmanager
async def visible_browser(headless=False):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        try:
            yield browser
        finally:
            try:
                await browser.close()
            except Exception:
                pass


class FormSession:
    """One context/tab for a worker; each attempt receives a new request gate."""
    def __init__(self, browser):
        self.browser = browser
        self.context = self.page = None
        self.gate = PostGate()

    async def route(self, route):
        gate = self.gate
        await gate.route(route)

    async def start_attempt(self):
        if self.context is None:
            try:
                self.context = await self.browser.new_context(viewport={'width': 1440, 'height': 1100})
                await self.context.route('**/*', self.route)
                self.page = await self.context.new_page()
            except BaseException:
                await self.close()
                raise
        if self.page.is_closed():
            raise ValueError('Worker tab was closed; resume the command to continue')
        if self.gate.armed:
            raise ValueError('Previous submission gate is still armed')
        self.gate = PostGate()
        return self.page, self.gate

    def disarm(self):
        self.gate.armed = False
        self.gate = PostGate()  # Background requests between jobs stay unarmed.

    async def close(self):
        self.disarm()
        if self.context is not None:
            try:
                await self.context.close()
            except BaseException:
                pass
            finally:
                self.context = self.page = None


@asynccontextmanager
async def form_session(browser, reuse=True):
    session = FormSession(browser) if reuse else None
    try:
        yield session
    finally:
        if session is not None:
            await session.close()


async def choose(form, field, label):
    selector = '#' + SELECTS[field]
    await form.locator(selector + ' option').filter(has_text=re.compile(r'^\s*' + re.escape(label) + r'\s*$', re.I)).first.wait_for(state='attached', timeout=20000)
    choices = await form.locator(selector).evaluate("s=>[...s.options].map(o=>({value:o.value,text:o.text,disabled:o.disabled}))")
    value = option(choices, label)
    await form.locator(selector).select_option(value=value, force=True)
    return value


async def load_form(page, config):
    # A repeated #registration URL can become a same-document navigation with
    # no HTTP response. The base URL forces a fresh GET in the reused tab.
    # Hero images, media and third-party assets may never reach window.load.
    # DOM readiness plus the form/option checks below is the actual contract.
    response = await page.goto(config['portal'].split('#', 1)[0], wait_until='domcontentloaded', timeout=60000)
    if response is None or response.status != 200:
        raise ValueError('Portal form did not load successfully')
    form = page.locator('#registration-form')
    await form.wait_for(state='attached')
    contract = await form.evaluate("f=>({action:f.action,method:f.method,enctype:f.enctype})")
    if contract != {'action': 'https://youthgames.punjab.gov.pk/applicant', 'method': 'post', 'enctype': 'multipart/form-data'}:
        raise ValueError('Registration form action/method changed')
    return form


async def catalog(browser, config, data, session=None, emit=None):
    owned = session is None
    session = session or FormSession(browser)
    evidence = None
    try:
        page, _ = await session.start_attempt()
        if emit:
            from .browser_evidence import PageEvidence
            evidence = PageEvidence(page,emit,{'scope':'catalog'})
        form = await load_form(page, config)
        for field in ('Category', 'SubCategory', 'Division', 'District', 'Tehsil'):
            await choose(form, field, data[field])
        options = {}
        for field, ident in SELECTS.items():
            options[field] = await form.locator('#' + ident).evaluate("s=>[...s.options].map(o=>({value:o.value,text:o.text,disabled:o.disabled}))")
        sports = {}
        for gender in options['Gender']:
            if gender['value']:
                await choose(form, 'Gender', gender['text'])
                sports[gender['text']] = await form.locator('#sport-id').evaluate("s=>[...s.options].map(o=>({value:o.value,text:o.text,disabled:o.disabled}))")
        dob = await form.locator('#dob').evaluate('e=>({min:e.min,max:e.max})')
        if not dob['min'] or not dob['max']:
            raise ValueError('Live category DOB bounds are unavailable')
        if dob != {'min': config['inspection']['observed_secondary_dob_min'], 'max': config['inspection']['observed_secondary_dob_max']}:
            raise ValueError('Category age rules changed; inspect/update the profile before preparing')
        html = await page.content()
        if not re.search(r'maxProfilePhotoSize\s*=\s*256\s*\*\s*1024', html):
            raise ValueError('Live image-size contract changed')
        schema = await form.evaluate("f=>[...f.querySelectorAll('input,select')].filter(e=>e.type!=='hidden').map(e=>({id:e.id,name:e.name,type:e.type,required:e.required,pattern:e.pattern||'',accept:e.accept||''}))")
        return {'private_manual_supported': private_manual_supported(html), 'options': options, 'sports_by_gender': sports, 'dob': dob,
                'location': {k: data[k] for k in ('Division', 'District', 'Tehsil')}, 'schema': schema,
                'photo_max_bytes': 262144, 'form_action': 'https://youthgames.punjab.gov.pk/applicant'}
    finally:
        if evidence:evidence.close()
        session.disarm()
        if owned:
            await session.close()


async def fill(page, config, item):
    form = await load_form(page, config)
    data, expected = item['data'], {}
    for field in ('Category', 'SubCategory', 'Gender', 'Division', 'District', 'Tehsil', 'UC', 'Sport', 'KitSize', 'JoggersSize'):
        label = uc_label(data, config) if field == 'UC' else data[field]
        value = await choose(form, field, label)
        if value != item['option_ids'][field]:
            raise ValueError('Live option mapping changed for ' + field)
    # load_form performs a fresh GET for each student, even in a reused tab.
    # Overwrite/compare every applicant field and reject stale institute data.
    for field, value in form_values(data).items():
        await form.locator('#' + TEXT[field]).fill(value)
    institutes = institute_fields(data, private_manual_supported(await page.content()))
    if data['SubCategory'] == 'Private':
        await form.locator('#use-manual-institute-btn').click()
        await form.locator('#institute-name-text').fill(institutes['institute_name'])
    photo = Path(item['photo']['path'])
    if sha(photo.read_bytes()) != item['photo']['sha256']:
        raise ValueError('Snapshotted photo bytes changed')
    await form.locator('#profile-photo-input').set_input_files(photo)
    snapshot = await form.evaluate('''f=>({
        values:Object.fromEntries([...f.querySelectorAll('input,select')].filter(e=>e.name&&e.type!=='file'&&e.name!=='_token').map(e=>[e.name,e.value])),
        files:[...f.querySelector('#profile-photo-input').files].map(f=>({name:f.name,size:f.size,type:f.type})),
        invalid:[...f.elements].filter(e=>e.id!=='captcha'&&e.willValidate&&!e.validity.valid).map(e=>e.id),
        dob:{min:f.querySelector('#dob').min,max:f.querySelector('#dob').max}
    })''')
    if snapshot['invalid'] or snapshot['dob'] != item['dob_rule'] or len(snapshot['files']) != 1 or not snapshot['files'][0]['size']:
        raise ValueError('Filled form validation/age/image comparison failed: ' + ', '.join(snapshot['invalid']))
    for field, ident in SELECTS.items():
        name = await form.locator('#' + ident).get_attribute('name')
        if snapshot['values'].get(name) != item['option_ids'][field]:
            raise ValueError('Selected dropdown differs for ' + field)
        expected[name] = item['option_ids'][field]
    for field, value in form_values(data).items():
        name = await form.locator('#' + TEXT[field]).get_attribute('name')
        if snapshot['values'].get(name) != value:
            raise ValueError('Filled text differs for ' + field)
        expected[name] = value
    if any(snapshot['values'].get(k, '') != v for k, v in institutes.items()):
        raise ValueError('Institute differs from the approved source')
    if data['SubCategory'] == 'Private':
        expected.update(institutes)
    return form, expected, snapshot


def arithmetic_answer(challenge):
    """Only solve the visible, bounded addition format; never evaluate page code."""
    match = re.fullmatch(r'\s*([0-9]{1,2})\s*\+\s*([0-9]{1,2})\s*=\s*\?\s*', challenge)
    if not match:
        raise ValueError('Unsupported visible CAPTCHA format; no submission attempted')
    total = int(match[1]) + int(match[2])
    if total > 99:
        raise ValueError('CAPTCHA answer exceeds the supported two-digit format')
    return str(total)


async def automatic_answer(form, job_id, log=True):
    display, field = form.locator('#number-captcha-display'), form.locator('#captcha')
    for _ in range(3):
        challenge = await display.inner_text()
        answer = arithmetic_answer(challenge)
        await field.fill(answer)
        if await display.inner_text() == challenge:
            if log:print(json.dumps({'job': job_id, 'phase': 'captcha_solved', 'captcha_mode': 'auto'}), flush=True)
            return answer
        await field.fill('')
    raise ValueError('Visible CAPTCHA kept changing; no submission attempted')


async def terminal_line(prompt):
    """Cancellable Linux terminal input; no executor thread can hold shutdown open."""
    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()
    future, buffer = loop.create_future(), bytearray()
    def readable():
        if future.done():
            return
        try:
            value = os.read(fd, 1)
            if not value:
                future.set_exception(EOFError('Terminal input closed'))
            elif value == b'\n':
                future.set_result(buffer.decode('utf-8').rstrip('\r'))
            elif len(buffer) >= 64:
                future.set_exception(ValueError('CAPTCHA terminal input is too long'))
            else:
                buffer.extend(value)
        except Exception as exc:
            future.set_exception(exc)
    sys.stdout.write(prompt); sys.stdout.flush()
    loop.add_reader(fd, readable)
    try:
        return await future
    finally:
        loop.remove_reader(fd)


async def assisted_answer(form, job_id):
    challenge = await form.locator('#number-captcha-display').inner_text()
    # Printing the visible challenge for assisted entry does not disclose student
    # identity or reuse a challenge from a previous context.
    try:
        answer = (await terminal_line(f'Job {job_id} CAPTCHA [{challenge.strip()}], answer (/skip to leave ready): ')).strip()
    except EOFError:
        return None
    if answer == '/skip':
        return None
    if not re.fullmatch(r'[0-9]{1,2}', answer):
        raise ValueError('CAPTCHA answer must be one or two digits')
    await form.locator('#captcha').fill(answer)
    return answer


async def send(page, form, expected, gate, before_intent, before_dispatch, photo_hash):
    valid = await form.evaluate('f=>f.checkValidity()')
    if not valid:
        raise ValueError('Form is invalid before the declaration; no submission attempted')
    await form.locator('#registration-submit-btn').click()
    modal = page.locator('#disclaimerModal')
    await modal.wait_for(state='visible')
    await modal.locator('#disclaimer-checkbox').check()
    file_proof = await form.locator('#profile-photo-input').evaluate('''async e=>{
        if(e.files.length!==1) return null;
        const f=e.files[0];const bytes=await f.arrayBuffer();
        const hash=await crypto.subtle.digest('SHA-256',bytes);
        return {name:f.name,size:f.size,sha256:[...new Uint8Array(hash)].map(v=>v.toString(16).padStart(2,'0')).join('')};
    }''')
    if not file_proof or not file_proof['size'] or file_proof['sha256'] != photo_hash:
        raise ValueError('Browser File hash does not match the approved image')
    before_intent()
    gate.verified_file = file_proof
    gate.fields, gate.photo_hash = {**expected, 'disclaimer_accepted': '1'}, photo_hash
    captcha = await form.locator('#captcha').input_value()
    gate.fields['captcha'] = captcha
    gate.before_dispatch = before_dispatch
    gate.armed = True
    final_response = None
    try:
        async with page.expect_navigation(wait_until='domcontentloaded', timeout=90000) as navigation:
            await modal.locator('#disclaimer-agree-button').click()
        final_response = await navigation.value
        if urlparse(page.url).netloc != 'youthgames.punjab.gov.pk':
            raise ValueError('Applicant response left the official portal')
        html = await page.content()
        status = final_response.status if final_response else 0
        return classify(html, status), html, status
    except BaseException:
        gate.armed = False
        raise
