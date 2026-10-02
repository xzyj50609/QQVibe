"""Known/unknown version and capability checks, synthetic services only."""
import unittest
from unittest.mock import Mock
import test_qq_sync as fixture
from fake_qce_server import FakeQCEHandler
from qq_connector import QQConnector,ConnectorError


class VersionHandler(FakeQCEHandler):
    def do_GET(self):
        if self.path.split('?',1)[0]!='/api/system/info':return super().do_GET()
        self._record(b'')
        if not self._authorized():return self._send(401,b'{"success":false}')
        self._send_envelope({'version':self.server.scenario.get('qceVersion','6.3.1'),
            'napcat':{'version':'4.0.0','online':True,'selfInfo':{'uin':'10001'}}})


class VersionTests(unittest.TestCase):
    def identity(self,version,**extra):
        info={'version':version,'napcat':{'online':True,'selfInfo':{'uin':'10001'},'version':'unknown'},**extra}
        client=Mock()
        client.request.side_effect=[(200,{'success':True,'data':info},0),(200,{'success':True,'data':{'loggedIn':True}},0)]
        return QQConnector.identity(client)

    def test_known_qce_api_does_not_invent_qq_or_napcat_versions(self):
        value=self.identity('6.3.0')['compatibility']
        self.assertEqual(value['state'],'verified-qce-api')
        self.assertEqual(value['qqVersion'],'unknown')
        self.assertEqual(value['napcatVersion'],'unknown')
        self.assertFalse(value['qqCombinationVerified'])

    def test_new_version_can_be_identified_without_being_blessed(self):
        value=self.identity('7.0.0')['compatibility']
        self.assertTrue(value['requiresConsent'])
        self.assertEqual(value['state'],'unverified-version')
        self.assertEqual(value['capabilities']['boundedPagination'],'not-yet-checked')

    def test_missing_or_malicious_version_remains_incompatible(self):
        for version in (None,1,'newest','6.3.0\nSECRET','6.3.0'+'x'*80):
            with self.subTest(version=version),self.assertRaisesRegex(ConnectorError,'unsupported-version'):
                self.identity(version)


class UnknownServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.SyncTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.sync=self.fixture.sync;self.server=self.fixture.server
        self.server.server.RequestHandlerClass=VersionHandler

    def test_unknown_version_requires_exact_version_consent_before_message_fetch(self):
        previous=len(self.server.requests)
        before=self.fixture.source.messages(fixture.CONV,100)
        with self.assertRaisesRegex(ConnectorError,'unverified-version'):self.sync.connect()
        self.assertFalse(self.sync.public()['enabled'])
        self.assertEqual(self.sync.public()['compatibility']['qceVersion'],'6.3.1')
        self.assertEqual(self.fixture.source.messages(fixture.CONV,100),before)
        self.assertFalse(any(row['path']=='/api/messages/fetch' for row in self.server.requests[previous:]))
        connected=self.sync.connect(allow_unverified=True)
        self.assertTrue(connected['enabled'])
        self.sync.run_once(fixture.CONV)
        value=self.sync.public()['compatibility']
        self.assertEqual(value['state'],'unverified-version')
        self.assertEqual(value['capabilities']['boundedPagination'],'checked')
        self.assertEqual(value['capabilities']['stableMessageIdentity'],'checked')
        self.sync.disconnect();self.assertTrue(self.sync.connect()['enabled'])
        self.server.server.scenario['qceVersion']='6.3.2'
        with self.assertRaisesRegex(ConnectorError,'unverified-version'):self.sync.connect()
        self.assertFalse(self.sync.public()['enabled'])

    def test_unknown_version_consent_does_not_bypass_malformed_pagination(self):
        self.sync.connect(allow_unverified=True)
        before=self.fixture.source.messages(fixture.CONV,100)
        self.server.server.scenario['fetch']='missing-metadata'
        result=self.sync.run_once(fixture.CONV)
        self.assertNotEqual(result['state'],'complete')
        self.assertEqual(self.fixture.source.messages(fixture.CONV,100),before)

    def test_unknown_version_with_incompatible_message_id_stops_before_any_message_write(self):
        self.sync.connect(allow_unverified=True)
        before=self.fixture.source.messages(fixture.CONV,100)
        self.server.server.dataset[0]['msgId']=True
        result=self.sync.run_once(fixture.CONV)
        self.assertEqual(result['state'],'error')
        self.assertEqual(result['reason'],'protocol-invalid')
        self.assertFalse(self.sync.public()['enabled'])
        self.assertEqual(self.fixture.source.messages(fixture.CONV,100),before)


if __name__=='__main__':unittest.main()
