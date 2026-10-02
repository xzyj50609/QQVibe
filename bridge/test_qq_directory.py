"""R2 directory contract through connector, coordinator, HTTP and source.

All people, names and messages in this file are synthetic.
"""
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

import test_qq_sync as sync_fixture
from qq_connector import QQConnector, ConnectorError
from qq_entities import qq_avatar
from qq_normalize import normalize_message
from real_http import make_handler


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sync_fixture.SyncTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.sync = self.fixture.sync
        self.source = self.fixture.source
        self.owner = self.fixture.account

    def info(self, code='20001'):
        return {'conversationKey': 'g:' + code, 'kind': 'group', 'groupCode': code,
                'name': '合成讨论群', 'avatar': qq_avatar(code, group=True), 'members': [
                    {'uin': '10001', 'uid': 'u_synthetic_self', 'nick': '合成本人', 'cardName': '本人名片'},
                    {'uin': '10002', 'uid': 'u_peer_synth01', 'nick': '同名', 'cardName': '本群名片'},
                    {'uin': '10003', 'uid': 'u_peer_synth02', 'nick': '同名', 'cardName': '另一名片', 'isDelete': True}]}

    def test_explicit_group_add_through_http_projects_profiles_without_fetching_history(self):
        self.sync.connector.group_metadata = Mock(return_value=self.info())
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.fixture.backend, self.fixture.api, control_token='SYNTHETIC_CONTROL'))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('POST', '/api/qq/connection/add-group', json.dumps({'groupCode':'20001'}),
                     {'Content-Type':'application/json','X-QQVibe-Control':'SYNTHETIC_CONTROL'})
        response = conn.getresponse()
        body = json.loads(response.read())
        self.assertEqual(response.status, 200, body)
        self.assertEqual(body['user'], 'g:20001')
        self.assertIn('g:20001', self.fixture.backend.conversation_selection()['selectedSessions'])
        session = next(row for row in self.source.sessions()['sessions'] if row['username']=='g:20001')
        self.assertEqual((session['displayName'],session['isGroup'],session['conversationKind']), ('合成讨论群',True,'group'))
        self.assertEqual(session['avatar'], qq_avatar('20001', group=True))
        members = self.source.members('g:20001')
        self.assertEqual(len(members), 3)
        self.assertEqual(len({row['id'] for row in members}), 3)
        self.assertEqual(next(row for row in members if row['isSelf'])['name'], '本人名片')
        self.assertFalse(next(row for row in members if row['uin']=='10003')['active'])
        self.assertEqual(self.source.stats('g:20001')[:2], (0,0))
        self.assertFalse(any(row['method']=='POST' for row in self.fixture.server.requests))

    def test_synthetic_group_message_browse_and_member_counts_keep_concrete_authors(self):
        self.sync.connector.group_metadata = Mock(return_value=self.info())
        self.sync.add_group('20001')
        with self.source.read_library(self.owner) as (_, library):
            records=[]
            for index, uin in enumerate(['10001','10002','10002','10003']):
                raw={'chatType':2,'peerUid':'20001','msgId':str(100+index),'msgTime':'1700000001',
                     'senderUin':uin,'msgType':2,'elements':[{'textElement':{'content':'合成消息'+str(index)}}]}
                records.append(normalize_message(raw,self_uin='10001',expected_kind='group')[0])
            library.ingest(self.owner,'g:20001',records)
        window=self.source.messages('g:20001',10)
        self.assertEqual(len(window),4)
        self.assertEqual({row['senderId'] for row in window}, {'uin:10001','uin:10002','uin:10003'})
        self.assertEqual(self.source.profile_metadata('g:20001','uin:10002')['textCount'],2)
        self.assertEqual(self.source.profile_metadata('g:20001','uin:10001')['textCount'],1)
        with self.assertRaisesRegex(ValueError,'member-not-in-conversation'):
            self.source.profile_metadata('g:20001','uin:99999')


class ConnectorDirectoryTests(unittest.TestCase):
    def connector(self, detail=None, members=None):
        connector=QQConnector('http://127.0.0.1:12345','SYNTHETIC_TOKEN')
        client=Mock()
        client.request.side_effect=[(200,{'success':True,'data':detail or {'groupCode':'20001','groupName':'合成群'}},0),
                                    (200,{'success':True,'data':members if members is not None else [{'uin':'10002','uid':'u_member','nick':'合成名字'}]},0)]
        connector.client=Mock(return_value=client)
        connector.identity=Mock(return_value={'ownerUin':'10001'})
        return connector,client

    def test_member_array_and_endpoint_scope_are_preserved(self):
        connector,client=self.connector()
        result=connector.group_metadata('10001','20001')
        self.assertEqual(result['members'][0]['uin'],'10002')
        self.assertEqual([call.args[2] for call in client.request.call_args_list],
                         ['/api/groups/20001','/api/groups/20001/members?forceRefresh=false'])

    def test_wrong_group_and_unknown_member_identity_are_rejected(self):
        for detail,members in [({'groupCode':'20002'},None), (None,[{'nick':'没有身份'}]), (None,{'members':[]})]:
            connector,_=self.connector(detail,members)
            with self.assertRaises(ConnectorError):
                connector.group_metadata('10001','20001')

    def test_late_account_change_does_not_return_directory(self):
        connector,_=self.connector()
        connector.identity.side_effect=[{'ownerUin':'10001'},{'ownerUin':'10002'}]
        with self.assertRaisesRegex(ConnectorError,'account-changed'):
            connector.group_metadata('10001','20001')


if __name__=='__main__': unittest.main()
