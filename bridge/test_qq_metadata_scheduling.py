"""Contact operations get a bounded slot without cancelling or crossing a scan."""
import http.client
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from qq_connector import ConnectorError
from qq_sync import QQSync
from real_http import make_handler
import test_qq_sync as fixtures
from test_qq_message_store import CONV


class MetadataSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SyncTests();self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.sync=self.fixture.sync
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.fixture.backend,self.fixture.api))
        self.server.daemon_threads=True
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.server_thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(lambda:self.server_thread.join(timeout=3))
        self.addCleanup(self.server.shutdown)

    def add(self,outputs):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.request('POST','/api/qq/connection/add-contact',json.dumps({'peerUin':'20001','name':'并发合成好友'}),
                headers={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{self.server.server_port}'})
            response=connection.getresponse();outputs.append((response.status,json.loads(response.read())))
        finally:connection.close()

    def test_add_contact_waits_for_an_owned_scan_and_does_not_cancel_its_commit(self):
        entered,release=threading.Event(),threading.Event();scans=[];contacts=[]
        original=self.sync.connector.scan;epoch=self.sync.epoch
        def delayed(*args,**kwargs):
            entered.set()
            if not release.wait(3):raise RuntimeError('synthetic scan barrier timeout')
            return original(*args,**kwargs)
        with patch.object(self.sync.connector,'scan',delayed):
            scanner=threading.Thread(target=lambda:scans.append(self.sync.run_once(CONV)),daemon=True)
            scanner.start();self.assertTrue(entered.wait(2))
            contact=threading.Thread(target=self.add,args=(contacts,),daemon=True);contact.start()
            try:
                end=time.monotonic()+1
                while not self.sync.metadata_waiting and contact.is_alive() and time.monotonic()<end:time.sleep(.005)
                self.assertEqual(self.sync.metadata_waiting,1)
                self.assertTrue(contact.is_alive())
            finally:release.set();scanner.join(timeout=4);contact.join(timeout=4)
        self.assertFalse(scanner.is_alive());self.assertFalse(contact.is_alive())
        self.assertEqual(scans[0]['state'],'complete')
        self.assertEqual(self.fixture.checkpoint()['state'],'COMMITTED')
        self.assertEqual(contacts[0][0],200)
        self.assertTrue(contacts[0][1]['selected'])
        self.assertEqual(self.sync.epoch,epoch)
        self.assertEqual(self.sync.metadata_waiting,0)

    def test_account_pause_cancels_a_waiting_contact_before_any_lookup(self):
        entered=threading.Event();scans=[];contacts=[]
        def delayed(*args,**kwargs):
            entered.set()
            end=time.monotonic()+3
            while time.monotonic()<end:
                kwargs['check']();time.sleep(.01)
            raise RuntimeError('scan was not cancelled')
        before=len(self.fixture.server.requests)
        with patch.object(self.sync.connector,'scan',delayed):
            scanner=threading.Thread(target=lambda:scans.append(self.sync.run_once(CONV)),daemon=True)
            scanner.start();self.assertTrue(entered.wait(2))
            contact=threading.Thread(target=self.add,args=(contacts,),daemon=True);contact.start()
            end=time.monotonic()+1
            while not self.sync.metadata_waiting and time.monotonic()<end:time.sleep(.005)
            self.assertEqual(self.sync.metadata_waiting,1)
            self.sync.pause_binding();scanner.join(timeout=4);contact.join(timeout=4)
        self.assertFalse(scanner.is_alive());self.assertFalse(contact.is_alive())
        self.assertEqual(scans[0]['state'],'cancelled')
        self.assertEqual(contacts[0],(409,{'error':'scope-changed'}))
        self.assertFalse(any(row['path'].startswith('/api/users/lookup') for row in self.fixture.server.requests[before:]))
        self.assertEqual(self.sync.metadata_waiting,0)

    def test_busy_timeout_does_not_release_someone_elses_slot(self):
        self.sync.busy=True
        try:
            with patch.object(QQSync,'METADATA_WAIT_SECONDS',.05):
                started=time.monotonic()
                with self.assertRaisesRegex(ConnectorError,'sync-busy'):self.sync.add_contact('20001')
                self.assertGreaterEqual(time.monotonic()-started,.04)
                self.assertTrue(self.sync.busy)
                self.assertEqual(self.sync.metadata_waiting,0)
        finally:
            with self.sync.condition:self.sync.busy=False;self.sync.condition.notify_all()


if __name__=='__main__':unittest.main()
