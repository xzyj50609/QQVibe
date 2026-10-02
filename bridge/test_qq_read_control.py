"""Persistent session stop/read controls do not block local browsing or other work."""
import threading
import unittest
import test_qq_sync as fixture
from qq_connector import ConnectorError


class ReadControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.SyncTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.sync=self.fixture.sync;self.backend=self.fixture.backend;self.account=self.fixture.account
        self.source=self.fixture.source;self.user=fixture.CONV

    def reading(self,enabled):
        self.backend.selection_store.set_reading(self.account,self.user,enabled)
        self.sync.reading_changed(self.account,self.user,enabled)

    def test_paused_session_remains_selected_and_readable_without_fetches(self):
        before=self.source.messages(self.user,100);requests=len(self.fixture.server.requests)
        self.reading(False)
        self.assertIn(self.user,self.backend.selection_store.get(self.account)['selectedSessions'])
        with self.assertRaisesRegex(ConnectorError,'conversation-read-paused'):self.sync.run_once(self.user)
        self.assertEqual(requests,len(self.fixture.server.requests))
        self.assertEqual(before,self.source.messages(self.user,100))
        self.reading(True);result=self.sync.run_once(self.user)
        self.assertEqual(result['state'],'complete')
        self.assertGreater(len(self.source.messages(self.user,100)),len(before))

    def test_stop_during_scan_rejects_late_message_and_checkpoint_commit(self):
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        original=self.sync.connector.scan
        def delayed(*args,**kwargs):
            result=original(*args,**kwargs);entered.set()
            if not release.wait(5):raise RuntimeError('bounded synthetic scan timeout')
            return result
        self.sync.connector.scan=delayed
        before=self.source.messages(self.user,100)
        result=[]
        thread=threading.Thread(target=lambda:result.append(self.sync.run_once(self.user)))
        thread.start();self.addCleanup(lambda:thread.join(8))
        self.assertTrue(entered.wait(3))
        self.reading(False)
        self.assertEqual(before,self.source.messages(self.user,100),'local reading stays available')
        release.set();thread.join(8)
        self.assertFalse(thread.is_alive());self.assertEqual(result[0]['state'],'cancelled')
        self.assertEqual(before,self.source.messages(self.user,100))
        self.assertIsNone(self.fixture.checkpoint())


if __name__=='__main__':unittest.main()
