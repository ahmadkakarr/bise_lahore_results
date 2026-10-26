"""Real browser/navigation coverage, with synthetic identities and local routes."""
import asyncio
import base64
from argparse import Namespace
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.async_api import async_playwright
from bise_results.runner import lookup, Pacer
from bise_results.parser import ReviewRequired


class HSSCPhotoBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def scenario(self, missing_photo=False, redirect=False):
        image=BytesIO();Image.new('RGB',(120,130),'blue').save(image,format='JPEG')
        portrait=image.getvalue();captcha=base64.b64encode(portrait).decode()
        form=f'''<form method="post" action="/ResultPart2.aspx">
        <input type="radio" id="rdlistCourse_0" name="rdlistCourse" value="SSC"><label for="rdlistCourse_0">Matric</label>
        <input type="radio" id="rdlistCourse_1" name="rdlistCourse" value="HSSC"><label for="rdlistCourse_1">Inter</label>
        <select id="ddlExamType"><option value="2">Part-II</option></select>
        <select id="ddlExamYear"><option value="2024">2024</option></select>
        <input id="txtFormNo"><input id="txtCaptcha">
        <img id="imgCaptcha" src="data:image/jpeg;base64,{captcha}"><input type="submit" id="Button1"></form>'''
        source=(Path(__file__).parent/'fixtures/hssc-part2.html').read_text()
        if not missing_photo:source=source.replace('</body>','<img id="imgDisplay" src="ImageLoader.aspx?Updateid=synthetic/PIC/1.jpg"></body>')
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                context=await browser.new_context();page=await context.new_page()
                async def intercept(route):
                    if 'ImageLoader.aspx' in route.request.url:
                        if redirect:await route.fulfill(status=302,headers={'location':'https://evil.example/image.jpg'})
                        else:await route.fulfill(content_type='image/jpeg',body=portrait)
                    elif route.request.method=='POST':await route.fulfill(content_type='text/html',body=source)
                    else:await route.fulfill(content_type='text/html',body=form)
                await context.route('**/*',intercept)
                # APIRequestContext requests don't use browser route interception.
                class Response:
                    ok=not redirect;status=302 if redirect else 200;headers={}
                    async def body(self):return portrait
                    async def dispose(self):pass
                async def get(url, **kwargs):
                    self.assertEqual(kwargs['max_redirects'],0)
                    self.assertTrue(url.startswith('http://result.biselahore.com/ImageLoader.aspx?'))
                    return Response()
                with tempfile.TemporaryDirectory() as temp,patch('bise_results.runner.recognize',return_value={'text':'ABC123','confidence':99,'variants_agree':True}),patch.object(context.request,'get',get):
                    args=Namespace(captcha_mode='auto',missing_dob_policy='estimate',estimated_age_at_exam=18,save_student_photos=True)
                    call=lookup(page,{'roll':'500001','year':2024,'course':'HSSC','exam_type':'2'},Path(temp),args,Pacer(0),asyncio.Lock())
                    if missing_photo or redirect:
                        with self.assertRaises(ReviewRequired):await call
                        self.assertFalse((Path(temp)/'parsed.json').exists())
                    else:
                        parsed,receipt=await call
                        self.assertEqual(parsed['date_of_birth'],'')
                        self.assertEqual(parsed['age_estimate']['age_at_reference_year'],18)
                        self.assertIn('student-photo.jpg',receipt)
                        self.assertEqual((Path(temp)/'student-photo.jpg').read_bytes(),portrait)
            finally:await browser.close()

    async def test_success_saves_original_and_separate_age_estimate(self):await self.scenario()
    async def test_missing_student_photo_never_substitutes_board_logo(self):await self.scenario(missing_photo=True)
    async def test_photo_redirect_fails_with_captures_preserved(self):await self.scenario(redirect=True)
