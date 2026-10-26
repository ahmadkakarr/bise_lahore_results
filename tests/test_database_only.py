"""Database-only collection and coherent exam-scoped normalization without Excel."""
import asyncio
from argparse import Namespace
from contextlib import redirect_stdout
from copy import deepcopy
import io,json,sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,AsyncMock,MagicMock
from types import SimpleNamespace

from bise_normalize.sources import snapshots,table,sqlite_table
from bise_normalize.app import execute
from bise_normalize.config import initialize,load
from bise_results.runner import collect
from bise_results.store import locked_store,jobs_for
from bise_results.parser import parse_result
from test_identity_fields import FIXTURE,receipt_for


class DatabaseOnlyCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self,interrupt=False,low_disk=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'rolls.csv';source.write_text('Roll Numbers\n001234\n001235\n')
            args=Namespace(csv=source,school='DB only',year=2026,grade=9,database=root/'results.sqlite3',retry_unresolved=False,limit=0,workers=1,interval=3,captcha_mode='auto',artifacts=root/'artifacts',output=root/'output',browser_executable=None,headless=True)
            ctx=SimpleNamespace(new_page=AsyncMock(return_value=MagicMock()),close=AsyncMock())
            browser=SimpleNamespace(new_context=AsyncMock(return_value=ctx),close=AsyncMock())
            manager=MagicMock();manager.__aenter__=AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))));manager.__aexit__=AsyncMock(return_value=None)
            started=asyncio.Event();calls=[]
            async def lookup(page,job,folder,*a,**kw):
                calls.append(job['roll'])
                if interrupt and job['roll']=='001235':started.set();await asyncio.Event().wait()
                html=FIXTURE.with_name('ssc-part1.html').read_text().replace('001234',job['roll'])
                parsed=parse_result(html,job['roll'],exam_type='1')
                return parsed,receipt_for(folder,html,parsed)
            with patch('bise_results.runner.async_playwright',return_value=manager),patch('bise_results.runner.lookup',side_effect=lookup),patch('bise_results.reports.export_batch',side_effect=AssertionError('Default collect must never export')),patch('bise_results.runner.shutil.disk_usage',return_value=SimpleNamespace(free=0 if low_disk else 10*1024**3)),redirect_stdout(io.StringIO()) as output:
                if interrupt:
                    task=asyncio.create_task(collect(args));await asyncio.wait_for(started.wait(),2);task.cancel();self.assertEqual(await task,130)
                else:self.assertEqual(await collect(args),2 if low_disk else 0)
            self.assertFalse((root/'output').exists())
            self.assertIn('"reports_generated": false',output.getvalue())
            with locked_store(args.database) as db:
                rows=jobs_for(db,db.execute('SELECT id FROM batches').fetchone()[0])
                self.assertEqual([r['state'] for r in rows],['pending','pending'] if low_disk else ['verified','pending'] if interrupt else ['verified','verified'])
            if low_disk:
                self.assertFalse((root/'artifacts').exists());self.assertFalse(calls)
            else:self.assertTrue((root/'artifacts').exists())
    async def test_default_collect_never_creates_excel_or_archives(self):await self.exercise()
    async def test_ctrl_c_commits_data_and_returns_without_report_generation(self):await self.exercise(interrupt=True)
    async def test_low_disk_stops_before_browser_and_preserves_pending_rolls(self):await self.exercise(low_disk=True)


class SqliteNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.path=self.root/'source.sqlite3';self.db=sqlite3.connect(self.path);self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE jobs(roll TEXT,year INTEGER,course TEXT,exam_type TEXT,state TEXT,result_json TEXT)')
        self.settings={'format':'sqlite','sheet':None,'header_row':1,'cell_range':None,'exam':{'board':'BISE Lahore','year':2026,'course':'SSC','part':'I','session':'Annual'}}
        result=parse_result(FIXTURE.with_name('ssc-part1.html').read_text(),'001234',exam_type='1')
        self.result=result
        for part,name,state in [('1','Ninth Student','verified'),('2','Tenth Student','verified'),('1','Pending Student','pending')]:
            record={**result,'name':name,'exam_type':part}
            self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',('001234' if state=='verified' else '001235',2026,'SSC',part,state,json.dumps(record)))
        self.db.commit()
    def capture(self):
        with snapshots({'source':self.path},source_settings=self.settings) as data:
            path,digest=data['source'];rows=list(table(path,self.settings))
            return digest,rows
    def test_committed_wal_rows_are_scoped_by_class_and_exclude_pending(self):
        digest,rows=self.capture();self.assertEqual(len(rows),2)
        self.assertEqual(rows[1][1]['Student Name'],'Ninth Student')
        self.assertEqual(rows[1][1]['Collection Status'],'Saved')
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0],3)
    def test_snapshot_is_coherent_while_writer_commits(self):
        rows=sqlite_table(self.path,self.settings);next(rows)
        changed={**self.result,'name':'Updated Ninth'}
        self.db.execute("UPDATE jobs SET result_json=? WHERE exam_type='1' AND state='verified'",(json.dumps(changed),));self.db.commit()
        self.assertEqual(next(rows)[1]['Student Name'],'Ninth Student');rows.close()
        self.assertEqual(self.capture()[1][1][1]['Student Name'],'Updated Ninth')
    def test_unrelated_queue_changes_do_not_change_snapshot_hash(self):
        before,_=self.capture()
        self.db.execute("UPDATE jobs SET state='failed' WHERE state='pending'");self.db.commit()
        after,_=self.capture();self.assertEqual(before,after)
    def test_normalization_reads_sqlite_and_publishes_csv_without_excel_idempotently(self):
        profile=self.root/'profiles/ninth.json';output=self.root/'normalized'
        initialize(profile,'Ninth',self.path,output=output)
        config=load(profile);config['source'].update(self.settings);config['source']['path']='../source.sqlite3'
        config['selection']['institutions']=[]
        profile.write_text(json.dumps(config))
        result=execute(profile)
        self.assertEqual(result['summary']['normalized'],1)
        self.assertTrue((output/'Normalized.csv').exists())
        self.assertFalse(list(output.rglob('*.xlsx')))
        self.assertEqual(execute(profile)['status'],'already_up_to_date')
