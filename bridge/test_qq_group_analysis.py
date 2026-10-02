"""R4 real local repositories/workers, synthetic group rows and deterministic model."""
import copy
import unittest
import test_qq_targets as targets
import test_qq_groups as groups
from qq_normalize import normalize_export


def seed(fixture):
    store=fixture.fixture.store
    for code in ('20001','20002'):
        doc=groups.export_document();doc['chatInfo']['peerUid']=code
        normalized=normalize_export(doc,expected_kind='group')
        store.ensure_conversation(fixture.account,'g:'+code,kind='group',group_code=code,display_name='合成讨论群')
        store.ingest(fixture.account,'g:'+code,normalized['records'])
        store.set_members(fixture.account,'g:'+code,groups.PEOPLE)


class GroupAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.fixture=targets.TargetTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend=self.fixture.backend;self.account=self.fixture.account
        seed(self.fixture)
        self.calls=[]
        prior=self.fixture.analyzer.analyze_batch
        def batch(session,payload,context):
            self.calls.append(copy.deepcopy(payload))
            result=prior(session,payload,context)
            result['result']['score']=None
            return result
        self.fixture.analyzer.analyze_batch=batch
        fine=self.fixture.analyzer.analyze
        def labels(*args,**kwargs):
            result=fine(*args,**kwargs);result['score']=None
            return result
        self.fixture.analyzer.analyze=labels

    def profile(self,user,member):
        self.backend.profile(user,member);self.fixture.fixture.drain()
        return self.backend.profile(user,member)

    def test_group_member_aggregate_matches_individual_counts_for_uid_only_alias_and_nontext(self):
        store=self.fixture.fixture.store
        rows=normalize_export(groups.export_document(),expected_kind='group')['records']
        alias=copy.deepcopy(rows[1]);alias.update(native_id='uid-only-alias',sender_uin=None,time_ms=rows[-1]['time_ms']+1000)
        store.ingest(self.account,'g:20001',[alias])
        for user in ('g:20001','g:20002'):
            people=self.fixture.source.members(user)
            for person in people:
                expected=store.target_counts(self.account,user,uin=person['uin'],uid=person['uid'])[0]
                self.assertEqual(person['count'],expected,(user,person['id']))
        self.assertEqual(sum(person['count'] for person in self.fixture.source.members('g:20001')),7)

    def test_each_member_only_counts_own_text_and_other_speakers_remain_background(self):
        for member in ['uin:10001','uin:10002','uin:10003']:
            result=self.profile('g:20001',member)
            self.assertEqual((result['stats']['textCount'],result['stats']['analyzedCount']),(2,2),result)
            self.assertIsNone(result['affinity'])
            targets_in_call=[row for payload in self.calls for row in payload if row['target']]
            self.assertEqual({row['senderId'] for row in targets_in_call},{member})
            self.calls.clear()
        other=self.profile('g:20002','uin:10002')
        self.assertEqual(other['stats']['analyzedCount'],2)
        self.assertTrue(self.calls,'another group must infer independently')
        overview=self.backend.profile('g:20001')
        self.assertEqual(overview['stats']['participantCount'],3)
        self.assertTrue(overview['objectiveOverview'])

    def test_bounded_fine_labels_expand_scope_reuse_unchanged_inputs_and_refuse_full_group_portrait(self):
        with self.assertRaisesRegex(ValueError,'group-portrait-target-required'):
            self.backend.start('g:20001','incremental',None,expected_account=self.account)
        self.backend.start('g:20001','recent',2,expected_account=self.account);self.fixture.fixture.drain()
        first=self.backend.analysis('g:20001',2)
        self.assertEqual(len(first['results']),2,first)
        self.backend.start('g:20001','recent',6,expected_account=self.account);self.fixture.fixture.drain()
        second=self.backend.analysis('g:20001',6)
        self.assertEqual(len(second['results']),6,second)
        self.assertEqual(first['affinity'],None)
        self.assertTrue(all(value.get('score') is None for value in second['results'].values()))
        historical=self.backend.history(self.account,'g:20001',limit=20)
        self.assertEqual(set(historical['results']),set(second['results']))
        previous=len(self.fixture.analyzer.calls)
        result=self.profile('g:20001','uin:10002')
        stable=len(self.fixture.analyzer.calls)
        self.assertGreater(stable,previous)
        self.assertEqual(self.profile('g:20001','uin:10002')['stats'],result['stats'])
        self.assertEqual(len(self.fixture.analyzer.calls),stable)

    def test_successful_group_profile_and_labels_recover_in_a_fresh_backend(self):
        from backend_service import Backend
        from qq_source import QQSource
        from qq_message_store import QQMessageStore
        from product_profile import load_product
        from model_source import ModelSourceStore
        from conversation_selection import ConversationSelectionStore
        from test_qq_analysis_revision import Analyzer
        own=self.profile('g:20001','uin:10001')
        other=self.profile('g:20001','uin:10002')
        self.backend.start('g:20001','recent',6,expected_account=self.account);self.fixture.fixture.drain()
        saved=self.backend.analysis('g:20001',6)['results']
        library=self.fixture.fixture.store.path;root=self.fixture.fixture.root
        self.backend.shutdown()
        source=QQSource(uin='10001',store=QQMessageStore(library),root=root,profile=load_product('qq'))
        self.addCleanup(source.close)
        analyzer=Analyzer()
        reopened=Backend(source,analyzer=analyzer,store_factory=lambda *_:self.fixture.base,
            model_source_store=ModelSourceStore(root/'models.json',root=root),selection_store=ConversationSelectionStore(root/'selection'))
        self.addCleanup(reopened.shutdown)
        self.assertEqual(reopened.profile('g:20001','uin:10001')['stats'],own['stats'])
        self.assertEqual(reopened.profile('g:20001','uin:10002')['stats'],other['stats'])
        self.assertEqual(reopened.analysis('g:20001',6)['results'],saved)
        self.assertEqual(analyzer.calls,[])

    def test_changed_context_invalidates_group_labels_and_failed_member_rebuild_keeps_success(self):
        old=self.profile('g:20001','uin:10002')
        other=self.profile('g:20002','uin:10002')
        self.backend.start('g:20001','recent',6,expected_account=self.account);self.fixture.fixture.drain()
        old_labels=self.backend.history(self.account,'g:20001',limit=20)['results']
        doc=groups.export_document();doc['messages'][0]['content']['text']='修改后的另一成员背景'
        records=normalize_export(doc,expected_kind='group')['records']
        self.fixture.fixture.store.ingest(self.account,'g:20001',records)
        remaining=self.backend.history(self.account,'g:20001',limit=20)['results']
        affected={row['native_id'] for row in records[:4]}
        self.assertFalse(affected & set(remaining),'changed target and its dependent contexts must be hidden')
        self.assertEqual(remaining,{key:value for key,value in old_labels.items() if key not in affected},
                         'an unrelated revision must preserve independently verified labels')
        self.fixture.analyzer.fail_at=len(self.fixture.analyzer.calls)+1
        self.backend.profile('g:20001','uin:10002');self.fixture.fixture.drain()
        failed=self.backend.profile('g:20001','uin:10002')
        self.assertTrue(failed['stale']);self.assertTrue(failed['hasPublishedAnalysis'])
        self.assertEqual(failed['stats']['analyzedCount'],old['stats']['analyzedCount'])
        self.assertEqual(self.backend.profile('g:20002','uin:10002')['stats'],other['stats'])


class GroupApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture=targets.ApiTargetTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        seed(self.fixture)

    def test_api_group_roles_and_member_target_are_explicit(self):
        backend=self.fixture.backend;account=self.fixture.account
        backend.start_model_insights(account,'g:20001',6);self.fixture.fixture.idle()
        wire=self.fixture.analyzer.insight_payloads[-1]
        self.assertEqual(len({row['groupContext']['speaker'] for row in wire}),3)
        self.assertTrue(all(row['conversationKind']=='group' for row in wire))
        backend.start_model_portrait(account,'g:20001','uin:10001');self.fixture.fixture.idle()
        result=backend.model_portrait('g:20001','uin:10001')
        self.assertEqual(result['progress']['processedTargetTexts'],2,result)
        rows=self.fixture.analyzer.portrait_calls[-1][-1]
        self.assertEqual(len({row['groupContext']['speaker'] for row in rows if row['target']}),1)


if __name__=='__main__':unittest.main()
