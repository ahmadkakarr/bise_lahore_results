"""9th/10th selectors, exam-scoped cache, legacy migration and separate masters."""
from argparse import Namespace
import asyncio
import base64
from contextlib import redirect_stdout
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from urllib.parse import parse_qs,urlsplit
from PIL import Image
from playwright.async_api import async_playwright

from bise_results.importer import audit
from bise_results.parser import parse_result, ReviewRequired
from bise_results.store import locked_store,import_batch,jobs_for,prepare_jobs,reparse_cached_results
from bise_results.reports import export_batch,master_name
from bise_results.runner import lookup,Pacer
from bise_results.__main__ import main
from test_identity_fields import receipt_for

FIXTURE=Path(__file__).parent/'fixtures/ssc-part1.html'
PART2=Path(__file__).parent/'fixtures/ssc-part2.html'


class ClassCollectionTests(unittest.TestCase):
    def test_part1_identity_subjects_and_totals_match_new_layout(self):
        result=parse_result(FIXTURE.read_text(),'001234',exam_type='1')
        self.assertEqual((result['exam_type'],result['obtained'],result['maximum'],len(result['subjects'])),('1',400,555,8))
        self.assertEqual(result['data_completeness'],'complete')
        self.assertEqual(result['student_cnic'],'00000-0123456-0')
        self.assertEqual(result['father_cnic'],'00000-0765432-0')
        self.assertEqual(result['date_of_birth'],'09/05/2010')
        self.assertEqual(result['subjects'][0]['theory_i_raw'],'45')
        self.assertEqual(result['maximum_source'],'sum_of_subject_maxima')
        self.assertEqual(result['result_status'],'PASS')

    def test_wrong_class_or_year_cannot_be_accepted_as_requested_exam(self):
        for source,part in [(FIXTURE.read_text(),'2'),(PART2.read_text(),'1'),(FIXTURE.read_text().replace('2026','2025'),'1')]:
            with self.assertRaises(ReviewRequired):parse_result(source,'001234',exam_type=part)

    def test_part1_partial_or_changed_marks_keep_identity_and_raw_values(self):
        changed=FIXTURE.read_text().replace('NAME OF SUBJECT','Changed subject header')
        result=parse_result(changed,'001234',exam_type='1')
        self.assertEqual(result['obtained'],400)  # Explicit footer total remains valid.
        self.assertEqual(result['data_completeness'],'partial')
        self.assertIsNone(result['maximum'])
        for source in [FIXTURE.read_text().replace('PASS 400','ABSENT'),FIXTURE.read_text().replace('PASS 400','PASS 450')]:
            result=parse_result(source,'001234',exam_type='1')
            self.assertEqual(result['name'],'Sample Candidate')
            self.assertIsNone(result['obtained'])
            self.assertTrue(result['subjects_raw'])
        missing=FIXTURE.read_text().split('<table')[0]
        self.assertEqual(parse_result(missing,'001234',exam_type='1')['data_completeness'],'identity_only')

    def test_both_headers_are_accepted_without_modifying_input(self):
        with tempfile.TemporaryDirectory() as temp:
            for header in ('Roll Number','Roll Numbers'):
                p=Path(temp)/'rolls.csv';p.write_text(header+'\n001234\n001234\n')
                before=p.read_bytes();r=audit(p)
                self.assertEqual((r['unique_rolls'],r['duplicate_records']),(1,1))
                self.assertEqual(p.read_bytes(),before)
            p.write_text('Roll Number,Extra\n001234,x\n')
            with self.assertRaises(ValueError):audit(p)

    def test_same_roll_is_independent_by_class_and_exports_never_mix(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'rolls.csv';source.write_text('Roll Numbers\n001234\n')
            with locked_store(root/'results.sqlite3') as db:
                batches=[]
                for part,fixture in [('2',PART2),('1',FIXTURE)]:
                    batch=import_batch(db,source,'Institution-wise',exam_type=part);batches.append(batch)
                    self.assertEqual(import_batch(db,source,'Institution-wise',exam_type=part),batch)
                    job=jobs_for(db,batch)[0];parsed=parse_result(fixture.read_text(),'001234',exam_type=part)
                    folder=root/part;folder.mkdir();receipt=receipt_for(folder,fixture.read_text(),parsed)
                    with db:db.execute("update jobs set state='verified',result_json=?,receipt_json=? where id=?",(json.dumps(parsed),json.dumps(receipt),job['id']))
                    self.assertEqual(prepare_jobs(db,batch),[])
                    self.assertEqual(reparse_cached_results(db,batch)['already_current'],1)
                self.assertNotEqual(*batches)
                self.assertEqual(db.execute('select count(*) from jobs').fetchone()[0],2)
                self.assertNotEqual(jobs_for(db,batches[0])[0]['id'],jobs_for(db,batches[1])[0]['id'])
                for part,batch in zip(('2','1'),batches):
                    output,summary=export_batch(db,batch,root/'output',include_pending=False)
                    self.assertEqual(summary,{'verified':1,'total':1})
                    manifest=json.loads((output/'Data/manifest.json').read_text())
                    self.assertEqual(manifest['exam_type'],part)
                    self.assertEqual(manifest['included_batches'],[batch])
                    records=json.loads((output/'Data/results.json').read_text())
                    self.assertEqual(records[0]['exam_type'],part)
                    self.assertEqual(output.name,f"Matric 2026 - Part-{'I' if part=='1' else 'II'} Annual")
                    master=output/(master_name(2026,part)+'.csv');mtime=master.stat().st_mtime_ns
                    self.assertEqual(export_batch(db,batch,root/'output',include_pending=False)[0],output)
                    self.assertEqual(master.stat().st_mtime_ns,mtime)

    def test_legacy_six_column_batch_schema_migrates_without_changing_part2_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);database=root/'legacy.sqlite3';source=root/'rolls.csv';source.write_text('Roll Numbers\n001234\n')
            sha=hashlib.sha256(source.read_bytes()).hexdigest()
            expected=hashlib.sha256(json.dumps(['Institution-wise',2026,'SSC','2',sha]).encode()).hexdigest()[:16]
            db=sqlite3.connect(database)
            db.execute('create table batches(id text primary key,school text,input_sha256 text,year integer,source_rows integer,created_at text)')
            db.execute('insert into batches values(?,?,?,?,?,?)',(expected,'Institution-wise',sha,2026,1,'original-time'));db.commit();db.close()
            with locked_store(database) as db:
                self.assertEqual(import_batch(db,source,'Institution-wise'),expected)
                row=db.execute('select * from batches where id=?',(expected,)).fetchone()
                self.assertEqual((row['course'],row['exam_type'],row['created_at']),('SSC','2','original-time'))
                self.assertEqual(db.execute('select count(*) from batches').fetchone()[0],1)

    def test_cli_class_is_passed_to_collector(self):
        for grade in (9,10):
            runner=AsyncMock(return_value=0)
            with patch.object(sys,'argv',['bise_results','collect','rolls.csv','--class',str(grade),'--headless','--captcha-mode','auto']),patch('bise_results.runner.collect',runner):
                self.assertEqual(main(),0)
            self.assertEqual(runner.call_args.args[0].grade,grade)


class Part1BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_posts_part1_selector_and_accepts_only_part1_endpoint(self):
        image=BytesIO();Image.new('RGB',(150,50),'white').save(image,format='PNG')
        png=base64.b64encode(image.getvalue()).decode()
        form=f'''<form method="post" action="/RollSlip.aspx"><input type="radio" id="rdlistCourse_0" name="rdlistCourse" value="SSC"><label for="rdlistCourse_0">Matric</label>
        <select id="ddlExamType" name="ddlExamType"><option value="2">Part-II</option><option value="1">Part-I</option></select><select id="ddlExamYear" name="ddlExamYear"><option value="2026">2026</option></select>
        <input id="txtFormNo" name="txtFormNo"><input id="txtCaptcha" name="txtCaptcha"><img id="imgCaptcha" src="data:image/png;base64,{png}"><input type="submit" id="Button1"></form>'''
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
                    result,receipt=await lookup(page,{'roll':'001234','year':2026,'exam_type':'1'},Path(temp),Namespace(captcha_mode='auto'),Pacer(0),asyncio.Lock())
                    self.assertEqual(result['exam_type'],'1')
                    self.assertEqual(posts[0]['ddlExamType'],['1'])
                    self.assertEqual(len(posts),1)
                    self.assertTrue(Path(receipt['parsed.json']['path']).is_file())
            finally:await browser.close()
