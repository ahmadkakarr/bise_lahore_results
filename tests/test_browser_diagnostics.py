"""Diagnostic contracts; browser integration is operator-run separately."""

import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from youthgames_register.browser_evidence import PageEvidence
from youthgames_register import browser_run
from tests.test_youthgames import config


class PageEvidenceTests(unittest.TestCase):
    def test_capture_only_main_document_status_and_public_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            frame=object();listeners={};events=[]
            page=SimpleNamespace(main_frame=frame,on=lambda name,fn:listeners.__setitem__(name,fn),
                remove_listener=lambda name,fn:listeners.pop(name,None))
            evidence=PageEvidence(page,lambda phase,**values:events.append({'phase':phase,**values}),{'worker':2,'job':12})
            request=SimpleNamespace(url='https://youthgames.punjab.gov.pk/applicant?token=PRIVATE',
                method='POST',resource_type='document',frame=frame)
            listeners['request'](request)
            listeners['response'](SimpleNamespace(request=request,status=429,
                headers={'retry-after':'45','set-cookie':'PRIVATE','x-ratelimit-limit':'5'}))
            evidence.close(folder);evidence.close()
            text=Path(folder,'browser-network.json').read_text()
            self.assertNotIn('PRIVATE',text);self.assertNotIn('set-cookie',text)
            result=json.loads(text)['events'][0]
            self.assertEqual(result['path'],'/applicant');self.assertEqual(result['status'],429)
            self.assertEqual(result['rate_headers'],{'retry-after':'45','x-ratelimit-limit':'5'})
            self.assertEqual(events[0]['worker'],2);self.assertFalse(listeners)


class DiagnosticRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_twenty_worker_options_and_durable_summary_without_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=config();settings['state'].update(database=str(Path(folder,'jobs.sqlite3')),output=str(Path(folder,'output')))
            async def fake(*args,**kwargs):
                self.assertEqual(kwargs['workers'],20);self.assertTrue(kwargs['headless'])
                emit=kwargs['emit'];emit('queue',total=2)
                for worker in (1,2):emit('awaiting_response',worker=worker,job=worker)
                for worker in (1,2):
                    emit('saving_result',worker=worker,job=worker)
                    emit('result',worker=worker,job=worker,state='registered',tracking='SYNTHETIC-'+str(worker))
                return {'outcomes':[{'state':'registered'},{'state':'registered'}]}
            with patch.object(browser_run,'load',return_value=settings),patch.object(browser_run.app,'run',side_effect=fake),redirect_stdout(io.StringIO()):
                result=await browser_run.run(Path(folder,'profile.json'),workers=20,headless=True,captcha_mode='auto',limit=2)
            self.assertEqual(result['registered_now'],2);self.assertEqual(result['peak_in_flight'],2)
            self.assertFalse(result['global_dispatch_gate']);self.assertEqual(result['submission_spacing'],'per_worker')
            self.assertEqual(json.loads(Path(result['report']).read_text())['workers'],20)
            self.assertIn('awaiting_response',Path(result['log']).read_text())

    async def test_throttle_is_distinguished_from_http_429_in_report(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=config();settings['state']['output']=str(Path(folder,'output'))
            async def fake(*args,**kwargs):
                emit=kwargs['emit'];emit('queue',total=1)
                emit('browser_response',status=429,method='POST',path='/applicant',rate_headers={'retry-after':'48'})
                emit('browser_throttle',message='Too many registration attempts. Please wait 48 seconds and try again.')
                kwargs['control'].reason='server_rate_limit_detected';kwargs['control'].stopped.set()
                return {'outcomes':[{'state':'rejected','rate_limited':True}]}
            with patch.object(browser_run,'load',return_value=settings),patch.object(browser_run.app,'run',side_effect=fake),redirect_stdout(io.StringIO()):
                result=await browser_run.run(Path(folder,'profile.json'),workers=18,headless=True,captcha_mode='auto')
            self.assertEqual(result['status'],'stopped');self.assertEqual(result['blocked'],'server_rate_limit_detected')
            self.assertEqual(result['diagnostics']['http_429_responses'],1)
            self.assertEqual(result['diagnostics']['form_throttle_refusals'],1)

    async def test_cli_selects_default_diagnostic_mode_with_thirty_six_workers(self):
        import sys
        from youthgames_register.__main__ import main
        fake=AsyncMock(return_value={'status':'complete','outcomes':[]})
        args=['youthgames_register','submit','--config','profile.json','--workers','36','--headless',
              '--captcha-mode','auto','--limit','0']
        with patch.object(sys,'argv',args),patch('youthgames_register.browser_run.run',fake), \
             patch('youthgames_register.__main__.load',return_value={'state':{'output':'/tmp/reports'}}),redirect_stdout(io.StringIO()):
            self.assertEqual(await asyncio.to_thread(main),0)
        self.assertEqual(fake.call_args.kwargs['workers'],36)
        self.assertTrue(fake.call_args.kwargs['headless'])

    async def test_external_registration_is_a_completed_skip_not_a_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=config();settings['state']['output']=str(Path(folder,'output'))
            fake=AsyncMock(return_value={'outcomes':[{'state':'already_registered_external'}]})
            with patch.object(browser_run,'load',return_value=settings),patch.object(browser_run.app,'run',fake),redirect_stdout(io.StringIO()):
                result=await browser_run.run(Path(folder,'profile.json'),workers=1,headless=True,captcha_mode='auto')
            self.assertEqual(result['status'],'complete');self.assertNotIn('blocked',result)
            self.assertEqual(result['already_registered_now'],1)
            self.assertEqual(result['registered_now'],0)

    async def test_no_debug_switch_uses_plain_runner(self):
        import sys
        from youthgames_register.__main__ import main
        fake=AsyncMock(return_value={'outcomes':[]})
        args=['youthgames_register','submit','--config','profile.json','--workers','36','--headless','--no-debug']
        with patch.object(sys,'argv',args),patch('youthgames_register.__main__.run',fake), \
             patch('youthgames_register.__main__.load',return_value={'state':{'output':'/tmp/reports'}}),redirect_stdout(io.StringIO()):
            self.assertEqual(await asyncio.to_thread(main),0)
        self.assertEqual(fake.call_args.kwargs['workers'],36)
