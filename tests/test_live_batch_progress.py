"""No live submissions: current batch counts, retries and persistent UI snapshots."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock,patch

from rich.console import Console
from bise_allocate import app
from youthgames_register.progress import BatchProgress,atomic_current
from youthgames_register import http_async as single
from youthgames_register.terminal import Dashboard,print_summary
from youthgames_register.store import opened as registrations
from tests import test_allocations as allocation_fixture
from tests import test_http_connections as network_fixture


def seed():
    return {'total':18925,'completed':6261,'remaining':12658,'held':6,
        'jobs':{str(i):[{'bucket':'remaining','eligible':True,'rows':1}] for i in range(1,12659)}}


class BatchStateTests(unittest.TestCase):
    def test_render_thread_always_observes_a_balanced_consistent_snapshot(self):
        model=BatchProgress();ready=Event()
        def render():
            ready.wait()
            for _ in range(10000):
                current=model.summary()
                if current:
                    self.assertEqual(current['total'],current['completed']+current['remaining']+current['held'])
                    self.assertEqual(current['completed_this_run'],current['completed']-current['completed_before_run'])
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading=pool.submit(render)
            model.seed(seed())
            ready.set()
            for job in range(1,1001):model.transition(job,'registered')
            reading.result()

    def test_original_confusion_is_resolved_by_decreasing_current_remaining(self):
        model=BatchProgress();values=seed();values['completed']=6255;values['remaining']=12664
        values['jobs'].update({str(i):[{'bucket':'remaining','eligible':True,'rows':1}] for i in range(12659,12665)})
        model.seed(values)
        for job in range(1,4778):model.transition(job,'registered')
        for job in range(4778,4811):model.transition(job,'already_registered_external')
        current=model.summary()
        self.assertEqual(current['remaining'],7854);self.assertEqual(current['completed_this_run'],4810)
        self.assertEqual(current['completed'],11065)
        self.assertEqual(current['total'],current['completed']+current['remaining']+current['held'])
        model.transition(1,'registered');model.transition(1,'uncertain');model.transition(1,'ready')
        self.assertEqual(model.summary(),current)

    def test_throttling_uncertain_retry_quarantine_and_deferred_ready_balance(self):
        model=BatchProgress();model.seed(seed());before=model.summary()
        model.transition(1,'ready');self.assertEqual(model.summary(),before)
        model.transition(1,'uncertain');self.assertEqual(model.summary()['held'],7)
        model.transition(1,'ready');self.assertEqual(model.summary(),before)
        model.transition(1,'rejected');model.transition(1,'rejected')
        self.assertEqual(model.summary()['held'],7);self.assertEqual(model.summary()['remaining'],12657)
        model.transition(2,'registered');self.assertEqual(model.summary()['completed_this_run'],1)
        model.transition(999999,'registered');self.assertEqual(model.summary()['completed_this_run'],1)

    def test_mixed_duplicate_source_rows_remain_balanced_when_sharing_a_job(self):
        model=BatchProgress();model.seed({'total':2,'completed':0,'remaining':1,'held':1,'jobs':{'1':[
            {'bucket':'remaining','eligible':True,'rows':1},{'bucket':'held','eligible':False,'rows':1}]}})
        model.transition(1,'registered')
        self.assertEqual(model.summary()['remaining'],0);self.assertEqual(model.summary()['held'],0)
        self.assertEqual(model.summary()['completed'],2)

    def test_final_database_refresh_preserves_run_start_baseline(self):
        model=BatchProgress();model.seed(seed());model.transition(1,'registered')
        values=seed();values['completed']+=1;values['remaining']-=1;values['jobs']['1'][0]['bucket']='completed'
        model.seed(values)
        self.assertEqual(model.summary()['completed_before_run'],6261)
        self.assertEqual(model.summary()['completed_this_run'],1)
        before=model.summary()
        with self.assertRaises(ValueError):model.seed({'total':10,'completed':11,'remaining':0,'held':0})
        self.assertEqual(model.summary(),before)


class DashboardProgressTests(unittest.TestCase):
    def dashboard(self,directory=None,enabled=False):
        self.output=io.StringIO();console=Console(file=self.output,width=220,height=70,force_terminal=False)
        ui=Dashboard('Synthetic batch',2,enabled=enabled,console=console)
        if directory:ui.attach(directory)
        self.addCleanup(ui.__exit__,None,None,None)
        return ui

    def test_header_plain_logs_and_final_summary_distinguish_start_from_remaining(self):
        ui=self.dashboard(enabled=True);ui.event('queue',total=12658);ui.event('batch_snapshot',**seed())
        ui.event('result',job=1,state='registered');ui.event('result',job=2,state='already_registered_external')
        ui.console.print(ui.render());ui.event('stage',message='Still running')
        rendered=self.output.getvalue()
        self.assertIn('REMAINING 12,656',rendered);self.assertIn('Completed 6,263',rendered)
        self.assertIn('Run selected at start 12,658',rendered);self.assertIn('Completed this run 2',rendered)
        result={'status':'stopped','queued':12658};ui.finish(result)
        print_summary(result,console=ui.console)
        self.assertIn('REMAINING in batch',self.output.getvalue())

    def test_current_file_changes_on_each_commit_and_final_summary_contains_batch_totals(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)/'Runs'/'Run1';ui=self.dashboard(directory)
            ui.event('queue',total=10);ui.event('batch_snapshot',**seed())
            current=Path(folder)/'Current Progress.json'
            before=json.loads(current.read_text());self.assertEqual(before['batch']['remaining'],12658)
            ui.event('result',job=1,state='registered')
            self.assertEqual(json.loads(current.read_text())['batch']['remaining'],12657)
            self.assertEqual(json.loads((directory/'Progress.json').read_text())['batch']['completed_this_run'],1)
            result={'status':'stopped','queued':10,'report':str(directory/'Summary.json')};ui.finish(result)
            final=json.loads((directory/'Summary.json').read_text())
            self.assertEqual(final['batch_progress']['remaining'],12657)
            self.assertEqual(final['run_selected_at_start'],10)
            self.assertEqual(json.loads(current.read_text())['status'],'stopped')
            events=[json.loads(line) for line in (directory/'events.jsonl').read_text().splitlines()]
            self.assertFalse(any('jobs' in event for event in events))

    def test_older_run_cannot_replace_new_current_snapshot_and_invalid_cache_is_repaired(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'Current Progress.json'
            atomic_current(path,{'run_started_at':'2026-10-22T20:00:00Z','batch':{'remaining':10}})
            atomic_current(path,{'run_started_at':'2026-10-22T19:00:00Z','batch':{'remaining':100}})
            self.assertEqual(json.loads(path.read_text())['batch']['remaining'],10)
            path.write_text('[]');atomic_current(path,{'run_started_at':'2026-10-22T21:00:00Z'})
            self.assertEqual(json.loads(path.read_text())['run_started_at'],'2026-10-22T21:00:00Z')

    def test_monitoring_io_and_database_read_errors_do_not_stop_registration_events(self):
        with tempfile.TemporaryDirectory() as folder:
            ui=self.dashboard(Path(folder)/'Runs'/'Run1')
            ui.event('batch_snapshot',**seed())
            with patch('youthgames_register.terminal.atomic_json',side_effect=OSError('synthetic read-only progress output')):
                ui.event('result',job=1,state='registered')
            self.assertEqual(ui.counts['registered'],1);self.assertEqual(ui.batch.summary()['remaining'],12657)
            self.assertEqual(ui.progress_error,'OSError')
            ui.batch_provider=lambda:(_ for _ in ()).throw(sqlite3.OperationalError('synthetic read failure'))
            ui.refresh_batch();self.assertEqual(ui.batch_error,'OperationalError')
            self.assertEqual(ui.batch.summary()['remaining'],12657)

    def test_per_result_updates_do_not_rescan_the_database(self):
        ui=self.dashboard();ui.batch_provider=Mock(side_effect=lambda:seed())
        ui.refresh_batch()
        for job in range(1,101):ui.event('result',job=job,state='registered')
        self.assertEqual(ui.batch_provider.call_count,1)
        self.assertEqual(ui.batch.summary()['remaining'],12558)


class AllocationSnapshotTests(unittest.TestCase):
    setUp=allocation_fixture.AllocationTests.setUp
    insert_registered=allocation_fixture.AllocationTests.insert_registered

    def test_full_scope_snapshot_includes_completed_excluded_from_pinned_ready_file(self):
        app.apply(self.profile);app.normalize(self.profile)
        self.insert_registered('000001','UC No. 255, Satto Katia')
        with registrations(self.reg) as db:
            db.execute("UPDATE jobs SET tracking=tracking WHERE state='ready'")
        snapshot=app.batch_progress(self.profile)
        self.assertEqual({k:snapshot[k] for k in ('total','completed','remaining','held')},
            {'total':43,'completed':1,'remaining':42,'held':0})
        self.assertEqual(sum(part['rows'] for rows in snapshot['jobs'].values() for part in rows),1)


class TransportSnapshotTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=network_fixture.MultiConnectionTests.asyncSetUp
    write_input=network_fixture.MultiConnectionTests.write_input
    handler=network_fixture.MultiConnectionTests.handler
    factory=network_fixture.MultiConnectionTests.factory
    multi=network_fixture.MultiConnectionTests.multi

    def snapshot(self):
        db=sqlite3.connect(self.root/'jobs.sqlite3');db.row_factory=sqlite3.Row
        try:rows=db.execute('SELECT id,state FROM jobs').fetchall()
        finally:db.close()
        states=Counter(r['state'] for r in rows);done=states['registered']+states['already_registered_external']
        return {'total':10,'completed':done,'remaining':8-done,'held':2,
            'jobs':{str(r['id']):[{'rows':1,'eligible':True,'bucket':'completed' if r['state'] in ('registered','already_registered_external') else 'remaining'}] for r in rows}}

    async def test_real_multi_worker_events_keep_live_counts_and_stopped_summary_current(self):
        self.write_input(8)
        output=io.StringIO()
        with Dashboard('Synthetic',6,enabled=False,console=Console(file=output)) as ui:
            ui.batch_provider=self.snapshot
            result=await self.multi(ui=ui)
        self.assertEqual(result['registered_now'],8)
        self.assertEqual(result['batch_progress']['remaining'],0)
        self.assertEqual(result['batch_progress']['completed_this_run'],8)
        final=json.loads(Path(result['report']).read_text())
        self.assertEqual(final['batch_progress']['total'],10);self.assertEqual(final['batch_progress']['held'],2)
        current=Path(result['report']).parent.parent.parent/'Current Progress.json'
        self.assertEqual(json.loads(current.read_text())['batch']['remaining'],0)

    async def test_single_connection_stop_reconciles_remaining_and_preserves_initial_queue_count(self):
        self.write_input(8);self.sent_on=[];control=single.Control(submission_interval=.01,prepare_ahead=2,max_in_flight=2)
        with Dashboard('Synthetic stopped',4,enabled=False,console=Console(file=io.StringIO())) as ui:
            ui.batch_provider=self.snapshot
            original=ui.event
            def event(phase,**values):
                original(phase,**values)
                if phase=='result' and values.get('state')=='registered':control.stop.set()
            ui.event=event
            result=await single.run(self.profile,workers=4,ui=ui,control=control,client_factory=self.factory,install_signals=False)
        self.assertEqual(result['status'],'stopped')
        final=json.loads(Path(result['report']).read_text())
        self.assertEqual(final['run_selected_at_start'],8)
        self.assertGreater(final['batch_progress']['remaining'],0)
        self.assertEqual(final['batch_progress']['remaining'],8-final['batch_progress']['completed'])
        self.assertEqual(final['batch_progress']['completed_this_run'],result['registered_now'])
