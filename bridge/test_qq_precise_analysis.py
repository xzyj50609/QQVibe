"""Exact QQ selected/rerun targets, publication and late-writer acceptance."""
import copy
import unittest
from unittest.mock import patch

from backend_contracts import LOCAL_SOURCE_ID, api_insight_scope
from qq_analysis_store import StaleAnalysisError
from qq_single_analysis import stream_version
import test_qq_targets as targets_fixture
import test_qq_group_analysis as group_fixture
import test_qq_api_revision as api_fixture
from test_qq_message_store import CONV


class PreciseSingleTests(unittest.TestCase):
    def setUp(self):
        self.fixture=targets_fixture.TargetTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend,self.account=self.fixture.backend,self.fixture.account
        self.source,self.store=self.fixture.source,self.fixture.base
        self.calls=[]
        original=self.fixture.analyzer.analyze
        def analyze(session,context,target,**kwargs):
            self.calls.append(copy.deepcopy((target,context)))
            return original(session,context,target,**kwargs)
        self.fixture.analyzer.analyze=analyze

    def execute(self,ids,*,force=False,limit=1):
        job=self.backend.start(CONV,'message' if force else 'recent',max(limit,len(ids)),
            expected_account=self.account,**({'message_id':ids[0]} if force else {'target_ids':ids}))
        self.fixture.fixture.drain()
        saved=self.backend.analysis_job(job['id'])['job']
        self.assertEqual(saved['status'],'done',saved)
        return saved

    def labels(self,ids):
        return self.backend.analysis_labels(self.account,CONV,ids)['results']

    def test_selected_self_and_old_peer_are_exact_and_do_not_queue_portrait(self):
        self.execute(['t0','t3'])
        self.assertEqual({target for target,_ in self.calls},{'t0','t3'})
        self.assertEqual(set(self.labels(['t0','t1','t2','t3','t4'])),{'t0','t3'})
        self.assertEqual(self.fixture.analyzer.calls,[],'selected labels must not start whole-history portrait work')
        for target,context in self.calls:
            self.assertEqual(context[-1]['id'],target)

    def test_single_rerun_overwrites_only_that_label_and_keeps_portrait(self):
        self.fixture.profile()
        before=self.backend.batch_engine.snapshot(self.account,CONV,self.fixture.analyzer.analysis_version(),self.store)
        self.execute(['t0','t3'])
        labels=self.labels(['t0','t3'])
        original=self.fixture.analyzer.analyze
        def changed(*args,**kwargs):
            result=original(*args,**kwargs)
            result.update(intent=[{'label':'确认','probability':1}],intentLabel='确认')
            return result
        self.fixture.analyzer.analyze=changed
        self.execute(['t3'],force=True)
        after=self.labels(['t0','t3'])
        self.assertEqual(after['t0'],labels['t0'])
        self.assertEqual(after['t3']['intentLabel'],'确认')
        self.assertEqual([target for target,_ in self.calls].count('t3'),2)
        self.assertEqual(self.backend.batch_engine.snapshot(self.account,CONV,
            self.fixture.analyzer.analysis_version(),self.store),before)

    def test_context_edit_hides_only_affected_label_then_exact_rerun_restores_it(self):
        self.execute(['t0','t3'])
        row=copy.deepcopy(self.fixture.rows[2]);row['text']='修改过的本人背景'
        self.fixture.fixture.ingest(row)
        self.assertEqual(set(self.labels(['t0','t3'])),{'t0'})
        self.execute(['t3'],force=True)
        self.assertEqual(set(self.labels(['t0','t3'])),{'t0','t3'})
        self.assertEqual(self.fixture.analyzer.calls,[])

    def test_reset_revokes_old_label_stage_and_reopen_cannot_resurrect_it(self):
        self.execute(['t0'])
        _,workdir,_=self.backend._scoped_identity()
        repository=self.backend._qq_analysis_store(self.account,workdir,self.store)
        version=stream_version(self.fixture.analyzer.analysis_version())
        stage=repository.bind_labels(CONV,version,self.fixture.analyzer.analysis_version(),is_group=False)
        self.backend.analysis_scope_clear(self.account,CONV,LOCAL_SOURCE_ID,'conversation')
        with self.assertRaises(StaleAnalysisError):stage.publish()
        self.assertEqual(self.labels(['t0']),{})
        self.execute(['t0'],force=True)
        self.assertEqual(set(self.labels(['t0'])),{'t0'})


class PreciseGroupTests(unittest.TestCase):
    def setUp(self):
        self.fixture=group_fixture.GroupAnalysisTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_exact_one_member_message_and_single_rerun_keep_other_members_out(self):
        backend=self.fixture.backend;account=self.fixture.account
        window=backend.source.messages('g:20001',20)
        target=window[0]['id'];calls=[];original=self.fixture.fixture.analyzer.analyze
        def analyze(session,context,message,**kwargs):
            calls.append(copy.deepcopy((message,context)))
            return original(session,context,message,**kwargs)
        self.fixture.fixture.analyzer.analyze=analyze
        for force in (False,True):
            job=backend.start('g:20001','message' if force else 'recent',1,expected_account=account,
                **({'message_id':target} if force else {'target_ids':[target]}))
            self.fixture.fixture.fixture.drain()
            self.assertEqual(backend.analysis_job(job['id'])['job']['status'],'done')
        self.assertEqual([row[0] for row in calls],[target,target])
        from qq_roles import group_context
        self.assertTrue(all(entry.get('conversationKind')=='group' and group_context(entry)['speaker']
            for _,context in calls for entry in context))
        saved=backend.analysis_labels(account,'g:20001',[row['id'] for row in window])['results']
        self.assertEqual(set(saved),{target})


class ForcedApiWriterTests(unittest.TestCase):
    def setUp(self):
        self.fixture=api_fixture.ApiRevisionTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_new_single_rerun_owns_commit_even_when_old_batch_returns_first(self):
        f=self.fixture;b=f.backend;a=f.analyzer
        original=a.model_insights
        def insights(*args,**kwargs):
            reply=original(*args,**kwargs)
            if args[-1]==['late1']:
                for item in reply['insights']:item['emotion']='平静'
            return reply
        a.model_insights=insights
        a.block_next='insights'
        b.start_model_insights(f.account,CONV,20)
        self.assertTrue(a.gate_entered.wait(3))
        newer=b.start_model_insights(f.account,CONV,20,['late1'],force=True)['job']
        self.assertEqual(b.api_insight_writers[(f.account,CONV,f.source_id,'late1')],newer['id'])
        a.gate_release.set();f.idle()
        self.assertEqual(b.analysis_job(newer['id'])['job']['status'],'done')
        values=f.results.api_insight_view(f.account,CONV,api_insight_scope(f.source_id))
        self.assertEqual(values['late1']['affect']['feeling'],'平静')
        self.assertEqual(values['late2']['affect']['feeling'],'期待')

    def test_partial_stream_with_invalid_final_ids_cannot_commit_or_replace_saved_result(self):
        f=self.fixture;before=f.insights()['results']
        def invalid(protocol,url,key,model,messages,ids,on_delta=None):
            if on_delta:on_delta('编号：late1\n情感：平静\n意图：确认\n')
            return {'insights':[{'id':'foreign-id','status':'ok','emotion':'平静','intent':'确认'}]}
        f.analyzer.model_insights=invalid
        with patch('backend_service.API_INSIGHT_RETRY_SECONDS',0):
            job=f.backend.start_model_insights(f.account,CONV,20,['late1'],force=True)['job']
            f.idle()
        self.assertEqual(f.backend.analysis_job(job['id'])['job']['status'],'error')
        self.assertEqual(f.backend.model_insights(CONV)['results'],before)


if __name__=='__main__':unittest.main()
