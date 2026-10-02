"""R4 G1 typed group API + import, only synthetic isolated fixtures."""
import copy
import json
import threading
import unittest
from pathlib import Path

import test_qq_sync as sync_fixture
import test_qq_import as import_fixture
from qq_connector import QQConnector, ConnectorError, SyncPolicy
from qq_normalize import normalize_export, ExportFormatError
from fake_qce_server import RunningFakeQCE, FakeQCEHandler, DEFAULT_TOKEN, BASE_EPOCH_S, filter_rows

BASE=BASE_EPOCH_S*1000
PEOPLE=[{'uin':'10001','uid':'u_synthetic_self','nick':'合成本人','cardName':'我'},
        {'uin':'10002','uid':'u_peer_synth01','nick':'同名','cardName':'成员甲'},
        {'uin':'10003','uid':'u_peer_synth02','nick':'同名','cardName':'成员乙'}]


class GroupHandler(FakeQCEHandler):
    def do_GET(self):
        if not self.path.split('?',1)[0].startswith('/api/groups/'):
            return super().do_GET()
        self._record(b'')
        if not self._authorized():
            return self._send(401,b'{"success":false}')
        code=self.path.split('/')[3]
        if code not in ('20001','20002'):
            return self._send(404,b'{"success":false}')
        data=PEOPLE if '/members' in self.path else {'groupCode':code,'groupName':'合成讨论群'+code[-1]}
        self._send_envelope(data)


def group_server(server):
    server.server.RequestHandlerClass=GroupHandler
    server.server.scenario['groupEnabled']=True
    original=copy.deepcopy(server.server.dataset)
    groups=[]
    for code in ('20001','20002'):
        for index,raw in enumerate(original[:9]):
            person=PEOPLE[index%3]
            row=copy.deepcopy(raw)
            row.update(chatType=2,peerUid=code,senderUin=person['uin'],senderUid=person['uid'],
                       sendType='2' if person['uin']=='10001' else '0',text='合成群消息'+str(index))
            groups.append(row)
    server.server.dataset=original+groups
    def matched(body):
        peer=body.get('peer',{})
        rows=[row for row in server.server.dataset if row.get('chatType',1)==peer.get('chatType') and
              row.get('peerUid','u_peer_synth01')==peer.get('peerUid')]
        window=body.get('filter') or {}
        return filter_rows(rows,window.get('startTime'),window.get('endTime'))
    server.server.matched=matched
    return server


class GroupReadTests(unittest.TestCase):
    def setUp(self):
        self.fixture=sync_fixture.SyncTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        group_server(self.fixture.server)
        self.sync=self.fixture.sync
        self.sync.group_ready=True
        self.connector=self.sync.connector

    def test_typed_group_scan_keeps_self_two_other_authors_and_string_ids(self):
        result=self.connector.scan('10001','20001',BASE,BASE+20000,kind='group')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len(result['records']),9)
        self.assertEqual({row['sender_uin'] for row in result['records']},{'10001','10002','10003'})
        self.assertEqual({row['conversation_key'] for row in result['records']},{'g:20001'})
        self.assertTrue(all(isinstance(row['native_id'],str) and isinstance(row['native_seq'],str) for row in result['records']))
        self.assertTrue(all(row['body']['peer']=={'chatType':2,'peerUid':'20001'} for row in self.fixture.server.requests if row['method']=='POST'))

    def test_group_selection_initial_read_tail_dedup_and_natural_added_row(self):
        self.sync.initial_read=True
        self.sync.add_group('20001')
        source=self.fixture.source
        self.assertEqual(len(source.messages('g:20001',100)),9)
        self.assertEqual({row['senderName'] for row in source.messages('g:20001',100)},{'我','成员甲','成员乙'})
        self.assertEqual(self.sync.run_once('g:20001')['inserted'],0)
        raw=copy.deepcopy(next(row for row in self.fixture.server.server.dataset if row.get('peerUid')=='20001'))
        raw.update(msgId='900719925474199999',msgTime=str(BASE_EPOCH_S+21),text='合成自然新增')
        self.fixture.server.server.dataset.append(raw)
        result=self.sync.run_once('g:20001',now_ms=BASE+22000)
        self.assertEqual(result['inserted'],1)
        self.assertEqual(len(source.messages('g:20001',100)),10)
        self.assertEqual(len(source.messages('u:u_peer_synth01',100)),1)
        before=sum(row['method']=='POST' for row in self.fixture.server.requests)
        self.sync.add_group('20001')
        self.assertEqual(sum(row['method']=='POST' for row in self.fixture.server.requests),before)

    def test_two_groups_with_the_same_native_ids_and_authors_remain_isolated(self):
        self.sync.initial_read=True
        for code in ('20001','20002'):self.sync.add_group(code)
        source=self.fixture.source
        self.assertEqual(source.profile_metadata('g:20001','uin:10002')['textCount'],3)
        self.assertEqual(source.profile_metadata('g:20002','uin:10002')['textCount'],3)
        with source.read_library(self.fixture.account) as (_,store):
            self.assertEqual(store.connection.execute("SELECT COUNT(DISTINCT message_key) FROM messages WHERE conversation_key LIKE 'g:%'").fetchone()[0],18)
            self.assertEqual(store.integrity_errors(),[])

    def test_group_pause_disconnect_and_reconnect_preserve_cached_history(self):
        self.sync.initial_read=True;self.sync.add_group('20001')
        before=len(self.fixture.server.requests)
        self.sync.pause_binding()
        with self.assertRaisesRegex(ConnectorError,'sync-paused'):self.sync.run_once('g:20001')
        self.assertEqual(len(self.fixture.server.requests),before)
        self.sync.resume_binding();self.sync.disconnect()
        self.assertEqual(len(self.fixture.source.messages('g:20001',100)),9)
        with self.assertRaisesRegex(ConnectorError,'sync-disabled'):self.sync.run_once('g:20001')
        self.sync.connect()
        self.assertEqual(self.sync.run_once('g:20001')['inserted'],0)
        self.assertEqual(len(self.fixture.source.messages('g:20001',100)),9)

    def test_unselected_group_and_wrong_peer_type_cannot_reach_message_reads(self):
        before=len(self.fixture.server.requests)
        with self.assertRaisesRegex(ConnectorError,'conversation-not-selected'):
            self.sync.run_once('g:20001')
        with self.assertRaises(ConnectorError):
            self.connector.scan('10001','u_peer_synth01',BASE,BASE+20000,kind='group')
        with self.assertRaises(ConnectorError):
            self.connector.scan('10001','20001',BASE,BASE+20000)
        self.assertEqual(len(self.fixture.server.requests),before)

    def test_group_browser_member_and_date_filter_share_sender_projection_and_scope(self):
        from qq_history_browser import browse,search
        self.sync.initial_read=True
        self.sync.add_group('20001')
        source=self.fixture.source;account=self.fixture.account
        page=browse(source,account,'g:20001',limit=20)
        self.assertEqual({row['senderName'] for row in page['messages']},{'我','成员甲','成员乙'})
        filtered=search(source,account,'g:20001',member='uin:10002',day='2023-11-14')
        self.assertEqual(len(filtered['messages']),3)
        self.assertEqual({row['senderId'] for row in filtered['messages']},{'uin:10002'})
        anchor=filtered['messages'][0]['historyCursor']
        around=browse(source,account,'g:20001',around=anchor,limit=8)
        self.assertIn(filtered['messages'][0]['id'],{row['id'] for row in around['messages']})
        self.assertEqual({row['senderName'] for row in around['messages']},{'我','成员甲','成员乙'})
        with self.assertRaises(ValueError):search(source,account,'g:20001',member='uin:99999')
        with self.assertRaises(ValueError):browse(source,account,'u:u_peer_synth01',around=anchor)

    def test_group_overview_uses_observed_participants_and_does_not_start_a_persona(self):
        self.sync.initial_read=True
        self.sync.add_group('20001')
        source=self.fixture.source
        with source.read_library(self.fixture.account) as (_,store):
            store.set_members(self.fixture.account,'g:20001',[{'uin':'10004','uid':'u_quiet','nick':'未发言的合成成员'}])
        before=self.fixture.backend.tasks.unfinished_tasks
        overview=self.fixture.backend.profile('g:20001')
        self.assertTrue(overview['objectiveOverview'])
        self.assertEqual(overview['stats']['participantCount'],3)
        self.assertEqual(overview['stats']['messageCount'],9)
        self.assertEqual(overview['stats']['textCount'],9)
        self.assertIsNone(overview['mbti'])
        self.assertEqual(self.fixture.backend.tasks.unfinished_tasks,before)


def export_document():
    messages=[]
    for index in range(6):
        person=PEOPLE[index%3]
        messages.append({'messageId':str(900719925474099300+index),'timestamp':f'2023-11-14T22:13:{20+index:02d}Z',
            'messageType':2,'sender':{'uin':person['uin'],'uid':person['uid'],'name':person['nick'],
                'nickname':person['nick'],'cardName':person['cardName']},'content':{'text':'合成群导入'+str(index)}})
    return {'metadata':{'version':'6.3.0','messageCount':6},'chatInfo':{'type':'group','peerUid':'20001',
        'selfUin':'10001','selfUid':'u_synthetic_self','name':'合成导入群'},'messages':messages}


class GroupExportTests(unittest.TestCase):
    def test_only_explicit_group_scope_accepts_multiauthor_export(self):
        document=export_document()
        with self.assertRaisesRegex(ExportFormatError,'not-single-chat'):normalize_export(document)
        result=normalize_export(document,expected_kind='group')
        self.assertEqual(result['kind'],'group')
        self.assertEqual(result['conversationKey'],'g:20001')
        self.assertEqual(result['counts']['rowsOk'],6)
        self.assertIsNone(result['peerUid'])
        self.assertEqual({row['direction'] for row in result['records']},{'self','peer'})

    def test_json_then_jsonl_group_import_is_previewed_and_idempotent(self):
        fixture=import_fixture.ImportTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.document=export_document();fixture.write()
        preview=fixture.preview()
        self.assertEqual(preview['state'],'ready',preview)
        self.assertEqual(preview['preview']['kind'],'group')
        self.assertEqual(preview['preview']['groupCode'],'20001')
        self.assertFalse(fixture.fixture.data_root.exists())
        result=fixture.commit(preview)
        self.assertEqual((result['state'],result['result']['inserted']),('complete',6),result)
        fixture.api.activate(result['result']['accountId'])
        source=fixture.fixture.source
        self.assertEqual({row['senderName'] for row in source.messages('g:20001',80)},{'我','成员甲','成员乙'})
        self.assertEqual(source.profile_metadata('g:20001','uin:10002')['textCount'],2)
        fixture.chunks()
        preview=fixture.preview()
        repeated=fixture.commit(preview)
        self.assertEqual((repeated['state'],repeated['result']['inserted'],repeated['result']['unchanged']),('complete',0,6),repeated)
        self.assertEqual(len(source.messages('g:20001',80)),6)


if __name__=='__main__':unittest.main()
