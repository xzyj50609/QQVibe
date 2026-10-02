"""R3 real source/backend/persistence with a deterministic synthetic model."""
import copy
import unittest

import test_qq_analysis_revision as fixture_module
import test_qq_api_revision as api_fixture_module
from analysis_targets import SELF_SUBJECT, portrait_version
from backend_contracts import LOCAL_SOURCE_ID
from backend_service import Backend
from conversation_selection import ConversationSelectionStore
from model_source import ModelSourceStore
from test_qq_message_store import record, CONV
from qq_source import QQSource
from qq_message_store import QQMessageStore
from product_profile import load_product


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture_module.RevisionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend=self.fixture.backend
        self.source=self.fixture.source
        self.account=self.fixture.account
        self.analyzer=self.fixture.analyzer
        self.base=self.fixture.results
        original_batch=self.analyzer.analyze_batch
        def batch(session,payload,context):
            result=original_batch(session,payload,context)
            result['consumed']=[{'start':item['offset'],'end':len(item['text']),'complete':True} for item in payload]
            if all(item['side']=='self' for item in payload if item['target']):
                result['result']['score']=None
            return result
        self.analyzer.analyze_batch=batch
        original_fine=self.analyzer.analyze
        def fine(session,context,target,**kwargs):
            result=original_fine(session,context,target,**kwargs)
            result['score']=None if next(item for item in context if item['id']==target)['side']=='self' else result['score']
            return result
        self.analyzer.analyze=fine
        rows=[]
        for index,direction in enumerate(['self','peer','self','peer','peer']):
            row=record('t'+str(index),time_ms=1000+index*1000,text='合成自己的发言' if direction=='self' else '合成对方的发言',direction=direction,
                       sender_uin='10001' if direction=='self' else '179000000001')
            row['sender_uid']='u_owner' if direction=='self' else 'u_peer_synth01'
            rows.append(row)
        self.rows=rows
        self.fixture.ingest(*rows)

    def profile(self,member=None):
        self.backend.profile(CONV,member)
        self.fixture.drain()
        return self.backend.profile(CONV,member)

    def test_single_chat_has_two_exact_targets_and_independent_published_generations(self):
        other=self.profile()
        own=self.profile(SELF_SUBJECT)
        self.assertEqual(other['stats']['textCount'],3)
        self.assertEqual(other['stats']['analyzedCount'],3)
        self.assertEqual(own['stats']['textCount'],2)
        self.assertEqual(own['stats']['analyzedCount'],2)
        self.assertEqual(own['targetSide'],'self')
        self.assertIsNone(own['affinity'])
        self.assertFalse(own['isGroup'])
        self.assertEqual(self.profile()['stats'],other['stats'])
        self.assertEqual(self.backend._fine_portrait_context(self.account,CONV,fixture_module.VERSION,self.base,self.source.messages(CONV,10)[0]),None,
                         'two self samples cannot borrow the three-sample peer prior')
        with self.base.connect() as db:
            generations=db.execute('SELECT version,published_revision FROM qq_analysis_generations_v1 WHERE account=? AND session=?',(self.account,CONV)).fetchall()
        self.assertEqual(len(generations),2)
        self.assertTrue(all(row[1]==1 for row in generations))
        self.assertNotEqual(generations[0][0],generations[1][0])
        targets=[item['id'] for payload,_ in self.analyzer.calls for item in payload if item['target']]
        self.assertEqual(set(targets),{'t0','t1','t2','t3','t4'})

    def test_recent_fine_labels_cover_both_sides_without_counting_them_as_one_person(self):
        self.fixture.run_job(mode='recent')
        values=self.backend.analysis(CONV)['results']
        self.assertEqual(set(values),{'t0','t1','t2','t3','t4'})
        self.assertTrue(all(row['state']=='done' for row in values.values()))
        self.assertEqual(self.profile()['stats']['analyzedCount'],3)
        self.assertEqual(self.profile(SELF_SUBJECT)['stats']['analyzedCount'],2)

    def test_reopen_reuses_each_successful_target_without_model_work(self):
        own=self.profile(SELF_SUBJECT)
        other=self.profile()
        self.fixture.drain()
        library_path=self.fixture.store.path
        self.backend.shutdown()
        reopened_source=QQSource(uin='10001',store=QQMessageStore(library_path),root=self.fixture.root,profile=load_product('qq'))
        self.addCleanup(reopened_source.close)
        reopened_analyzer=fixture_module.Analyzer()
        reopened=Backend(reopened_source,analyzer=reopened_analyzer,store_factory=lambda *_:self.base,
            model_source_store=ModelSourceStore(self.fixture.root/'model.json',root=self.fixture.root),
            selection_store=ConversationSelectionStore(self.fixture.root/'selection'))
        self.addCleanup(reopened.shutdown)
        restored_self=reopened.profile(CONV,SELF_SUBJECT)
        restored_other=reopened.profile(CONV)
        self.assertEqual(restored_self['stats'],own['stats'])
        self.assertEqual(restored_other['stats'],other['stats'])
        self.assertEqual(reopened_analyzer.calls,[])

    def test_failed_self_rebuild_cannot_publish_over_self_or_peer_success(self):
        own=self.profile(SELF_SUBJECT)
        other=self.profile()
        own_version=portrait_version(fixture_module.VERSION,self.source,CONV,SELF_SUBJECT)
        with self.base.connect() as db:
            before=list(db.execute('SELECT * FROM batch_progress_v1 ORDER BY base_version,subject'))
        changed=copy.deepcopy(self.rows[0]);changed['text']='变更后的合成发言'
        self.fixture.ingest(changed)
        self.analyzer.fail_at=len(self.analyzer.calls)+1
        self.backend.profile(CONV,SELF_SUBJECT)
        self.fixture.drain()
        failed=self.backend.profile(CONV,SELF_SUBJECT)
        self.assertTrue(failed['stale'])
        self.assertTrue(failed['hasPublishedAnalysis'])
        self.assertEqual(failed['stats']['analyzedCount'],own['stats']['analyzedCount'])
        with self.base.connect() as db:
            self.assertEqual(list(db.execute('SELECT * FROM batch_progress_v1 ORDER BY base_version,subject')),before)
            self.assertEqual(db.execute('SELECT published_revision FROM qq_analysis_generations_v1 WHERE version=?',(own_version,)).fetchone()[0],1)
        self.assertEqual(other['stats']['analyzedCount'],3)

    def test_media_blank_and_recalled_self_text_are_not_personal_expression_samples(self):
        self.fixture.ingest(record('media',time_ms=7000,direction='self',kind='image'),
                            record('blank',time_ms=8000,direction='self',text='  '),
                            record('recalled',time_ms=9000,direction='self',text='占位不应分析',status='recalled'))
        own=self.profile(SELF_SUBJECT)
        self.assertEqual(own['stats']['messageCount'],5)
        self.assertEqual((own['stats']['textCount'],own['stats']['analyzedCount']),(2,2))

    def test_self_and_same_native_id_in_two_conversations_do_not_share_persona(self):
        first=self.profile(SELF_SUBJECT)
        second='u:u_second_peer'
        self.fixture.store.ensure_conversation(self.account,second,peer_uid='u_second_peer',display_name='另一合成单聊')
        row=copy.deepcopy(self.rows[0]);row['conversation_key']=second
        self.fixture.store.ingest(self.account,second,[row])
        self.backend.profile(second,SELF_SUBJECT)
        self.fixture.drain()
        other=self.backend.profile(second,SELF_SUBJECT)
        self.assertEqual(other['stats']['analyzedCount'],1)
        self.assertEqual(other['stats']['textCount'],1)
        self.assertEqual(self.profile(SELF_SUBJECT)['stats'],first['stats'])


class ApiTargetTests(unittest.TestCase):
    def setUp(self):
        self.fixture=api_fixture_module.ApiRevisionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend=self.fixture.backend
        self.source=self.fixture.source
        self.account=self.fixture.account
        self.analyzer=self.fixture.analyzer
        for index in range(2):
            row=record('own'+str(index),time_ms=5000+index*1000,text='合成本人发言',direction='self',sender_uin='10001')
            row['sender_uid']='u_owner'
            self.fixture.ingest(row)
        original=self.analyzer.model_portrait
        def portrait(*args):
            result=original(*args)
            messages=args[-1]
            result['summary']='OWN_PORTRAIT' if any(row['target'] and row['sender']=='SELF' for row in messages) else 'PEER_PORTRAIT'
            return result
        self.analyzer.model_portrait=portrait

    def test_api_counts_and_portrait_priors_belong_to_each_single_chat_target(self):
        for member in [None,SELF_SUBJECT]:
            self.backend.start_model_portrait(self.account,CONV,member)
            self.fixture.idle()
            result=self.backend.model_portrait(CONV,member)
            self.assertEqual(result['progress']['totalTargetTexts'],2)
            self.assertEqual(result['progress']['processedTargetTexts'],2)
            self.assertEqual(result['identity']['messageCount'],2)
            self.assertTrue(result['progress']['complete'])
            wire=self.analyzer.portrait_calls[-1][-1]
            targets={item['id'].split(':')[0] for item in wire if item['target']}
            self.assertEqual(targets,{'own0','own1'} if member else {'late1','late2'})
        insights=self.fixture.insights()
        self.assertEqual(set(insights['results']),{'late1','late2','own0','own1'})
        wire=self.analyzer.insight_payloads[-1]
        for message in wire:
            self.assertEqual(message.get('portraitContext'),'OWN_PORTRAIT' if message['sender']=='SELF' else 'PEER_PORTRAIT')


if __name__=='__main__':unittest.main()
