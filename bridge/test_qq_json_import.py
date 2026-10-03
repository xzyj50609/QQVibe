"""Non-QCE inputs through the actual preview, identity, commit and restore path."""
import copy
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

import qq_ingest_audit as audit
import qq_json_adapter as adapter
from qq_import_reader import TOKEN_LIMIT
from qq_normalize import NormalizationError
import test_qq_import as import_fixture
from test_qq_groups import export_document


class JsonImportTests(unittest.TestCase):
    def setUp(self):
        self.fx = import_fixture.ImportTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def run_preview(self, document, mapping=None, owner='10001', suffix='.json'):
        self.fx.path = self.fx.path.with_suffix(suffix)
        self.fx.path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
        return self.fx.wait(self.fx.imports.start(str(self.fx.path), owner, mapping))

    def records(self):
        with closing(sqlite3.connect(self.fx.imports.job['database'])) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT record FROM records ORDER BY idx')]

    @staticmethod
    def rows():
        return [{'id': '9007199254740993', 'sender_id': '10002', 'sender_uid': 'u_synthetic_peer',
                 'timestamp': 1700000000123, 'text': '合成正文'},
                {'id': '9007199254740994', 'sender_id': '10001', 'sender_uid': 'u_synthetic_self',
                 'timestamp': 1700000001123, 'text': '<script>仅显示文本</script>'}]

    def assert_ready(self, job, count=2):
        self.assertEqual(job['state'], 'ready', job)
        self.assertEqual(job['preview']['counts']['rowsOk'], count, job)

    def test_top_array_preserves_body_large_ids_time_and_deduplicates(self):
        job = self.run_preview(self.rows())
        self.assert_ready(job)
        rows = self.records()
        self.assertEqual(rows[0]['native_id'], '9007199254740993')
        self.assertEqual(rows[0]['time_ms'], 1700000000123)
        self.assertEqual(rows[1]['text'], '<script>仅显示文本</script>')
        self.assertFalse(self.fx.fixture.data_root.exists())
        first = self.fx.commit(job)
        self.assertEqual(first['state'], 'complete', first)
        again = self.fx.commit(self.run_preview(self.rows()))
        self.assertEqual(again['result']['unchanged'], 2, again)
        self.fx.api.activate(first['result']['accountId'])
        library = self.fx.fixture.source._store
        self.assertEqual(library.connection.execute('SELECT source_format FROM qq_ingest_runs_v1 LIMIT 1').fetchone()[0], 'generic-json')
        audit.validate_database(library.connection)

    def test_nested_messages_with_unknown_metadata_are_detected(self):
        job = self.run_preview({'data': {'messages': self.rows()}, 'status': 'ok'})
        self.assert_ready(job)
        self.assertEqual(job['schema']['arrayPaths'], ['/data/messages'])

    def test_multiple_arrays_require_choice_and_do_not_combine(self):
        doc = {'messages': self.rows(), 'records': self.rows()}
        pending = self.run_preview(doc)
        self.assertEqual((pending['state'], pending['reason']), ('mapping', 'multiple-message-arrays'), pending)
        self.assertFalse(self.fx.fixture.data_root.exists())
        selected = self.run_preview(doc, {'recordPath': '/records'})
        self.assert_ready(selected)

    def test_unknown_array_offers_path_and_custom_fields_are_explicit(self):
        doc = {'payload': {'entries': [{'who': '10002', 'when': '2026-10-02T01:02:03Z',
                    'words': '保留内容', 'key': 'native-1'}]}}
        pending = self.run_preview(doc)
        self.assertEqual(pending['state'], 'mapping', pending)
        self.assertIn('/payload/entries', pending['schema']['arrayPaths'])
        job = self.run_preview(doc, {'recordPath': '/payload/entries', 'sender': 'who',
            'time': 'when', 'text': 'words', 'id': 'key', 'peerUid': 'u_synthetic_peer'})
        self.assert_ready(job, 1)
        self.assertEqual(self.records()[0]['text'], '保留内容')

    def test_missing_ids_preserve_identical_rows_but_repeat_file_is_idempotent(self):
        row = self.rows()[0]; del row['id']
        job = self.run_preview([row, row])
        self.assert_ready(job)
        self.assertEqual(job['schema']['generatedIds'], 2)
        self.assertEqual(job['preview']['uniqueMessages'], 2)
        first = self.fx.commit(job)
        self.assertEqual(first['result']['inserted'], 2, first)
        second = self.fx.commit(self.run_preview([row, row]))
        self.assertEqual(second['result']['unchanged'], 2, second)

    def test_jsonl_handles_valid_and_broken_lines_with_explicit_partial_choice(self):
        self.fx.path = self.fx.path.with_suffix('.jsonl')
        self.fx.path.write_text('\n'.join([json.dumps(self.rows()[0]), '{broken', json.dumps(self.rows()[1])]), encoding='utf-8')
        job = self.fx.wait(self.fx.imports.start(str(self.fx.path), '10001'))
        self.assert_ready(job)
        self.assertEqual(job['preview']['counts']['rowsRejected'], 1)
        self.assertEqual(job['format'], 'generic-jsonl')
        with self.assertRaisesRegex(ValueError, 'partial-import-confirmation-required'):
            self.fx.commit(job)
        self.assertEqual(self.fx.commit(job, True)['state'], 'complete')

    def test_jsonl_with_json_extension_is_detected_by_content(self):
        self.fx.path.write_text('\n'.join(json.dumps(row) for row in self.rows()), encoding='utf-8-sig')
        job = self.fx.wait(self.fx.imports.start(str(self.fx.path), '10001'))
        self.assert_ready(job)
        self.assertEqual(job['format'], 'generic-jsonl')

    def test_bom_marked_unicode_exports_preserve_chinese(self):
        for encoding in ('utf-8-sig', 'utf-16', 'utf-32'):
            with self.subTest(encoding=encoding):
                self.fx.path.write_text(json.dumps(self.rows(), ensure_ascii=False), encoding=encoding)
                job = self.fx.wait(self.fx.imports.start(str(self.fx.path), '10001'))
                self.assert_ready(job)
                self.assertEqual(self.records()[0]['text'], '合成正文')

    def test_onebot_segments_keep_only_authored_text_and_group_scope(self):
        row = {'message_id': 12345678901234567890, 'time': 1700000000, 'group_id': 20001,
               'user_id': 10002, 'sender': {'user_id': 10002, 'nickname': '甲'},
               'message': [{'type': 'text', 'data': {'text': '正文'}},
                           {'type': 'image', 'data': {'file': 'private-image-path'}}]}
        job = self.run_preview([row])
        self.assert_ready(job, 1)
        self.assertEqual(job['preview']['conversationKey'], 'g:20001')
        record = self.records()[0]
        self.assertEqual((record['text'], record['native_id_kind']), ('正文', 'onebot-message-id'))

    def test_raw_napcat_preserves_recall_quote_and_native_identity(self):
        from test_qq_export import GOLDEN
        row = copy.deepcopy(GOLDEN['raw']); row['recallTime'] = '1700000002'
        job = self.run_preview({'data': {'messages': [row]}})
        self.assert_ready(job, 1)
        record = self.records()[0]
        self.assertEqual(record['status'], 'recalled')
        self.assertEqual(record['native_id_kind'], 'qce-msgId')
        self.assertEqual(record['quote'], GOLDEN['expected']['quote'])

    def test_chatlab_qq_group_metadata_and_content(self):
        doc = {'chatlab': {'version': '0.0.1'}, 'meta': {'platform': 'qq', 'type': 'group', 'groupId': '20001', 'name': '合成群'},
               'messages': [{'sender': '10002', 'timestamp': 1700000000, 'type': 0, 'content': '正文'}]}
        job = self.run_preview(doc)
        self.assert_ready(job, 1)
        self.assertEqual(job['preview']['name'], '合成群')
        self.assertEqual(self.fx.commit(job)['state'], 'complete')

    def test_chatlab_jsonl_header_members_and_media_are_not_chat_text(self):
        lines = [{'_type': 'header', 'chatlab': {'version': '0.0.2'},
                  'meta': {'platform': 'qq', 'type': 'group', 'groupId': '20001'}},
                 {'_type': 'member', 'platformId': '10002', 'accountName': '甲'},
                 {'_type': 'message', 'sender': '10002', 'timestamp': 1700000000,
                  'platformMessageId': '123', 'type': 0, 'content': '正文'},
                 {'_type': 'message', 'sender': '10002', 'timestamp': 1700000001,
                  'platformMessageId': '124', 'type': 1, 'content': 'private-image-path'}]
        self.fx.path = self.fx.path.with_suffix('.jsonl')
        self.fx.path.write_text('\n'.join(json.dumps(row) for row in lines), encoding='utf-8')
        job = self.fx.wait(self.fx.imports.start(str(self.fx.path), '10001'))
        self.assert_ready(job)
        self.assertEqual(job['preview']['counts']['unknownTypes'], 1)
        records = self.records()
        self.assertEqual(records[0]['native_id'], '123')
        self.assertEqual(records[0]['text'], '正文')
        self.assertIsNone(records[1]['text'])
        self.assertIn('private-image-path', records[1]['raw'])

    def test_non_qq_platform_is_not_misrepresented_as_qq(self):
        job = self.run_preview({'meta': {'platform': 'wechat'}, 'messages': self.rows()})
        self.assertEqual(job['error'], 'non-qq-export', job)

    def test_old_qce_keeps_real_ids_and_owner_confirmation(self):
        doc = copy.deepcopy(self.fx.document)
        doc['metadata']['version'] = '4.9.0'
        del doc['chatInfo']['selfUin']
        job = self.run_preview(doc, owner=None)
        self.assertEqual((job['state'], job['reason']), ('identity', 'owner-required'), job)
        job = self.fx.wait(self.fx.imports.map_owner(job['jobId'], '10001'))
        self.assert_ready(job)
        self.assertEqual(self.records()[0]['native_id_kind'], 'qce-msgId')

    def test_local_dates_need_explicit_timezone_and_names_need_mapping(self):
        rows = [{'sender': '小明', 'time': '2026-10-02 08:00:00', 'text': '正文'}]
        job = self.run_preview(rows, {'peerUid': 'u_synthetic_peer'})
        self.assertEqual(job['schema']['senders'], ['小明'])
        mapped = self.run_preview(rows, {'peerUid': 'u_synthetic_peer', 'senders': {'小明': '10002'}})
        self.assertEqual(mapped['schema']['rejections'], {'time-zone-required': 1}, mapped)
        ready = self.run_preview(rows, {'peerUid': 'u_synthetic_peer', 'senders': {'小明': '10002'}, 'timeZone': '+08:00'})
        self.assert_ready(ready, 1)
        self.assertEqual(self.records()[0]['time_ms'], 1790899200000)

    def test_ambiguous_fields_do_not_silently_choose_the_wrong_body(self):
        rows = self.rows(); rows[0]['content'] = '另一份内容'
        job = self.run_preview(rows)
        self.assertEqual(job['schema']['rejections'], {'ambiguous-text-field': 1}, job)
        fixed = self.run_preview(rows, {'text': 'text'})
        self.assert_ready(fixed)

    def test_multiple_declared_groups_are_rejected_even_with_manual_target(self):
        rows = self.rows()
        for row, group in zip(rows, [20001, 20002]): row['group_id'] = group
        job = self.run_preview(rows, {'kind': 'group', 'groupCode': '20001'})
        self.assertEqual(job['error'], 'multiple-conversations', job)

    def test_group_code_identity_can_be_supplied_after_owner(self):
        doc = export_document(); del doc['chatInfo']['peerUid']
        job = self.run_preview(doc)
        self.assertEqual(job['reason'], 'group-code-required', job)
        job = self.fx.wait(self.fx.imports.map_owner(job['jobId'], '10001', '20001'))
        self.assert_ready(job, 6)

    def test_large_unused_avatars_do_not_break_valid_messages(self):
        doc = copy.deepcopy(self.fx.document)
        doc['avatars'] = {'large': 'a' * (TOKEN_LIMIT + 100)}
        job = self.run_preview(doc)
        self.assert_ready(job)

    def test_explicit_budgets_cannot_be_bypassed_by_fallback(self):
        self.fx.imports.max_rows = 1
        job = self.run_preview(self.rows())
        self.assertEqual(job['error'], 'row-budget-exceeded')
        self.fx.imports.max_rows = 2000000; self.fx.imports.max_bytes = 20
        self.assertEqual(self.run_preview(self.rows())['error'], 'byte-budget-exceeded')

    def test_bad_json_and_no_chat_data_never_produce_success(self):
        self.fx.path.write_text('{"data":{"messages":[{"text":"cut off', encoding='utf-8')
        job = self.fx.wait(self.fx.imports.start(str(self.fx.path), '10001'))
        self.assertEqual(job['error'], 'invalid-export-json')
        self.assertEqual(self.run_preview({'settings': True})['state'], 'mapping')
        self.assertFalse(self.fx.fixture.data_root.exists())

    def test_mapping_is_strict_and_does_not_execute_expressions(self):
        for options in ({'unknown': True}, {'timeZone': '+14:59'}, {'recordPath': 'not-a-pointer'}, {'senders': {'甲': 'not-a-qq'}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.fx.imports.start(str(self.fx.path), mapping=options)
        self.assertIsNone(adapter.get({'a': 'safe'}, '__import__("os").system("anything")'))

    def test_timestamp_units_and_timezone_are_not_assumed(self):
        self.assertEqual(adapter.timestamp(1700000000, {}), adapter.timestamp(1700000000000, {}))
        with self.assertRaisesRegex(NormalizationError, 'time-unit-ambiguous'):
            adapter.timestamp(170000000000, {})
        with self.assertRaisesRegex(NormalizationError, 'time-zone-required'):
            adapter.timestamp('2026-01-02 03:04:05', {})

    def test_legacy_audit_upgrade_preserves_all_rows_and_backup(self):
        first = self.fx.commit(self.fx.preview())
        self.fx.api.activate(first['result']['accountId'])
        library = self.fx.fixture.source._store
        # Build the exact released auxiliary schema, retaining current data.
        with library.transaction() as cursor:
            with patch.object(audit, 'DDL', audit.LEGACY_DDL):
                audit.upgrade_formats(cursor)
        before = {table: [tuple(row) for row in library.connection.execute('SELECT * FROM ' + table)]
                  for table in ('qq_ingest_runs_v1', 'qq_ingest_observations_v1', 'qq_ingest_message_sources_v1')}
        self.assertTrue(audit.needs_format_upgrade(library.connection))
        library.ensure_ingest_audit()
        self.assertFalse(audit.needs_format_upgrade(library.connection))
        for table, rows in before.items():
            self.assertEqual([tuple(row) for row in library.connection.execute('SELECT * FROM ' + table)], rows)
        self.assertTrue(list(library.path.parent.glob('*.json-import-backup-*')))
        audit.validate_database(library.connection)

    def test_failed_audit_upgrade_rolls_back_children_and_parent(self):
        first = self.fx.commit(self.fx.preview())
        self.fx.api.activate(first['result']['accountId'])
        library = self.fx.fixture.source._store
        before = list(library.connection.iterdump())
        with self.assertRaises(sqlite3.OperationalError):
            with library.transaction() as cursor, patch.object(audit, 'DDL', (*audit.DDL, 'INVALID SQL')):
                audit.upgrade_formats(cursor)
        self.assertEqual(list(library.connection.iterdump()), before)
        audit.validate_database(library.connection)


if __name__ == '__main__':
    unittest.main()
