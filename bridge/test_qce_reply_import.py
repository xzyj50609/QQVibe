"""Synthetic QCE reply regressions through normalization, real import and analysis."""
import copy
import json
import unittest
from unittest.mock import patch

from qq_normalize import normalize_export_row
import test_qq_import as import_fixture
from test_qq_analysis_revision import Analyzer


def reply_row(kind='reply'):
    return {'id': 'm2', 'timestamp': 1789883900000, 'type': kind,
            'system': False, 'recalled': False,
            'sender': {'uin': '10002', 'uid': 'u_peer', 'name': 'B'},
            'content': {'text': '[回复消息]哈哈哈', 'elements': [
                {'type': 'reply', 'data': {'content': '上一句', 'senderUin': '10001'}},
                {'type': 'text', 'data': {'text': '哈哈哈'}}]}}


class NormalizeReplyTests(unittest.TestCase):
    def test_reply_and_other_outer_types_use_only_structured_authored_text(self):
        for kind in ('reply', 'type_19', 'type_77', 'text', 'image'):
            with self.subTest(kind=kind):
                row = reply_row(kind)
                row['content']['elements'].extend([
                    {'type': 'image', 'data': {'url': 'https://example.invalid/photo.png'}},
                    {'type': 'text', 'data': {'text': '！'}}])
                record, flags = normalize_export_row(row, '10001', 'u_peer')
                self.assertEqual((record['kind'], record['text'], record['quote']),
                                 ('text', '哈哈哈！', '上一句'))
                self.assertFalse(flags['unknownType'])

    def test_numeric_outer_message_type_cannot_hide_author_text(self):
        row = reply_row(); row['messageType'] = 9
        record, _ = normalize_export_row(row, '10001', 'u_peer')
        self.assertEqual((record['kind'], record['text']), ('text', '哈哈哈'))

    def test_pure_media_quote_and_empty_elements_never_use_preview(self):
        for kind in ('reply', 'type_19', 'image', 'file', 'audio', 'forward', 'text'):
            for elements in ([], [{'type': 'reply', 'data': {'content': '上一句'}}],
                             [{'type': 'image', 'data': {'url': 'https://example.invalid/a.png'}}],
                             [{'type': 'forward', 'data': {'messages': [reply_row()]}}]):
                with self.subTest(kind=kind, elements=elements):
                    row = reply_row(kind); row['content']['elements'] = elements
                    record, flags = normalize_export_row(row, '10001', 'u_peer')
                    self.assertEqual(record['kind'], 'unknown')
                    self.assertIsNone(record['text'])
                    self.assertTrue(flags['unknownType'])


class ReplyImportTests(unittest.TestCase):
    def setUp(self):
        self.fx = import_fixture.ImportTests(); self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def document(self, version='0.1.0', kind='reply'):
        doc = copy.deepcopy(self.fx.document)
        doc['metadata'].update(name='QQChatExporter', version=version, messageCount=2)
        doc['chatInfo'].update(peerUid='u_peer', peerUin='10002')
        own = reply_row('text')
        own.update(id='m3', timestamp=1789883901000)
        own['sender'].update(uin='10001', uid='u_self')
        own['content'] = {'text': '好', 'elements': [{'type': 'text', 'data': {'text': '好'}}]}
        doc['messages'] = [reply_row(kind), own]
        return doc

    def preview(self, doc, mapping=None):
        self.fx.path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        result = self.fx.wait(self.fx.imports.start(str(self.fx.path), None, mapping))
        self.assertEqual(result['state'], 'ready', result)
        return result

    def test_real_import_task_handles_legacy_and_qce6_without_duplicates(self):
        for version in ('0.1.0', '6.3.0', '6.3.1'):
            for kind in ('reply', 'type_19'):
                with self.subTest(version=version, kind=kind):
                    preview = self.preview(self.document(version, kind))
                    self.assertEqual(preview['preview']['counts']['unknownTypes'], 0)
                    result = self.fx.commit(preview)
                    self.assertEqual(result['state'], 'complete', result)
                    self.fx.api.activate(result['result']['accountId'])
                    source = self.fx.fixture.source
                    rows = source.messages(result['result']['conversationKey'], 80)
                    self.assertEqual((rows[0]['kind'], rows[0]['text'], rows[0]['qqDisplay']['quoteText']),
                                     ('text', '哈哈哈', '上一句'))
                    repeated = self.fx.commit(self.preview(self.document(version, kind)))
                    self.assertEqual(repeated['result']['inserted'], 0)
                    self.assertEqual(repeated['result']['unchanged'], 2)

    def test_manual_sender_mapping_never_promotes_media_previews(self):
        import sqlite3
        from contextlib import closing
        for kind in ('reply', 'type_19', 'forward', 'file', 'image', 'audio', 'json'):
            with self.subTest(kind=kind):
                doc = self.document('6.3.0', kind)
                doc['messages'][0]['content'].pop('elements')
                result = self.preview(doc, {'sender': 'sender.uin', 'senderName': 'sender.name'})
                self.assertEqual(result['preview']['counts']['rowsRejected'], 0, result)
                with closing(sqlite3.connect(self.fx.imports.job['database'])) as db:
                    record = json.loads(db.execute('SELECT record FROM records ORDER BY idx LIMIT 1').fetchone()[0])
                self.assertEqual(record['kind'], 'unknown')
                self.assertIsNone(record['text'])

    def seed_legacy(self, doc):
        # Preserve the exact old bug while still using the real asynchronous import.
        import qq_import
        original = qq_import.normalize_export_row
        def old_normalizer(*args, **kwargs):
            record, flags = original(*args, **kwargs)
            if args[0].get('type') != 'text' and 'messageType' not in args[0]:
                record.update(kind='unknown', text=None, msg_type=None)
                flags['unknownType'] = True
            record['normalize_version'] = 'qq-v3:qce-export-v6'
            return record, flags
        with patch.object(qq_import, 'normalize_export_row', old_normalizer):
            result = self.fx.commit(self.preview(doc))
        self.fx.api.activate(result['result']['accountId'])
        return result['result']

    def test_reimport_repairs_old_unknown_preserves_keys_and_rebuilds_analysis(self):
        doc = self.document()
        first = self.seed_legacy(doc)
        source, backend = self.fx.fixture.source, self.fx.fixture.backend
        library = source._store
        account, conv = first['account'], first['conversationKey']
        old = dict(library.connection.execute('SELECT * FROM messages WHERE native_id=?', ('m2',)).fetchone())
        revision = library.revision(account, conv)
        analyzer = Analyzer(); backend.analyzer = analyzer
        from batch_engine import BatchEngine
        backend.batch_engine = BatchEngine(backend)
        from profile_signals import keyword_counts
        keyword_counts(['合成初始化'])
        def analyze(mode):
            job = backend.start(conv, mode, 80 if mode == 'recent' else None, expected_account=account)
            with backend.tasks.all_tasks_done:
                self.assertTrue(backend.tasks.all_tasks_done.wait_for(lambda: backend.tasks.unfinished_tasks == 0, timeout=15))
            completed = next(row for row in backend.jobs.values() if row['id'] == job['id'])
            self.assertEqual(completed['status'], 'done', completed)
        analyze('incremental')
        self.assertEqual(backend.profile(conv)['stats']['analyzedCount'], 0)
        result = self.fx.commit(self.preview(doc))
        self.assertEqual((result['result']['inserted'], result['result']['conflicts']), (0, 0), result)
        new = dict(library.connection.execute('SELECT * FROM messages WHERE native_id=?', ('m2',)).fetchone())
        self.assertEqual((new['kind'], new['text'], new['quote'], new['status']), ('text', '哈哈哈', '上一句', 'normal'))
        for field in ('message_key', 'local_seq', 'native_id', 'time_ms', 'raw'):
            self.assertEqual(new[field], old[field], field)
        self.assertGreater(library.revision(account, conv)[0], revision[0])
        self.assertTrue(backend.analysis(conv)['stale'])
        analyze('recent'); analyze('incremental')
        self.assertIn('m2', backend.analysis(conv)['results'])
        self.assertEqual(backend.profile(conv)['stats']['analyzedCount'], 1)
        self.assertTrue(any(item['text'] == '哈哈哈' for payload, _ in analyzer.calls for item in payload))
        stable = library.revision(account, conv)
        calls = len(analyzer.calls)
        repeat = self.fx.commit(self.preview(doc))
        self.assertEqual((repeat['result']['inserted'], repeat['result']['unchanged']), (0, 2))
        self.assertEqual(library.revision(account, conv), stable)
        analyze('incremental')
        self.assertEqual(len(analyzer.calls), calls)
        self.assertEqual(library.connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)

    def test_changed_raw_or_identity_still_conflicts_and_never_overwrites_old_row(self):
        doc = self.document(); first = self.seed_legacy(doc)
        doc['messages'][0]['content']['elements'][1]['data']['text'] = '另一条内容'
        result = self.fx.commit(self.preview(doc))
        self.assertEqual(result['result']['inserted'], 0)
        self.assertEqual(result['result']['conflicts'], 1)
        row = self.fx.fixture.source._store.connection.execute('SELECT * FROM messages WHERE native_id=?', ('m2',)).fetchone()
        self.assertEqual((row['kind'], row['text'], row['status']), ('unknown', None, 'conflict'))

    def test_recalled_reply_remains_recalled_after_text_repair(self):
        doc = self.document(); doc['messages'][0]['recalled'] = True
        first = self.seed_legacy(doc)
        result = self.fx.commit(self.preview(doc))
        self.assertEqual((result['result']['revised'], result['result']['conflicts']), (1, 0))
        source = self.fx.fixture.source
        row = source._store.connection.execute('SELECT * FROM messages WHERE native_id=?', ('m2',)).fetchone()
        self.assertEqual((row['text'], row['quote'], row['status']), ('哈哈哈', '上一句', 'recalled'))
        self.assertEqual(source._store.target_counts(first['account'], first['conversationKey'], direction='peer'), (1, 0))

    def test_upgrade_rejects_changed_identity_quote_or_forged_body(self):
        from qq_message_store import _qce_text_upgrade
        from qq_normalize import QCE_EXPORT_NORMALIZE_VERSION
        doc = self.document(); self.seed_legacy(doc)
        old = dict(self.fx.fixture.source._store.connection.execute('SELECT * FROM messages WHERE native_id=?', ('m2',)).fetchone())
        fixed = {**old, 'kind': 'text', 'text': '哈哈哈', 'msg_type': 2,
                 'normalize_version': QCE_EXPORT_NORMALIZE_VERSION}
        self.assertTrue(_qce_text_upgrade(old, fixed))
        for change in ({'sender_uin': '10003'}, {'time_ms': old['time_ms'] + 1},
                       {'quote': '别的引用'}, {'text': '伪造正文'}, {'status': 'normal', 'sender_uid': 'u_else'},
                       {'normalize_version': 'qq-v99'}, {'raw': None}):
            with self.subTest(change=change):
                self.assertFalse(_qce_text_upgrade(old, {**fixed, **change}))
        self.assertFalse(_qce_text_upgrade({**old, 'status': 'conflict'}, {**fixed, 'status': 'conflict'}))


if __name__ == '__main__': unittest.main(verbosity=2)
