"""R4 online event matrix: real SQLite/backend with controllable inference."""
import copy
import threading
import unittest

import test_qq_group_analysis as group_fixture
from qq_normalize import normalize_export
import test_qq_groups as groups


class OnlineAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.fixture = group_fixture.GroupAnalysisTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend = self.fixture.backend
        self.account = self.fixture.account
        self.store = self.fixture.fixture.fixture.store
        self.source = self.fixture.fixture.source
        self.analyzer = self.fixture.fixture.analyzer
        self.group = 'g:20001'
        self.rows = normalize_export(groups.export_document(), expected_kind='group')['records']
        self.entered, self.release = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)

    def append(self, identifier, timestamp):
        row = copy.deepcopy(self.rows[-1])
        row.update(native_id=identifier, time_ms=timestamp, text='新消息：收到，明天见', raw='synthetic-online-' + identifier)
        return self.store.ingest(self.account, self.group, [row])

    def revision(self):
        return self.store.revision(self.account, self.group)[0]

    def drain(self):
        self.fixture.fixture.fixture.drain()

    def block_inference(self):
        original = self.analyzer.analyze
        def blocked(*args, **kwargs):
            self.entered.set()
            if not self.release.wait(8):
                raise RuntimeError('bounded synthetic inference timeout')
            return original(*args, **kwargs)
        self.analyzer.analyze = blocked

    def test_pure_append_keeps_revision_but_moving_latest_range_must_show_completed_overlap(self):
        before = self.revision()
        self.block_inference()
        job = self.backend.start(self.group, 'recent', 6, expected_account=self.account)
        self.assertTrue(self.entered.wait(3))
        original_ids = {row['id'] for row in self.source.messages(self.group, 6)}
        self.append('online-new', self.rows[-1]['time_ms'] + 1000)
        self.assertEqual(self.revision(), before)
        self.assertEqual(len(self.source.messages(self.group, 20)), 7, 'reading must continue during inference')
        self.release.set()
        self.drain()
        completed = next(row for row in self.backend.jobs.values() if row['id'] == job['id'])
        self.assertEqual(completed['status'], 'done')
        current_ids = {row['id'] for row in self.source.messages(self.group, 6)}
        result = self.backend.analysis(self.group, 6)
        self.assertEqual(set(result['results']), original_ids & current_ids,
                         'completed valid overlap must survive a latest-range move')

    def test_append_after_completion_preserves_visible_labels(self):
        self.backend.start(self.group, 'recent', 6, expected_account=self.account)
        self.drain()
        old = self.backend.analysis(self.group, 6)['results']
        self.assertEqual(len(old), 6)
        self.append('online-after', self.rows[-1]['time_ms'] + 1000)
        current = {row['id'] for row in self.source.messages(self.group, 6)}
        self.assertEqual(set(self.backend.analysis(self.group, 6)['results']), set(old) & current)

    def test_same_second_late_arrival_is_revision_changing(self):
        before = self.revision()
        outcome = self.append('same-second', self.rows[-1]['time_ms'])
        self.assertEqual(outcome['backfilled'], 1)
        self.assertEqual(self.revision(), before + 1)

    def test_each_completed_message_publishes_while_next_inference_is_blocked(self):
        original=self.analyzer.analyze
        calls=[]
        def blocked_second(*args,**kwargs):
            calls.append(args[2])
            if len(calls)==2:
                self.entered.set()
                if not self.release.wait(8):raise RuntimeError('bounded inference timeout')
            return original(*args,**kwargs)
        self.analyzer.analyze=blocked_second
        self.backend.start(self.group,'recent',6,expected_account=self.account)
        self.assertTrue(self.entered.wait(3))
        try:
            current=self.backend.analysis(self.group,6)
            self.assertEqual(set(current['results']),{calls[0]})
            self.assertEqual(current['job']['status'],'running')
            self.assertEqual(current['pendingCount'],5)
        finally:self.release.set()
        self.drain()
        self.assertEqual(self.backend.analysis(self.group,6)['pendingCount'],0)

    def test_same_second_arrival_during_inference_does_not_cancel_unchanged_inputs(self):
        self.block_inference()
        job=self.backend.start(self.group,'recent',6,expected_account=self.account)
        self.assertTrue(self.entered.wait(3))
        before=self.revision()
        self.append('online-same-second',self.rows[-1]['time_ms'])
        self.assertEqual(self.revision(),before+1)
        self.release.set();self.drain()
        completed=next(row for row in self.backend.jobs.values() if row['id']==job['id'])
        self.assertEqual(completed['status'],'done')
        result=self.backend.analysis(self.group,6)
        self.assertEqual(len(result['results']),5)

    def test_changed_inflight_context_retries_only_obsolete_input_then_converges(self):
        original=self.analyzer.analyze
        calls=[]
        def block_last(*args,**kwargs):
            calls.append(copy.deepcopy(args))
            if len(calls)==6:
                self.entered.set()
                if not self.release.wait(8):raise RuntimeError('bounded inference timeout')
            return original(*args,**kwargs)
        self.analyzer.analyze=block_last
        self.backend.start(self.group,'recent',6,expected_account=self.account)
        self.assertTrue(self.entered.wait(3))
        # This insertion changes the blocked target's three preceding positions.
        self.append('between-context',self.rows[-1]['time_ms']-1)
        self.release.set();self.drain()
        result=self.backend.analysis(self.group,6)
        self.assertEqual(result['job']['status'],'done')
        self.assertGreaterEqual(result['job']['recomputed'],1)
        self.assertEqual(len(calls),7,'only the obsolete in-flight target is re-inferred')
        self.assertNotEqual(calls[-2][1],calls[-1][1])
        self.backend.start(self.group,'recent',6,expected_account=self.account)
        self.drain()
        self.assertEqual(self.backend.analysis(self.group,6)['pendingCount'],0)

    def test_overlapping_latest_requests_keep_admitted_messages_until_drain(self):
        self.block_inference()
        job=self.backend.start(self.group,'recent',2,expected_account=self.account)
        self.assertTrue(self.entered.wait(3))
        admitted=set(row['id'] for row in self.source.messages(self.group,2))
        for index in range(9):
            self.append('burst-'+str(index),self.rows[-1]['time_ms']+(index+1)*1000)
            admitted.update(row['id'] for row in self.source.messages(self.group,2))
            queued=self.backend.start(self.group,'recent',2,expected_account=self.account)
            self.assertEqual(queued['id'],job['id'],'latest-range motion must not spawn a new job')
        self.release.set();self.drain()
        history=self.backend.history(self.account,self.group,limit=30)
        self.assertEqual(set(history['results']),admitted,'accepted older targets must not fall out of the moving range')
        self.assertEqual(self.backend.analysis(self.group,2)['job']['status'],'done')

    def test_failure_keeps_committed_verified_prefix_and_retry_resumes(self):
        original=self.analyzer.analyze
        calls=[]
        def fail_second(*args,**kwargs):
            calls.append(args[2])
            if len(calls)==2:raise RuntimeError('synthetic ordinary model failure')
            return original(*args,**kwargs)
        self.analyzer.analyze=fail_second
        self.backend.start(self.group,'recent',6,expected_account=self.account);self.drain()
        failed=self.backend.analysis(self.group,6)
        self.assertEqual(failed['job']['status'],'error')
        self.assertEqual(len(failed['results']),1)
        self.analyzer.analyze=original
        self.backend.start(self.group,'recent',6,expected_account=self.account);self.drain()
        self.assertEqual(self.backend.analysis(self.group,6)['pendingCount'],0)

    def test_backfill_that_invalidates_completed_prefix_rechecks_it_without_another_request(self):
        original=self.analyzer.analyze
        calls=[]
        def blocked_last(*args,**kwargs):
            calls.append(args[2])
            if len(calls)==6:
                self.entered.set()
                if not self.release.wait(8):raise RuntimeError('bounded inference timeout')
            return original(*args,**kwargs)
        self.analyzer.analyze=blocked_last
        admitted={row['id'] for row in self.source.messages(self.group,6)}
        self.backend.start(self.group,'recent',6,expected_account=self.account)
        self.assertTrue(self.entered.wait(3))
        self.append('prefix-late',self.rows[0]['time_ms']+1)
        self.release.set();self.drain()
        history=self.backend.history(self.account,self.group,limit=20)
        self.assertEqual(set(history['results']),admitted,'completed but invalidated accepted targets must converge automatically')
        self.assertEqual(len(calls),9,'only the three changed earlier neighborhoods should repeat')

    def test_historical_backfill_is_revision_changing(self):
        before = self.revision()
        outcome = self.append('historical', self.rows[0]['time_ms'] - 1000)
        self.assertEqual(outcome['backfilled'], 1)
        self.assertEqual(self.revision(), before + 1)

    def test_content_conflict_and_recall_invalidate_published_results(self):
        for change in ('conflict', 'recall'):
            with self.subTest(change=change):
                self.backend.start(self.group, 'recent', 6, expected_account=self.account)
                self.drain()
                before = self.revision()
                row = copy.deepcopy(self.rows[0 if change == 'conflict' else 1])
                if change == 'conflict':
                    row['text'] = '竞争内容版本'
                    row['raw'] = 'synthetic-conflict'
                else:
                    row.update(status='recalled', recall_time='1700000100')
                self.store.ingest(self.account, self.group, [row])
                self.assertEqual(self.revision(), before + 1)
                result=self.backend.analysis(self.group, 6)
                self.assertNotIn(row['native_id'],result['results'],
                                 'changed/recalled input must not look current')
                self.assertGreater(result.get('pendingCount',0),0,
                                   'invalidated dependent labels need explicit pending status')


if __name__ == '__main__':
    unittest.main()
