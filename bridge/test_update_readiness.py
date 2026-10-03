"""Loopback update readiness with synthetic jobs; never reads real accounts."""
import threading
import unittest
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
import json
from real_http import make_handler

class UpdateReadinessTests(unittest.TestCase):
    def setUp(self):
        self.backend=SimpleNamespace(jobs_lock=threading.Lock(),jobs={},request_condition=threading.Condition(),active_requests=1)
        self.importer=SimpleNamespace(lock=threading.Lock(),job=None)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.backend,SimpleNamespace(imports=self.importer)))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
    def ready(self):
        with urlopen('http://127.0.0.1:'+str(self.server.server_port)+'/api/update/readiness',timeout=3) as response:
            value=json.load(response)
        self.assertEqual(set(value),{'ready'})
        return value['ready']
    def test_idle(self):self.assertTrue(self.ready())
    def test_analysis_blocks_then_finishes(self):
        self.backend.jobs['synthetic']={'status':'running'};self.assertFalse(self.ready())
        self.backend.jobs['synthetic']['status']='done';self.assertTrue(self.ready())
    def test_import_blocks_until_complete(self):
        for state in ('reading','committing'):
            self.importer.job={'public':{'state':state}};self.assertFalse(self.ready())
        self.importer.job={'public':{'state':'complete'}};self.assertTrue(self.ready())
    def test_inflight_requests(self):
        self.backend.active_requests=2;self.assertFalse(self.ready())
    def test_member_analysis_blocks(self):
        self.backend.batch_engine=SimpleNamespace(member_jobs={'synthetic':{'status':'queued'}})
        self.assertFalse(self.ready())
    def test_api_work_blocks(self):
        self.backend.api_tasks=SimpleNamespace(condition=threading.Condition(),inflight=1)
        self.assertFalse(self.ready())
    def test_recent_window_blocks(self):
        self.backend.recent_windows={'synthetic':{}}
        self.assertFalse(self.ready())

if __name__=='__main__':unittest.main()
