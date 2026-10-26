"""Collection must yield to other applications without losing queued results."""
import asyncio
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

from bise_results.resources import CollectionResources,available_memory_mb,configure_ocr_children
from test_database_only import DatabaseOnlyCollectionTests


class ResourceAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_low_ram_waits_then_resumes_without_blocking_peer_activity(self):
        events=[];free=[1000];peer_ticks=0
        resources=CollectionResources(memory=lambda:free[0],emit=lambda **v:events.append(v),poll_seconds=.001)
        waiting=asyncio.create_task(resources.wait_for_memory())
        for _ in range(5):
            await asyncio.sleep(.001);peer_ticks+=1
        self.assertFalse(waiting.done());self.assertEqual(peer_ticks,5)
        free[0]=4096
        self.assertTrue(await asyncio.wait_for(waiting,.3))
        self.assertEqual([e['phase'] for e in events],['resource_wait','resource_resumed'])

    async def test_operator_cancel_during_resource_wait_is_immediate(self):
        resources=CollectionResources(memory=lambda:0,poll_seconds=60)
        waiting=asyncio.create_task(resources.wait_for_memory())
        await asyncio.sleep(0);waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(waiting,.3)

    async def test_empty_queue_does_not_wait_for_memory(self):
        resources=CollectionResources(memory=lambda:(_ for _ in ()).throw(AssertionError('No remaining work')))
        self.assertFalse(await resources.wait_for_memory(needed=lambda:False))

    async def test_many_browser_workers_do_not_spawn_unbounded_ocr(self):
        resources=CollectionResources(ocr_workers=2);active=peak=done=0
        async def worker():
            nonlocal active,peak,done
            async with resources.ocr:
                active+=1;peak=max(peak,active)
                await asyncio.sleep(.001)
                active-=1;done+=1
        await asyncio.gather(*(worker() for _ in range(18)))
        self.assertEqual(peak,2);self.assertEqual(done,18)

    def test_linux_inventory_is_available_ram_in_mib_and_unknown_is_not_false_oom(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'meminfo';path.write_text('MemTotal: 16000 kB\nMemAvailable: 2048000 kB\n')
            self.assertEqual(available_memory_mb(path),2000)
            path.write_text('broken');self.assertIsNone(available_memory_mb(path))

    def test_ocr_environment_is_local_and_other_environment_is_preserved(self):
        with patch.dict(os.environ,{'OMP_THREAD_LIMIT':'16','OMP_NUM_THREADS':'16','TEST_KEEP':'yes'}):
            configure_ocr_children()
            self.assertEqual(os.environ['OMP_THREAD_LIMIT'],'1')
            self.assertEqual(os.environ['OMP_NUM_THREADS'],'1')
            self.assertEqual(os.environ['TEST_KEEP'],'yes')

    def test_ocr_keeps_existing_vote_result_with_finite_low_priority_subprocesses(self):
        from bise_results.captcha import recognize
        image=io.BytesIO();Image.new('RGB',(150,50),'white').save(image,format='PNG')
        with patch('bise_results.captcha.pytesseract.image_to_data',return_value={'text':['SAMPLE'],'conf':[99]}) as engine:
            result=recognize(image.getvalue())
        self.assertEqual(result,{'text':'SAMPLE','confidence':99.0,'variants_agree':True})
        self.assertEqual(engine.call_count,2)
        self.assertTrue(all(call.kwargs['timeout']==20 and call.kwargs['nice']==10 for call in engine.call_args_list))


class CollectionMemoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    exercise=DatabaseOnlyCollectionTests.exercise

    async def test_pending_rolls_survive_memory_wait_then_commit_without_excel(self):
        original=CollectionResources;samples=[]
        def memory():
            samples.append(1);return 1024 if len(samples)<5 else 8192
        def limited(*args,**kwargs):
            return original(*args,**kwargs,memory=memory,poll_seconds=.001)
        with patch('bise_results.runner.CollectionResources',side_effect=limited):
            await self.exercise()
        self.assertGreaterEqual(len(samples),6)
