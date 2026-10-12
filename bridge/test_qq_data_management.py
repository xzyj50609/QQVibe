"""Full backups/restores/failures only on synthetic, disposable data copies."""
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend_service import Backend
from conversation_selection import ConversationSelectionStore
from model_source import ModelSourceStore
from qq_account_api import QQAccountAPI
from qq_data_management import create_backup,inspect_backup,restore_backup,digest,recover_pending_restores
from result_store import ResultStore
import test_qq_local_integrity as library_fixture
import test_qq_analysis_revision as analysis_fixture
from test_qq_message_store import CONV,record,UIN


class DataTests(unittest.TestCase):
    def setUp(self):
        self.fixture=library_fixture.Library();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root/'QQVibeData';self.account=self.fixture.account
        (self.root/'real-client-data').mkdir(parents=True,exist_ok=True)
        from account_store import account_id
        self.results=ResultStore(self.root/'real-client-data'/(account_id(self.account)+'.sqlite3'))
        self.backend=Backend(self.fixture.source,analyzer=analysis_fixture.Analyzer(),store_factory=lambda *_:self.results,
            selection_store=ConversationSelectionStore(self.root/'real-client-data'),
            model_source_store=ModelSourceStore(self.root/'real-client-runtime/model-source.json',root=self.fixture.root))
        self.addCleanup(self.backend.shutdown)
        self.accounts=QQAccountAPI(self.backend,self.root)
        self.accounts.store.register(self.account,self.fixture.source.identity()[1],owner_uin=UIN,nickname='合成用户')
        self.backend.set_conversation_selected(self.account,CONV,True)
        self.fixture.ingest(record('b1',time_ms=1000,text='合成资料已经准备好了'),record('b2',time_ms=2000,text='谢谢你的帮助'))
        self.backend.start(CONV,'recent',2,expected_account=self.account)
        with self.backend.tasks.all_tasks_done:
            self.assertTrue(self.backend.tasks.all_tasks_done.wait_for(lambda:self.backend.tasks.unfinished_tasks==0,timeout=12))
        runtime=self.root/'real-client-runtime';runtime.mkdir(parents=True,exist_ok=True)
        self.runtime=runtime
        (runtime/'qq-connection.json').write_text(json.dumps({'version':1,'enabled':True,'baseUrl':'http://127.0.0.1:12345',
            'ownerUin':UIN,'bearer':'U1lOVEhFVElDX0VOQ1JZUFRFRF9UT0tFTg=='}),encoding='utf-8')
        (runtime/'inference-settings.json').write_text('{"provider":"cpu"}',encoding='utf-8')
        (runtime/'local-model-source.json').write_text('{"schema":1,"path":"D:/synthetic/models"}',encoding='utf-8')
        (runtime/'api-model-source.json').write_text(json.dumps({'version':1,'selectedMode':'api','sourceId':'a'*32,
            'api':{'protocol':'chat_completions','baseUrl':'https://synthetic.invalid/v1','model':'SYNTHETIC',
                   'contextTokens':4096,'encryptedKey':'U1lOVEhFVElD'}}),encoding='utf-8')
        self.preferences={'theme':'light','zoom':'1.25','intent':False,'labelDetails':True}
        self.backup=self.fixture.root/'backup.zip'

    def backup_data(self):
        return self.accounts.backup_data(self.backup,self.preferences)

    def copies(self,name):
        target=self.fixture.root/name
        shutil.copytree(self.root,target)
        return target

    def hashes(self,root):
        return {file.relative_to(root).as_posix():hashlib.sha256(file.read_bytes()).hexdigest()
            for folder in ('accounts','real-client-data','real-client-runtime') for file in (root/folder).rglob('*')
            if file.is_file() and not file.name.endswith(('-wal','-shm','-journal'))}

    def test_full_snapshot_preserves_sources_results_selection_and_settings(self):
        original=self.hashes(self.root)
        result=self.backup_data()
        self.assertEqual(result['state'],'complete')
        manifest=inspect_backup(self.backup)
        paths={row['path'] for row in manifest['files']}
        self.assertTrue(any(path.endswith('/messages.sqlite') for path in paths))
        self.assertTrue(any(path.startswith('real-client-data/') and path.endswith('.sqlite3') for path in paths))
        self.assertIn('real-client-runtime/api-model-source.json',paths)
        self.assertEqual(manifest['uiPreferences'],self.preferences)
        self.assertGreater(manifest['counts']['labelCacheRows'],0)
        self.assertEqual(original,self.hashes(self.root),'creating a backup must not modify originals')

    def encrypted_fixture(self):
        from payload_crypto import PayloadCipher
        cipher=PayloadCipher(self.runtime/'analysis-key.bin')
        ResultStore(self.results.path,cipher=cipher)
        with closing(sqlite3.connect(self.results.path)) as conn:
            row=conn.execute('SELECT rowid,result FROM fine_results_v1 LIMIT 1').fetchone()
            self.assertIsNotNone(row)
            ciphertext=cipher.protect(row[1])
            conn.execute('UPDATE fine_results_v1 SET result=? WHERE rowid=?',(ciphertext,row[0]));conn.commit()
        (self.runtime/'analysis-workers.json').write_text('{"limit":2}',encoding='utf-8')
        return cipher,ciphertext

    def test_encrypted_backup_restores_key_results_and_pre_encryption_recovery(self):
        from payload_crypto import PayloadCipher
        cipher,ciphertext=self.encrypted_fixture()
        self.backup_data()
        manifest=inspect_backup(self.backup)
        self.assertTrue(manifest['encryptedAnalysis'])
        with closing(sqlite3.connect(self.results.path)) as conn:
            expected=sum(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                         for table in ('results_v2','fine_results_v1','api_insights_v1'))
        self.assertEqual(manifest['counts']['labelCacheRows'],expected,'recovery copies must not inflate active result counts')
        paths={row['path'] for row in manifest['files']}
        for name in ('analysis-key.bin','analysis-key.backup.bin','payload-format.json','analysis-workers.json'):
            self.assertIn('real-client-runtime/'+name,paths)
        self.assertTrue(any(path.endswith('.pre-wv1.sqlite3') for path in paths))
        self.assertTrue(any(path.endswith('.pre-wv1.json') for path in paths))
        target=self.fixture.root/'encrypted-restored/QQVibeData'
        restored=restore_backup(target,self.backup,digest(self.backup))
        self.assertEqual(restored['state'],'complete')
        restored_cipher=PayloadCipher(target/'real-client-runtime/analysis-key.bin')
        self.assertEqual(restored_cipher.unprotect(ciphertext),cipher.unprotect(ciphertext))
        with closing(sqlite3.connect(target/'real-client-data'/self.results.path.name)) as conn:
            self.assertEqual(conn.execute('SELECT result FROM fine_results_v1 LIMIT 1').fetchone()[0],ciphertext)

    def test_encrypted_foreign_user_restore_fails_before_swapping_any_data(self):
        self.encrypted_fixture();self.backup_data()
        target=self.copies('foreign-encrypted-target');before=self.hashes(target)
        with patch('qq_data_management.windows_user',return_value='different-windows-user'):
            with self.assertRaisesRegex(ValueError,'original-windows-user'):
                restore_backup(target,self.backup,digest(self.backup))
        self.assertEqual(self.hashes(target),before)

    def test_missing_key_backup_is_not_published_as_success(self):
        from payload_crypto import PayloadUnreadable
        self.encrypted_fixture()
        (self.runtime/'analysis-key.bin').unlink()
        with self.assertRaises(PayloadUnreadable):self.backup_data()
        self.assertFalse(self.backup.exists())

    def test_legacy_foreign_user_restore_clears_every_model_profile_credential(self):
        profiles=[{'id':'a'*32,'encryptedKey':'SYNTHETIC-A'},{'id':'b'*32,'encryptedKey':'SYNTHETIC-B'}]
        (self.runtime/'model-source.json').write_text(json.dumps({'version':2,'selectedMode':'api','profiles':profiles}),encoding='utf-8')
        self.backup_data();target=self.fixture.root/'legacy-foreign/QQVibeData'
        with patch('qq_data_management.windows_user',return_value='different-windows-user'):
            restored=restore_backup(target,self.backup,digest(self.backup))
        self.assertTrue(restored['credentialsRequireReentry'])
        value=json.loads((target/'real-client-runtime/model-source.json').read_text(encoding='utf-8'))
        self.assertEqual(value['selectedMode'],'local')
        self.assertEqual([row['encryptedKey'] for row in value['profiles']],[None,None])

    @unittest.skipUnless(os.name=='nt','Windows path length semantics')
    def test_long_restore_staging_paths_keep_logical_registry_and_result_counts(self):
        from qq_data_management import _validate_file
        self.encrypted_fixture();self.backup_data()
        padding=max(65,155-len(str(self.fixture.root))-len('/deep-/QQVibeData'))
        target=self.fixture.root/('deep-'+('x'*padding))/'QQVibeData'
        checked=[]
        def validate(file,relative):
            checked.append(str(file))
            return _validate_file(file,relative)
        with patch('qq_data_management._validate_file',side_effect=validate):
            restored=restore_backup(target,self.backup,digest(self.backup))
        self.assertEqual(restored['state'],'complete')
        self.assertEqual(restored['counts'],inspect_backup(self.backup)['counts'])
        self.assertTrue(any(len(path)>260 for path in checked),'the regression must exercise a long staging file')
        registry=json.loads((target/'real-client-data/accounts.json').read_text(encoding='utf-8'))
        self.assertFalse(registry['accounts'][0]['workdir'].startswith('\\\\?\\'))

    @unittest.skipUnless(os.name=='nt','Windows directory sharing semantics')
    def test_handoff_log_in_swapped_runtime_blocks_restore_but_external_log_does_not(self):
        self.backup_data();target=self.copies('native-log-target')
        inside=target/'real-client-runtime'/'data-restore-handoff.log'
        with inside.open('a',encoding='utf-8') as log:
            log.write('synthetic handoff start\n');log.flush()
            before=self.hashes(target)
            with self.assertRaises(PermissionError):
                restore_backup(target,self.backup,digest(self.backup),client_root=self.fixture.root/'client')
            self.assertEqual(self.hashes(target),before,'the failed native swap must restore every original file')
        outside=target/'data-restore-handoff';outside.mkdir()
        with (outside/'data-restore-handoff.log').open('a',encoding='utf-8') as log:
            log.write('synthetic handoff start\n');log.flush()
            restored=restore_backup(target,self.backup,digest(self.backup),client_root=self.fixture.root/'client')
            self.assertEqual(restored['state'],'complete')
            log.write('synthetic restore complete\n');log.flush()
        self.assertEqual(restored['counts'],inspect_backup(self.backup)['counts'])

    def test_restore_complete_copy_matches_message_and_label_rows_but_requires_connection_confirmation(self):
        self.backup_data();target=self.copies('restore-target/QQVibeData')
        original=self.hashes(self.root)
        with closing(sqlite3.connect(next((target/'accounts').glob('*/messages.sqlite')))) as conn:
            conn.execute("UPDATE messages SET text='合成目标副本被改动'");conn.commit()
        restored=restore_backup(target,self.backup,digest(self.backup),client_root=self.fixture.root/'client')
        self.assertEqual(restored['state'],'complete')
        from qq_account_store import QQAccountStore
        restored_store=QQAccountStore(target)
        identifier=restored_store._read_registry()[__import__('account_store').account_id(self.account)]['accountId']
        self.assertEqual(Path(restored_store.resolve(identifier)['workdir']),target/'accounts'/self.account[2:])
        from qq_source import QQSource
        from product_profile import load_product
        source=QQSource(root=target.parent,profile=load_product('qq'))
        try:
            source.attach(UIN)
            self.assertEqual(len(source.messages(CONV,20)),2,'a moved installation must open old messages offline')
        finally:source.close()
        self.assertTrue(Path(restored['rollbackDirectory']).is_dir())
        with closing(sqlite3.connect(next((target/'accounts').glob('*/messages.sqlite')))) as conn:
            self.assertEqual(conn.execute('SELECT text FROM messages ORDER BY time_ms').fetchall(),
                [('合成资料已经准备好了',),('谢谢你的帮助',)])
        copied_results=next((target/'real-client-data').glob('*.sqlite3'))
        with self.results.connect() as before,closing(sqlite3.connect(copied_results)) as after:
            for table in ('fine_results_v1','qq_analysis_generations_v1','conversation_selection_v1'):
                self.assertEqual(before.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall(),
                    after.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall())
        self.assertFalse(json.loads((target/'real-client-runtime/qq-connection.json').read_text())['enabled'])
        self.assertEqual(json.loads((target/'real-client-runtime/api-model-source.json').read_text())['selectedMode'],'local')
        self.assertEqual(self.hashes(self.root),original,'restore test must not touch original data')

    def test_restore_failure_after_each_directory_swap_rolls_back_every_old_byte(self):
        self.backup_data()
        for count in (1,2,3):
            target=self.copies('failure-'+str(count));before=self.hashes(target)
            with self.subTest(swap=count),self.assertRaisesRegex(OSError,'synthetic-restore-interruption'):
                restore_backup(target,self.backup,digest(self.backup),fail_after=count)
            self.assertEqual(before,self.hashes(target))
            self.assertEqual(json.loads(next(target.glob('.restore-*/journal.json')).read_text())['state'],'rolled-back')

    def test_changed_archive_after_preview_is_rejected_before_data_mutation(self):
        self.backup_data();target=self.copies('changed-archive');before=self.hashes(target)
        saved=digest(self.backup)
        with self.backup.open('ab') as stream:stream.write(b'SYNTHETIC_CHANGE')
        with self.assertRaisesRegex(ValueError,'backup-changed-after-preview'):
            restore_backup(target,self.backup,saved)
        self.assertEqual(before,self.hashes(target))

    def test_traversal_and_extra_archive_members_are_rejected(self):
        self.backup_data()
        corrupted=self.fixture.root/'corrupted.zip'
        with zipfile.ZipFile(self.backup) as source,zipfile.ZipFile(corrupted,'w') as target:
            for row in source.infolist():target.writestr(row,source.read(row))
            target.writestr('data/../OUTSIDE.txt','SYNTHETIC')
        with self.assertRaisesRegex(ValueError,'backup-members-invalid'):inspect_backup(corrupted)
        self.assertFalse((self.fixture.root/'OUTSIDE.txt').exists())

    def test_corrupt_sqlite_with_matching_archive_hash_does_not_replace_data(self):
        self.backup_data();manifest=inspect_backup(self.backup)
        broken=self.fixture.root/'broken-db.zip'
        library=next(row for row in manifest['files'] if row['path'].endswith('/messages.sqlite'))
        garbage=b'NOT_A_SQLITE_DATABASE'
        library.update(bytes=len(garbage),sha256=hashlib.sha256(garbage).hexdigest())
        with zipfile.ZipFile(self.backup) as original,zipfile.ZipFile(broken,'w') as archive:
            archive.writestr('manifest.json',json.dumps(manifest))
            for row in manifest['files']:
                archive.writestr('data/'+row['path'],garbage if row is library else original.read('data/'+row['path']))
        target=self.copies('invalid-db');before=self.hashes(target)
        with self.assertRaises((ValueError,sqlite3.DatabaseError)):restore_backup(target,broken,digest(broken))
        self.assertEqual(before,self.hashes(target))

    def test_other_windows_user_must_reenter_credentials(self):
        self.backup_data();target=self.copies('other-user')
        with patch('qq_data_management.windows_user',return_value='OTHER_WINDOWS_USER'):
            result=restore_backup(target,self.backup,digest(self.backup))
        self.assertTrue(result['credentialsRequireReentry'])
        self.assertIsNone(json.loads((target/'real-client-runtime/qq-connection.json').read_text())['bearer'])
        self.assertIsNone(json.loads((target/'real-client-runtime/api-model-source.json').read_text())['api']['encryptedKey'])

    def test_interrupted_swap_can_recover_without_a_live_helper(self):
        target=self.copies('hard-interruption');before=self.hashes(target)
        work=target/('.restore-'+'a'*32);(work/'prepared').mkdir(parents=True);(work/'rollback').mkdir()
        os.replace(target/'accounts',work/'rollback/accounts')
        (target/'accounts').mkdir()
        (target/'accounts/new.txt').write_text('SYNTHETIC_NEW_GENERATION')
        (work/'journal.json').write_text(json.dumps({'schema':'qqvibe-full-data-v1','state':'preparing','pid':-1,
            'originalExists':{folder:True for folder in ('accounts','real-client-data','real-client-runtime')}}))
        recover_pending_restores(target,process_alive=lambda _:False)
        self.assertEqual(before,self.hashes(target))
        self.assertTrue((work/'recovered-new/accounts/new.txt').exists())

    def test_live_restore_helper_blocks_bridge_startup_recovery(self):
        target=self.copies('live-helper');before=self.hashes(target)
        work=target/('.restore-'+'b'*32);work.mkdir()
        (work/'journal.json').write_text('{"schema":"qqvibe-full-data-v1","state":"preparing","pid":123}')
        with self.assertRaisesRegex(RuntimeError,'data-restore-in-progress'):
            recover_pending_restores(target,process_alive=lambda _:True)
        self.assertEqual(before,self.hashes(target))

    def test_stopping_read_keeps_selection_messages_and_labels(self):
        before=self.fixture.source.messages(CONV,10)
        labels=self.backend.analysis(CONV)['results']
        result=self.accounts.set_conversation_reading(self.account,CONV,False)
        self.assertFalse(result['readEnabled'])
        self.assertIn(CONV,self.backend.selection_store.get(self.account)['selectedSessions'])
        self.assertEqual(before,self.fixture.source.messages(CONV,10))
        self.assertEqual(labels,self.backend.analysis(CONV)['results'])
        self.accounts.set_conversation_reading(self.account,CONV,True)
        self.assertNotIn(CONV,self.backend.selection_store.paused_reading(self.account))

    def test_clear_one_conversation_revokes_its_stages_and_preserves_other_conversation(self):
        other='u:u_other_synth01'
        self.fixture.store.ensure_conversation(self.account,other,peer_uid='u_other_synth01')
        other_row=record('other1',time_ms=3000,text='其他会话应保留')
        other_row.update(conversation_key=other,sender_uid='u_other_synth01')
        raw=json.loads(other_row['raw']);raw['peerUid']='u_other_synth01';other_row['raw']=json.dumps(raw)
        self.fixture.store.ingest(self.account,other,[other_row])
        self.backend.set_conversation_selected(self.account,other,True)
        self.backend.start(other,'recent',1,expected_account=self.account)
        with self.backend.tasks.all_tasks_done:
            self.assertTrue(self.backend.tasks.all_tasks_done.wait_for(lambda:self.backend.tasks.unfinished_tasks==0,timeout=12))
        other_labels=self.backend.analysis(other)['results']
        result=self.accounts.clear_conversation(self.account,CONV)
        self.assertEqual(result['state'],'cleared')
        self.assertTrue(Path(result['recoveryBackup']).is_file())
        self.assertIsNone(self.fixture.store.revision(self.account,CONV))
        self.assertEqual(self.backend.analysis(other)['results'],other_labels)
        self.assertNotIn(CONV,self.backend.selection_store.get(self.account)['selectedSessions'])
        for path in self.fixture.store.path.parent.rglob('*.sqlite3'):
            with closing(sqlite3.connect(path)) as conn:
                tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'fine_results_v1' in tables:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM fine_results_v1 WHERE account=? AND session=?',(self.account,CONV)).fetchone()[0],0)
        with self.results.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM qq_analysis_generations_v1 WHERE account=? AND session=?',(self.account,CONV)).fetchone()[0],0)

    def test_clear_source_delete_failure_rolls_back_published_and_staged_analysis(self):
        labels=self.backend.analysis(CONV)['results']
        before=self.fixture.source.messages(CONV,10)
        original_backup=self.accounts.backup_data
        def backup_then_fail(*args,**kwargs):
            result=original_backup(*args,**kwargs)
            with self.fixture.store.connection:
                self.fixture.store.connection.execute("CREATE TRIGGER fail_clear BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT,'SYNTHETIC_CLEAR_FAIL'); END")
            return result
        with patch.object(self.accounts,'backup_data',side_effect=backup_then_fail),self.assertRaisesRegex(sqlite3.DatabaseError,'SYNTHETIC_CLEAR_FAIL'):
            self.accounts.clear_conversation(self.account,CONV)
        with self.fixture.store.connection:self.fixture.store.connection.execute('DROP TRIGGER fail_clear')
        self.assertEqual(before,self.fixture.source.messages(CONV,10))
        self.assertEqual(labels,self.backend.analysis(CONV)['results'])
        self.backend.start(CONV,'recent',2,expected_account=self.account)
        with self.backend.tasks.all_tasks_done:
            self.assertTrue(self.backend.tasks.all_tasks_done.wait_for(lambda:self.backend.tasks.unfinished_tasks==0,timeout=12))
        self.assertEqual(labels,self.backend.analysis(CONV)['results'])


if __name__=='__main__':unittest.main()
