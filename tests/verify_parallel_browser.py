"""Explicit headed-browser check; every request is fulfilled locally.

Run separately with:
  .venv/bin/python -m unittest discover -s tests -p verify_parallel_browser.py -v
"""

import asyncio
from argparse import Namespace
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image
from playwright.async_api import async_playwright

from bise_results.runner import lookup, Pacer, SharedCooldown
from bise_results import MAX_WORKERS
from bise_results.exams import exam_type_for_grade, exam_details


class VisibleParallelBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_browser_contexts_have_overlapping_submissions(self):
        headless = os.environ.get('BISE_VERIFY_HEADLESS') == '1'
        workers = int(os.environ.get('BISE_VERIFY_WORKERS', '2'))
        grade = int(os.environ.get('BISE_VERIFY_CLASS', '10'))
        exam_type = exam_type_for_grade(grade)
        endpoint = '/' + exam_details(exam_type)['endpoint']
        self.assertIn(workers, range(2, MAX_WORKERS + 1))
        pixel = BytesIO()
        Image.new('RGB', (150, 50), 'white').save(pixel, format='PNG')
        image = base64.b64encode(pixel.getvalue()).decode()
        form = f'''<html><body><form method="post" action="{endpoint}">
          <input type="radio" id="rdlistCourse_0" name="rdlistCourse" value="SSC"><label for="rdlistCourse_0">Matric</label>
          <select id="ddlExamType" name="ddlExamType"><option value="{exam_type}">Annual</option></select>
          <select id="ddlExamYear" name="ddlExamYear"><option value="2026">2026</option></select>
          <input id="txtFormNo" name="txtFormNo"><input id="txtCaptcha" name="txtCaptcha">
          <img id="imgCaptcha" src="data:image/png;base64,{image}">
          <input id="Button1" type="submit" value="View Result">
          </form></body></html>'''
        fixture = (Path(__file__).parent / f'fixtures/ssc-part{exam_type}.html').read_text()
        ready = asyncio.Event()
        active, peak = 0, 0
        starts, finishes, phases = {}, {}, []
        cooldown = SharedCooldown()
        with tempfile.TemporaryDirectory() as directory:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=headless)
                try:
                    async def worker(number):
                        nonlocal active, peak
                        context = await browser.new_context()

                        async def intercept(route):
                            nonlocal active, peak
                            request = route.request
                            path = urlsplit(request.url).path
                            if path == '/' and request.method == 'GET':
                                await route.fulfill(content_type='text/html', body=form)
                            elif path == endpoint and request.method == 'POST':
                                roll = parse_qs(request.post_data)['txtFormNo'][0]
                                starts[number] = time.monotonic()
                                active += 1
                                peak = max(peak, active)
                                if len(starts) == workers:
                                    ready.set()
                                try:
                                    await asyncio.wait_for(ready.wait(), 10)
                                    await asyncio.sleep(0.25)
                                    await route.fulfill(content_type='text/html', body=fixture.replace('001234', roll))
                                finally:
                                    finishes[number] = time.monotonic()
                                    active -= 1
                            else:
                                await route.abort()

                        # All URLs are intercepted: no board/external network access.
                        await context.route('**/*', intercept)
                        page = await context.new_page()
                        folder = Path(directory) / str(number)
                        folder.mkdir()
                        try:
                            result, _ = await lookup(page, {'roll': f'{number:06d}', 'year': 2026, 'exam_type': exam_type}, folder,
                                                     Namespace(captcha_mode='auto'), Pacer(3, cooldown), asyncio.Lock(),
                                                     progress=lambda stage: phases.append({'worker': number, 'phase': stage}))
                            self.assertEqual(result['roll'], f'{number:06d}')
                        finally:
                            await context.close()

                    with patch('bise_results.runner.recognize', return_value={'text': 'ABC123', 'confidence': 99, 'variants_agree': True}):
                        await asyncio.gather(*(worker(number) for number in range(1, workers + 1)))
                finally:
                    await browser.close()
        self.assertEqual(peak, workers)
        self.assertLess(max(starts.values()), min(finishes.values()))
        proof = {'headless': headless, 'class': grade, 'contexts': workers,
                 'display_environment_present': bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')),
                 'simultaneous_submissions': peak, 'start_gap_seconds': round(max(starts.values()) - min(starts.values()), 3),
                 'submission_intervals_overlap': True, 'network': 'all requests intercepted and fulfilled locally',
                 'ocr': 'fixed fake candidate; this test measures concurrency, not OCR accuracy',
                 'board_results_refetched': 0, 'phases': phases}
        filename = f'headless-{workers}-workers-verification.json' if headless and workers > 3 else 'headless-workers-verification.json' if headless else 'parallel-workers-verification.json'
        path = Path(__file__).resolve().parents[1] / 'docs/evidence' / filename
        path.write_text(json.dumps(proof, indent=2) + '\n')
        print(json.dumps({key: value for key, value in proof.items() if key != 'phases'}))


if __name__ == '__main__':
    unittest.main()
