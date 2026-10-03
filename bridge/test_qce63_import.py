"""QCE 6.3.0/6.3.1 CleanMessage-shaped synthetic imports, no real chats."""
import copy
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta

import qq_json_adapter as adapter
from qq_normalize import NormalizationError, _export_timestamp, read_export
import test_qq_import as import_fixture


class QCE63ImportTests(unittest.TestCase):
    def setUp(self):
        self.fx = import_fixture.ImportTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def modern(self, version='6.3.0'):
        doc = copy.deepcopy(self.fx.document)
        doc['metadata'].update(name='QQChatExporter', version=version)
        for index, row in enumerate(doc['messages']):
            row['id'] = row.pop('messageId')
            row['seq'] = str(index + 1)
            row['timestamp'] = _export_timestamp(row['timestamp'])
            row['time'] = datetime.fromtimestamp(row['timestamp']/1000, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
            row['type'] = 'text'
            row.pop('messageType')
        return doc

    def preview(self, doc, mapping=None):
        self.fx.path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        return self.fx.wait(self.fx.imports.start(str(self.fx.path), None, mapping))

    def records(self):
        with closing(sqlite3.connect(self.fx.imports.job['database'])) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT record FROM records ORDER BY idx')]

    def test_both_versions_auto_preview_without_field_mapping(self):
        for version in ('6.3.0', '6.3.1'):
            with self.subTest(version=version):
                doc=self.modern(version)
                result=self.preview(doc)
                self.assertEqual(result['state'],'ready',result)
                self.assertEqual(result['preview']['counts']['rowsRejected'],0,result)
                rows=self.records()
                self.assertEqual([row['time_ms'] for row in rows],[row['timestamp'] for row in doc['messages']])
                self.assertEqual([row['native_id'] for row in rows],[row['id'] for row in doc['messages']])
                self.assertEqual(rows[0]['quote'],'几点见？')
                self.assertEqual(rows[0]['text'],'明天见，九点。')
                self.assertFalse(self.fx.fixture.data_root.exists(),'preview must not write the chat library')

    def test_commit_and_repeat_across_exporter_versions_preserve_ids(self):
        first=self.fx.commit(self.preview(self.modern('6.3.0')))
        self.assertEqual(first['result']['inserted'],2,first)
        second=self.fx.commit(self.preview(self.modern('6.3.1')))
        self.assertEqual(second['result']['unchanged'],2,second)
        self.assertEqual(second['result']['inserted'],0,second)
        self.fx.api.activate(first['result']['accountId'])
        readback=self.fx.fixture.source.messages(first['result']['conversationKey'],80)
        self.assertEqual([row['text'] for row in readback],['明天见，九点。','好'])

    def test_manifest_jsonl_uses_the_same_exact_clock(self):
        self.fx.document=self.modern('6.3.1')
        expected=[row['timestamp'] for row in self.fx.document['messages']]
        self.fx.chunks()
        result=self.fx.preview()
        self.assertEqual(result['state'],'ready',result)
        self.assertEqual(result['format'],'qce-chunked-jsonl')
        self.assertEqual([row['time_ms'] for row in self.records()],expected)
        self.assertEqual(self.fx.commit(result)['result']['inserted'],2)

    def test_group_export_uses_new_ids_and_millisecond_clock(self):
        doc=self.modern('6.3.1')
        doc['chatInfo'].update(type='group',peerUid='20001',name='合成群')
        doc['chatInfo'].pop('peerUin',None)
        result=self.preview(doc)
        self.assertEqual(result['state'],'ready',result)
        self.assertEqual(result['preview']['conversationKey'],'g:20001')
        self.assertEqual(self.records()[0]['time_ms'],doc['messages'][0]['timestamp'])
        self.assertEqual(self.fx.commit(result)['result']['inserted'],2)

    def test_display_date_does_not_replace_canonical_timestamp(self):
        doc=self.modern()
        doc['messages'][0]['time']='昨日'
        result=self.preview(doc)
        self.assertEqual(result['state'],'ready',result)
        self.assertEqual(self.records()[0]['time_ms'],doc['messages'][0]['timestamp'])

    def test_explicit_timestamp_mapping_and_units_remain_supported(self):
        for mapping in ({'time':'timestamp'}, {'time':'/timestamp'}, {'time':'timestamp','timeUnit':'milliseconds'}):
            with self.subTest(mapping=mapping):
                doc=self.modern()
                result=self.preview(doc,mapping)
                self.assertEqual(result['state'],'ready',result)
                self.assertEqual(self.records()[0]['time_ms'],doc['messages'][0]['timestamp'])
                self.assertEqual(self.records()[0]['native_id'],doc['messages'][0]['id'])

    def test_mapping_only_unrelated_field_keeps_qce_clock_rule(self):
        doc=self.modern()
        result=self.preview(doc,{'peerUid':'u_synthetic_peer'})
        self.assertEqual(result['state'],'ready',result)
        self.assertEqual(result['schema']['rejections'],{})
        self.assertEqual(self.records()[0]['time_ms'],doc['messages'][0]['timestamp'])

    def test_explicit_display_time_override_is_respected(self):
        doc=self.modern()
        result=self.preview(doc,{'time':'time','timeZone':'+08:00'})
        self.assertEqual(result['state'],'ready',result)
        self.assertEqual(self.records()[0]['time_ms'],doc['messages'][0]['timestamp']//1000*1000)
        missing_zone=self.preview(doc,{'time':'time'})
        self.assertEqual(missing_zone['schema']['rejections'],{'time-zone-required':2})

    def test_milliseconds_are_source_defined_even_near_epoch(self):
        doc=self.modern()
        doc['messages'][0]['timestamp']=1234
        for mapping in (None, {'peerUid':'u_synthetic_peer'}):
            with self.subTest(mapping=mapping):
                result=self.preview(doc,mapping)
                self.assertEqual(result['state'],'ready',result)
                self.assertEqual(self.records()[0]['time_ms'],1234)

    def test_invalid_canonical_clock_is_rejected_without_display_fallback(self):
        for value in (-1, True, 1.25, 10**30):
            with self.subTest(value=value):
                doc=self.modern();doc['messages'][0]['timestamp']=value
                result=self.preview(doc)
                self.assertEqual(result['preview']['counts']['rowsRejected'],1,result)
                self.assertEqual(result['preview']['uniqueMessages'],1,result)

    def test_media_and_unknown_types_do_not_become_authored_text(self):
        for kind in ('file','video','audio','forward','json','type_77'):
            for mapping in (None, {'time':'timestamp'}):
                with self.subTest(kind=kind,mapping=mapping):
                    doc=self.modern();doc['messages'][0]['type']=kind
                    doc['messages'][0]['content']['text']='[媒体显示预览，不是发言]'
                    result=self.preview(doc,mapping)
                    self.assertEqual(result['state'],'ready',result)
                    self.assertIsNone(self.records()[0]['text'])

    def test_invalid_clock_stays_invalid_after_mapping_other_fields(self):
        for value in (True,1.25,-1):
            with self.subTest(value=value):
                doc=self.modern();doc['messages'][0]['timestamp']=value
                result=self.preview(doc,{'peerUid':'u_synthetic_peer'})
                self.assertEqual(result['preview']['counts']['rowsRejected'],1,result)

    def test_legacy_iso_rows_are_unchanged(self):
        result=self.preview(self.fx.document)
        self.assertEqual(result['state'],'ready',result)
        self.assertEqual(self.records(),read_export(self.fx.path)['records'])

    def test_unknown_json_keeps_time_conflict_and_manual_override(self):
        row={'timestamp':1700000000123,'time':'2023-11-15 06:13:20'}
        with self.assertRaisesRegex(NormalizationError,'ambiguous-time-field'):
            adapter.field(row,'time',{})
        self.assertEqual(adapter.field(row,'time',{'time':'timestamp'}),1700000000123)


if __name__=='__main__':unittest.main(verbosity=2)
