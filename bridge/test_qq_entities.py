"""Schema upgrade preserves frozen rows; typed group/member identities stay separate."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from qq_identity import account_key
from qq_message_store import QQMessageStore, SCHEMA_SQL_V3, SCHEMA_VERSION, message_key
from qq_normalize import normalize_message, NormalizationError
from qq_entities import qq_avatar, avatar


class EntityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'messages.sqlite'
        self.store = QQMessageStore(self.path)
        self.addCleanup(self.store.close)
        self.owner = account_key('10001')

    def raw(self, group, sender='10002', msg='90071992547409931'):
        return {'chatType':2,'peerUid':group,'senderUin':sender,'senderUid':'u_member_'+sender,
                'msgId':msg,'msgSeq':'9223372036854775808','msgTime':'1700000001',
                'msgType':2,'elements':[{'textElement':{'content':'合成作者文本'}}]}

    def test_two_groups_same_author_and_message_id_are_separate(self):
        for group, card in [('20001','群一名片'),('20002','群二名片')]:
            user='g:'+group
            self.store.ensure_conversation(self.owner,user,kind='group',group_code=group)
            self.store.set_members(self.owner,user,[{'uin':'10002','uid':'u_member_10002','nick':'同名','cardName':card}])
            row,_=normalize_message(self.raw(group),self_uin='10001',expected_kind='group')
            self.store.ingest(self.owner,user,[row])
        rows=self.store.connection.execute('SELECT message_key,local_seq FROM messages').fetchall()
        self.assertEqual(len({row[0] for row in rows}),2)
        self.assertEqual([row[1] for row in rows],[1,1])
        self.assertEqual(self.store.members(self.owner,'g:20001')[0]['card_name'],'群一名片')
        self.assertEqual(self.store.members(self.owner,'g:20002')[0]['card_name'],'群二名片')
        self.assertEqual(self.store.integrity_errors(),[])

    def test_wrong_scope_stays_rejected(self):
        with self.assertRaises(NormalizationError):
            normalize_message(self.raw('20001'),self_uin='10001')
        with self.assertRaises(Exception):
            self.store.ensure_conversation(self.owner,'u:u_peer',kind='group',group_code='20001')
        raw=self.raw('20001');raw['chatType']=1
        with self.assertRaises(NormalizationError):
            normalize_message(raw,self_uin='10001',expected_kind='group')

    def test_uid_then_uin_does_not_rename_the_existing_person(self):
        member=self.store.set_person(self.owner,uid='u_late_member',nickname='旧昵称')
        self.assertEqual(self.store.set_person(self.owner,uin='10002',uid='u_late_member',nickname='新昵称'),member)
        self.assertEqual(self.store.person_for_sender(self.owner,uin='10002')['member_id'],member)
        self.store.set_person(self.owner,uin='10003',uid='u_other_member',nickname='新昵称')
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM qq_people_v1').fetchone()[0],2)
        with self.assertRaises(ValueError):
            self.store.set_person(self.owner,uin='10002',uid='u_other_member')

    def test_unknown_group_sender_survives_but_cannot_enter_target_statistics(self):
        raw=self.raw('20001');raw.pop('senderUin');raw.pop('senderUid')
        row,_=normalize_message(raw,self_uin='10001',expected_kind='group')
        self.assertEqual((row['direction'],row['status']),('conflict','conflict'))
        self.store.ensure_conversation(self.owner,'g:20001',kind='group',group_code='20001')
        self.store.ingest(self.owner,'g:20001',[row])
        self.assertEqual(self.store.counts(self.owner,'g:20001'),(1,0))

    def test_avatar_addresses_cannot_carry_credentials_or_point_at_local_files(self):
        self.assertEqual(avatar(qq_avatar('10001')),qq_avatar('10001'))
        for value in ('file:///C:/private','https://q1.qlogo.cn.evil/path','https://token@q1.qlogo.cn/path','http://q1.qlogo.cn/path','https://[malformed'):
            self.assertEqual(avatar(value),'')

    def test_v3_copy_upgrade_preserves_every_old_column_and_key(self):
        self.store.close()
        legacy=Path(self.temp.name)/'legacy.sqlite'
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript(SCHEMA_SQL_V3)
            db.execute('PRAGMA user_version=3')
            user='u:u_old_peer'
            db.execute("INSERT INTO conversations(conversation_key,account_key,peer_uid,kind,data_revision) VALUES (?,?,?,'friend',7)",(user,self.owner,'u_old_peer'))
            key=message_key(self.owner,user,'qce-msgId','90071992547409931')
            db.execute("INSERT INTO messages(message_key,account_key,conversation_key,native_id_kind,native_id,native_seq,local_seq,direction,time_ms,kind,text,normalize_version) "
                "VALUES (?,?,?,'qce-msgId',?,'9223372036854775808',17,'peer',1700000000000,'text','旧内容','qq-v3')",(key,self.owner,user,'90071992547409931'))
            old={name:[tuple(row) for row in db.execute('SELECT * FROM '+name)] for name in ('conversations','messages')}
            db.commit()
        upgraded=QQMessageStore(legacy);self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.connection.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
        self.assertTrue(upgraded.migration_backup.is_file())
        for name in ('conversations','messages'):
            with closing(sqlite3.connect(upgraded.migration_backup)) as backup:
                columns=[row[1] for row in backup.execute('PRAGMA table_info('+name+')')]
            self.assertEqual([tuple(row) for row in upgraded.connection.execute('SELECT '+','.join(columns)+' FROM '+name)],old[name])
        self.assertEqual(upgraded.integrity_errors(),[])
        upgraded.backup_to(Path(self.temp.name)/'verified-v4.sqlite')


if __name__=='__main__':unittest.main()
