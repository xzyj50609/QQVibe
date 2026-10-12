"""Explicit V06/V08/V17 cases; temporary exports and actual Windows junctions."""
import calendar
import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qq_import_reader import stream_export
from qq_normalize import ExportFormatError, normalize_export, read_export
import test_qq_import as import_fixture
from test_qq_export import GOLDEN


class InputBoundaryTests(unittest.TestCase):
    def document(self):return copy.deepcopy(GOLDEN['export'])

    def test_explicit_iso_offsets_preserve_the_same_instant_across_calendar_days(self):
        expected=calendar.timegm((2026,7,9,16,0,0))*1000+123
        for stamp in ['2026-07-09T16:00:00.123Z','2026-07-09T16:00:00.123+00:00',
                      '2026-07-10T00:00:00.123+08:00','2026-07-09T12:00:00.123-04:00']:
            with self.subTest(stamp=stamp):
                document=self.document();document['messages'][0]['timestamp']=stamp
                result=normalize_export(document)
                self.assertEqual(result['status'],'complete')
                self.assertEqual(result['records'][0]['time_ms'],expected)

    def test_timezone_free_and_invalid_calendar_values_are_counted_without_guessing(self):
        for stamp in ['2026-07-10T12:00:00','2026-07-10 12:00:00','2026-02-30T00:00:00Z',
                      'not a timestamp','1969-12-31T23:59:59Z']:
            with self.subTest(stamp=stamp):
                document=self.document();document['messages'][0]['timestamp']=stamp
                result=normalize_export(document)
                self.assertEqual(result['status'],'partial')
                self.assertEqual(result['counts']['rowsRejected'],1)
                self.assertEqual(result['rejected'][0]['reason'],'invalid-time')
                self.assertEqual(len(result['records']),1)

    def test_single_json_bom_preserves_multiline_unicode_in_both_readers(self):
        document=self.document();body='第一行\n第二行\t🪴 e\u0301 引号"与反斜线\\'
        document['messages'][0]['content']['text']=body
        # This golden format carries ordinary authored text, not an elements preview.
        document['messages'][0]['content'].pop('elements',None)
        with tempfile.TemporaryDirectory(prefix='qq-input-boundary-') as directory:
            path=Path(directory)/'中文 BOM 聊天.json'
            path.write_text(json.dumps(document,ensure_ascii=False),encoding='utf-8-sig')
            expected=normalize_export(document);read=read_export(path)
            emitted=[]
            metadata,kind=stream_export(path,lambda row,reason:emitted.append((row,reason)),cancel=lambda:None)
            rebuilt={**metadata,'messages':[row for row,reason in emitted if reason is None]}
            self.assertEqual(kind,'qce-single-json')
            self.assertTrue(all(reason is None for row,reason in emitted))
            self.assertEqual(read['records'],expected['records'])
            self.assertEqual(normalize_export(rebuilt)['records'],expected['records'])
            self.assertEqual(read['records'][0]['text'],body)

    def test_manifest_and_each_jsonl_chunk_bom_preserve_all_native_ids(self):
        document=self.document();manifest=copy.deepcopy(document);rows=manifest.pop('messages')
        manifest['chunked']={'format':'jsonl','chunksDir':'chunks','chunks':[
            {'relativePath':'chunks/0001.jsonl'},{'relativePath':'chunks/0002.jsonl'}]}
        with tempfile.TemporaryDirectory(prefix='qq-chunk-bom-') as directory:
            root=Path(directory);(root/'chunks').mkdir();path=root/'manifest.json'
            path.write_text(json.dumps(manifest),encoding='utf-8-sig')
            for index,row in enumerate(rows,1):
                (root/'chunks'/f'{index:04d}.jsonl').write_text(json.dumps(row,ensure_ascii=False)+'\n',encoding='utf-8-sig')
            expected=normalize_export(document);emitted=[]
            read=read_export(path)
            metadata,kind=stream_export(path,lambda row,reason:emitted.append((row,reason)),cancel=lambda:None)
            self.assertEqual(kind,'qce-chunked-jsonl')
            self.assertTrue(all(reason is None for row,reason in emitted))
            self.assertEqual(read['records'],expected['records'])
            self.assertEqual([row['messageId'] for row,reason in emitted],[row['native_id'] for row in expected['records']])


@unittest.skipUnless(os.name=='nt','actual Windows junction required')
class ChunkJunctionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=import_fixture.ImportTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.fixture.root
        self.export=self.root/'selected-export';self.export.mkdir()
        self.outside=self.root/'outside-export-root';self.outside.mkdir()
        self.target=self.outside/'0001.jsonl'
        self.original=('\n'.join(json.dumps(row,ensure_ascii=False) for row in self.fixture.document['messages'])+'\n').encode('utf-8')
        self.target.write_bytes(self.original)
        self.link=self.export/'chunks'

    def prepare_junction(self):
        environment=os.environ.copy();environment['QQVIBE_TEST_LINK']=str(self.link);environment['QQVIBE_TEST_TARGET']=str(self.outside)
        result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',
            "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:QQVIBE_TEST_LINK -Target $env:QQVIBE_TEST_TARGET | Out-Null"],
            env=environment,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(self.link.is_junction(),'test must exercise a real junction, not a mocked attribute')
        self.addCleanup(self.remove_link)
        self.assertEqual(self.link.resolve(),self.outside.resolve())
        manifest=copy.deepcopy(self.fixture.document);manifest.pop('messages')
        manifest['chunked']={'format':'jsonl','chunksDir':'chunks','chunks':[{'relativePath':'chunks/0001.jsonl'}]}
        self.path=self.export/'manifest.json';self.path.write_text(json.dumps(manifest),encoding='utf-8')
        self.fixture.path=self.path

    def remove_link(self):
        if self.link.exists():
            self.assertTrue(self.link.is_junction())
            os.rmdir(self.link)  # Remove only the junction itself, never its target.
        self.assertEqual(self.target.read_bytes(),self.original)

    def test_both_readers_reject_outside_junction_before_opening_the_target(self):
        self.prepare_junction();opened=[];original_open=Path.open
        def observed_open(path,*args,**kwargs):
            opened.append(path.resolve());return original_open(path,*args,**kwargs)
        with patch.object(Path,'open',observed_open):
            with self.assertRaisesRegex(ExportFormatError,'chunk-outside-export-directory'):read_export(self.path)
            with self.assertRaisesRegex(ExportFormatError,'chunk-outside-export-directory'):
                stream_export(self.path,lambda row,reason:self.fail('outside row emitted'),cancel=lambda:None)
        self.assertNotIn(self.target.resolve(),opened)
        self.assertEqual(self.target.read_bytes(),self.original)

    def test_formal_preview_refuses_junction_without_creating_an_account_library(self):
        self.prepare_junction();preview=self.fixture.preview()
        self.assertEqual(preview['state'],'failed')
        self.assertEqual(preview['error'],'chunk-outside-export-directory')
        self.assertFalse(self.fixture.fixture.data_root.exists())
        self.assertEqual(self.target.read_bytes(),self.original)

    def test_failed_junction_preview_preserves_an_existing_library_and_binding(self):
        initial=self.fixture.commit(self.fixture.preview());self.fixture.api.activate(initial['result']['accountId'])
        source=self.fixture.fixture.source;library=source._store
        before=library.connection.execute('SELECT native_id,text,direction,status FROM messages ORDER BY native_id').fetchall()
        identity=source.identity()
        self.prepare_junction();preview=self.fixture.preview()
        self.assertEqual(preview['state'],'failed');self.assertEqual(preview['error'],'chunk-outside-export-directory')
        self.assertEqual(source.identity(),identity)
        self.assertEqual(library.connection.execute('SELECT native_id,text,direction,status FROM messages ORDER BY native_id').fetchall(),before)
        self.assertEqual(self.target.read_bytes(),self.original)


if __name__=='__main__':unittest.main(verbosity=2)
