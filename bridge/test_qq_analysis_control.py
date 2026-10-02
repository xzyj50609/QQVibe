"""R7 user controls through real backend, SQLite and loopback HTTP; synthetic data only."""
import copy
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import test_qq_online_analysis as online
from real_http import make_handler


class AnalysisControlTests(unittest.TestCase):
    def setUp(self):
        self.f=online.OnlineAnalysisTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.backend,self.account,self.user=self.f.backend,self.f.account,self.f.group
        self.base=self.f.fixture.fixture.base

    def control(self,action):
        return self.backend.control_analysis(self.account,self.user,action)

    def block_second(self):
        calls=[];original=self.f.analyzer.analyze
        def analyze(*args,**kwargs):
            calls.append(args[2])
            if len(calls)==2:
                self.f.entered.set()
                if not self.f.release.wait(8):raise RuntimeError('bounded synthetic model timeout')
            return original(*args,**kwargs)
        self.f.analyzer.analyze=analyze
        self.backend.start(self.user,'recent',6,expected_account=self.account)
        self.assertTrue(self.f.entered.wait(3))
        return calls

    def test_pause_keeps_completed_label_stops_late_commit_and_reading_continues(self):
        calls=self.block_second()
        self.assertEqual(self.control('pause')['analysisControl'],'paused')
        self.f.append('while-paused',self.f.rows[-1]['time_ms']+1000)
        self.f.release.set();self.f.drain()
        result=self.backend.analysis(self.user,20)
        self.assertEqual(set(result['results']),{calls[0]})
        self.assertEqual(len(calls),2)
        self.assertEqual(result['analysisControl'],'paused')
        self.assertEqual(self.backend.start(self.user,'recent',6)['status'],'paused')
        self.assertEqual(len(self.f.source.messages(self.user,20)),7)
        self.assertEqual(self.backend.profile(self.user,'uin:10002')['job']['status'],'paused')

    def test_resume_during_old_inference_rejects_old_epoch_and_finishes_new_job(self):
        calls=self.block_second();completed=calls[0]
        self.control('pause');self.control('resume')
        new=self.backend.start(self.user,'recent',6,expected_account=self.account)
        self.f.release.set();self.f.drain()
        result=self.backend.analysis(self.user,6)
        self.assertEqual(result['job']['id'],new['id'])
        self.assertEqual(result['job']['status'],'done')
        self.assertEqual(len(result['results']),6)
        self.assertEqual(calls.count(completed),1,'completed input must not be reinferred')
        self.assertEqual(calls.count(calls[1]),2,'pre-pause response must not be committed after resume')

    def test_resume_retains_targets_that_moved_outside_latest_window(self):
        calls=self.block_second();original={row['id'] for row in self.f.source.messages(self.user,6)}
        self.control('pause');self.f.release.set();self.f.drain()
        for index in range(8):self.f.append('paused-new-'+str(index),self.f.rows[-1]['time_ms']+1000*(index+1))
        self.control('resume');self.backend.start(self.user,'recent',2);self.f.drain()
        self.assertTrue(original <= set(self.backend.analysis(self.user,30)['results']))
        self.assertEqual(calls.count(calls[0]),1)

    def test_cancel_discards_pending_range_preserves_results_and_other_group(self):
        calls=self.block_second();self.control('cancel');self.f.release.set();self.f.drain()
        self.assertEqual(set(self.backend.analysis(self.user,6)['results']),{calls[0]})
        self.assertEqual(self.backend.start(self.user,'recent',6)['status'],'cancelled')
        self.backend.start('g:20002','recent',6);self.f.drain()
        self.assertEqual(len(self.backend.analysis('g:20002',6)['results']),6)
        with self.base.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM qq_analysis_resume_v1 WHERE account=? AND session=?',(self.account,self.user)).fetchone()[0],0)

    def test_control_is_durable_and_unknown_or_other_account_rejected(self):
        from result_store import ResultStore
        from backend_contracts import AccountChangedError
        self.control('pause')
        reopened=ResultStore(self.base.path)
        self.assertEqual(reopened.analysis_state(self.account,self.user),'paused')
        with self.assertRaises(AccountChangedError):self.backend.control_analysis('wrong',self.user,'resume')
        with self.assertRaises(ValueError):self.backend.control_analysis(self.account,'g:999999','pause')
        self.assertEqual(reopened.analysis_state(self.account,self.user),'paused')

    def test_http_control_and_r6_routes_reach_handlers(self):
        class Accounts:
            def clear_conversation(self,*args):return {'cleared':True}
            def set_conversation_reading(self,*args):return {'readEnabled':False}
            def backup_data(self,*args):return {'backedUp':True}
            def preview_restore(self,*args):return {'preview':True}
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.backend,accounts=Accounts()))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join,2);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def post(path,body):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            try:
                conn.request('POST',path,json.dumps(body),{'Content-Type':'application/json'})
                response=conn.getresponse();return response.status,json.loads(response.read())
            finally:conn.close()
        status,data=post('/api/qq/analysis/control',{'account':self.account,'user':self.user,'action':'pause'})
        self.assertEqual(status,200);self.assertEqual(data['analysisControl'],'paused')
        self.assertEqual(post('/api/qq/analysis/control',{'account':self.account,'user':self.user,'action':'unknown'})[0],400)
        for path,body in [('/api/qq/conversation/read',{'account':self.account,'user':self.user,'enabled':False}),
            ('/api/qq/conversation/clear',{'account':self.account,'user':self.user,'confirm':True}),
            ('/api/qq/data/backup',{'path':'synthetic-path'}),('/api/qq/data/restore-preview',{'path':'synthetic-path'})]:
            self.assertEqual(post(path,body)[0],200,path)


class ApiControlTests(unittest.TestCase):
    def setUp(self):
        import test_qq_api_revision as api
        self.f=api.ApiRevisionTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.analyzer.gate_release.set)
        self.user=api.CONV

    def test_cancel_inflight_insights_preserves_published_and_stops_retry_requests(self):
        from test_qq_message_store import record
        old=self.f.insights()['results']
        self.f.ingest(record('added',time_ms=5000))
        self.f.analyzer.block_next='insights'
        self.f.backend.start_model_insights(self.f.account,self.user,20)
        self.assertTrue(self.f.analyzer.gate_entered.wait(3))
        self.f.backend.control_analysis(self.f.account,self.user,'cancel')
        self.f.analyzer.gate_release.set();self.f.idle()
        job=self.f.backend.api_jobs[(self.f.account,self.user,self.f.source_id)]
        self.assertEqual(job['status'],'cancelled',job)
        from backend_contracts import api_insight_scope
        self.assertEqual(self.f.results.api_insight_view(self.f.account,self.user,api_insight_scope(self.f.source_id),limit=20),old)
        from analysis_control import AnalysisInterrupted
        calls=len(self.f.analyzer.calls)
        with self.assertRaises(AnalysisInterrupted):self.f.backend.start_model_insights(self.f.account,self.user,20)
        self.assertEqual(len(self.f.analyzer.calls),calls)
        self.f.backend.control_analysis(self.f.account,self.user,'resume')
        self.assertEqual(len(self.f.insights()['results']),3)

    def test_pause_portrait_waiting_response_is_user_state_then_resumable(self):
        self.f.analyzer.block_next='portrait'
        self.f.backend.start_model_portrait(self.f.account,self.user)
        self.assertTrue(self.f.analyzer.gate_entered.wait(3))
        self.f.backend.control_analysis(self.f.account,self.user,'pause')
        self.f.analyzer.gate_release.set();self.f.idle()
        job=self.f.backend.api_portrait_jobs[(self.f.account,self.user,self.f.source_id,self.user)]
        self.assertEqual(job['status'],'paused',job)
        self.f.backend.control_analysis(self.f.account,self.user,'resume')
        self.assertIsNotNone(self.f.portrait())

    def test_request_budget_counts_failed_attempt_prevents_retry_and_survives_reopen(self):
        from result_store import ResultStore
        usage=self.f.backend.grant_analysis_budget(self.f.account,self.user,1)['apiUsage']
        self.assertEqual(usage['remainingRequests'],1)
        calls=[]
        def fail(*args,**kwargs):calls.append(args);raise RuntimeError('rate-limit')
        with patch.object(self.f.analyzer,'model_insights',side_effect=fail),patch('backend_service.API_INSIGHT_RETRY_SECONDS',0):
            self.f.backend.start_model_insights(self.f.account,self.user,20);self.f.idle()
        self.assertEqual(len(calls),1,'a failed request consumes allowance; retry cannot exceed it')
        job=self.f.backend.api_jobs[(self.f.account,self.user,self.f.source_id)]
        self.assertEqual(job['error'],'analysis-budget-exhausted',job)
        usage=ResultStore(self.f.results.path).api_request_budget(self.f.account,self.user,self.f.source_id)
        self.assertEqual((usage['usedRequests'],usage['remainingRequests']),(1,0))
        self.assertIsNone(usage['currencyCost'])
        self.f.backend.grant_analysis_budget(self.f.account,self.user,2)
        self.assertEqual(len(self.f.insights()['results']),2)
        usage=self.f.backend.analysis_control_status(self.f.account,self.user)['apiUsage']
        self.assertEqual((usage['usedRequests'],usage['remainingRequests']),(2,1))

    def test_invalid_budget_does_not_change_allowance_or_cross_scope(self):
        for invalid in (True,0,-1,1001,'1'):
            with self.assertRaises(ValueError):self.f.backend.grant_analysis_budget(self.f.account,self.user,invalid)
        self.assertEqual(self.f.results.api_request_budget(self.f.account,self.user,self.f.source_id)['remainingRequests'],24)
        self.f.backend.grant_analysis_budget(self.f.account,self.user,1)
        self.assertEqual(self.f.results.api_request_budget(self.f.account,'other-session',self.f.source_id)['remainingRequests'],24)
        self.assertEqual(self.f.results.api_request_budget(self.f.account,self.user,'other-source')['remainingRequests'],24)


if __name__=='__main__':unittest.main()
