import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import asyncio
from argparse import Namespace
import base64
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs
from PIL import Image
from playwright.async_api import async_playwright

from bise_results.parser import parse_result, ReviewRequired
from bise_results.store import locked_store, import_batch, jobs_for, retain_in_database, prepare_jobs
from bise_results.runner import lookup, Pacer

FIXTURE=Path(__file__).parent/'fixtures/hssc-part2.html'


class HSSCCollectionTests(unittest.TestCase):
    def test_real_layout_shape_has_identifiers_but_no_invented_dob(self):
        parsed=parse_result(FIXTURE.read_text(),'500001',2024,course='HSSC')
        self.assertEqual(parsed['course'],'HSSC')
        self.assertEqual(parsed['date_of_birth'],'')
        self.assertEqual(parsed['name'],'Sample HSSC Candidate')
        self.assertEqual(len(parsed['subjects']),7)
        self.assertTrue(parsed['subjects_raw'])

    def test_course_part_year_roll_and_session_must_match(self):
        source=FIXTURE.read_text()
        for args in ({'course':'SSC'},{'course':'HSSC','year':2025},
                     {'course':'HSSC','expected_roll':'500002'}):
            options={'expected_roll':'500001','year':2024};options.update(args)
            with self.assertRaises(ReviewRequired):parse_result(source,**options)
        for heading in ('INTERMEDIATE PART I ANNUAL EXAMINATION, 2024',
                        'INTERMEDIATE (PART I & II ) SECOND ANNUAL EXAMINATION, 2024'):
            changed=source.replace('INTERMEDIATE (PART I & II ) ANNUAL EXAMINATION, 2024',heading)
            with self.assertRaises(ReviewRequired):parse_result(changed,'500001',2024,course='HSSC')

    def test_ssc_and_hssc_same_roll_never_collide_and_retained_rows_skip_without_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);csv=root/'rolls.csv';csv.write_text('Roll Number\n500001\n')
            with locked_store(root/'results.sqlite3') as db:
                ssc=import_batch(db,csv,'Batch',2024)
                hssc=import_batch(db,csv,'Batch',2024,course='HSSC')
                self.assertNotEqual(ssc,hssc)
                self.assertEqual(import_batch(db,csv,'Batch',2024,course='HSSC'),hssc)
                job=jobs_for(db,hssc)[0]
                # A verified alternate layout must actually contain DOB; the
                # real inspected HSSC shape without DOB is not retainable.
                source=FIXTURE.read_text().replace('</body>', '<span id="lblDOB">09/05/2006</span></body>')
                parsed=parse_result(source,'500001',2024,course='HSSC')
                with db:db.execute("UPDATE jobs SET state='verified',result_json=?,receipt_json='{}' WHERE id=?",(json.dumps(parsed),job['id']))
                retain_in_database(db,jobs_for(db,hssc)[0],'verified DB only')
                self.assertEqual(prepare_jobs(db,hssc),[])
                self.assertEqual(jobs_for(db,ssc)[0]['state'],'pending')

    def test_hssc_retention_requires_both_actual_identifiers_and_correct_exam(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);csv=root/'rolls.csv';csv.write_text('Roll Number\n500001\n')
            with locked_store(root/'db.sqlite3') as db:
                batch=import_batch(db,csv,'Batch',2024,course='HSSC')
                for field in ('student_cnic','father_cnic','course','date_of_birth'):
                    parsed=parse_result(FIXTURE.read_text(),'500001',2024,course='HSSC');parsed[field]=''
                    with db:db.execute("UPDATE jobs SET state='verified',result_json=?",(json.dumps(parsed),))
                    with self.assertRaises(ValueError):retain_in_database(db,jobs_for(db,batch)[0],'invalid')


class HSSCDOBAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_browser_flow_rejects_missing_dob_and_selects_hssc_2024(self):
        image=BytesIO();Image.new('RGB',(150,50),'white').save(image,format='PNG')
        png=base64.b64encode(image.getvalue()).decode()
        form=f'''<form method="post" action="/ResultPart2.aspx">
        <input type="radio" id="rdlistCourse_0" name="rdlistCourse" value="SSC"><label for="rdlistCourse_0">Matric</label>
        <input type="radio" id="rdlistCourse_1" name="rdlistCourse" value="HSSC"><label for="rdlistCourse_1">Inter</label>
        <select id="ddlExamType" name="ddlExamType"><option value="2">Part-II</option></select>
        <select id="ddlExamYear" name="ddlExamYear"><option value="2024">2024</option></select>
        <input id="txtFormNo" name="txtFormNo"><input id="txtCaptcha" name="txtCaptcha">
        <img id="imgCaptcha" src="data:image/png;base64,{png}"><input type="submit" id="Button1"></form>'''
        posts=[]
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                context=await browser.new_context()
                async def intercept(route):
                    if route.request.method=='POST':
                        posts.append(parse_qs(route.request.post_data))
                        await route.fulfill(content_type='text/html',body=FIXTURE.read_text())
                    else:await route.fulfill(content_type='text/html',body=form)
                await context.route('**/*',intercept);page=await context.new_page()
                with tempfile.TemporaryDirectory() as temp,patch('bise_results.runner.recognize',return_value={'text':'ABC123','confidence':99,'variants_agree':True}):
                    with self.assertRaisesRegex(ReviewRequired,'Required DOB'):
                        await lookup(page,{'roll':'500001','year':2024,'exam_type':'2','course':'HSSC'},Path(temp),Namespace(captcha_mode='auto'),Pacer(0),asyncio.Lock())
                    self.assertEqual(posts[0]['rdlistCourse'],['HSSC'])
                    self.assertEqual(posts[0]['ddlExamYear'],['2024'])
                    self.assertFalse((Path(temp)/'parsed.json').exists())
                    self.assertTrue((Path(temp)/'card.html').exists())
            finally:await browser.close()
