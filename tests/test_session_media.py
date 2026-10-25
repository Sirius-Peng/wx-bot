"""Exact opaque image persistence, immutable writes and local path boundaries."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from src.core.session_media import INLINE_THRESHOLD, PREFIX, SessionMedia


def data_url(character='A', length=6000):
    return 'data:image/png;base64,'+character*length


def session(*urls):
    return {'version':1,'unknown':{'retained':[1,2]},'messages':[
        {'role':'system','content':'人格原文，不应修改'},
        {'role':'user','content':[{'type':'text','text':'请按顺序看这几张图'}]+[
            {'type':'image_url','image_url':{'url':url,'detail':'high'},'extra':'retained'} for url in urls]},
        {'role':'assistant','content':'原答复'}]}


class SessionMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.context=self.root/'context-qq.json'
        self.media=SessionMedia(self.context)

    def blobs(self):
        return sorted((self.root/'context-qq-media').glob('*.blob'))

    def symlink(self, path, target, *, directory=False):
        try:path.symlink_to(target,target_is_directory=directory)
        except (OSError,NotImplementedError) as error:
            self.skipTest('Native symlink creation not available: '+type(error).__name__)

    def test_exact_roundtrip_preserves_structure_quality_order_and_unknown_fields(self):
        original=session(data_url(),data_url('B'))
        untouched=deepcopy(original)
        frozen=self.media.freeze(original)
        self.assertEqual(original,untouched)
        self.assertIsNot(frozen,original)
        self.assertNotEqual(frozen,original)
        self.assertEqual(self.media.thaw(frozen),original)
        self.assertEqual(frozen['unknown'],original['unknown'])
        self.assertEqual(frozen['messages'][1]['content'][1]['image_url']['detail'],'high')
        self.assertEqual(len(self.blobs()),2)

    def test_payload_is_original_utf8_bytes_named_by_hash(self):
        url='data:image/png;自定义参数;base64,'+'图'*5000
        frozen=self.media.freeze(session(url))
        digest=hashlib.sha256(url.encode('utf-8')).hexdigest()
        blob=self.root/'context-qq-media'/(digest+'.blob')
        self.assertEqual(blob.read_bytes(),url.encode('utf-8'))
        self.assertEqual(frozen['messages'][1]['content'][1]['image_url']['url'],PREFIX+digest)
        if os.name!='nt':self.assertEqual(stat.S_IMODE(blob.stat().st_mode),0o600)

    def test_duplicate_urls_and_repeated_freeze_do_not_rewrite_or_fsync(self):
        original=session(data_url(),data_url())
        with patch('src.core.session_media.os.fsync',wraps=os.fsync) as fsync:
            first=self.media.freeze(original);blob=self.blobs()[0];before=blob.stat()
            second=self.media.freeze(original);after=blob.stat()
            self.assertEqual(fsync.call_count,1)
        self.assertEqual(first,second)
        self.assertEqual((before.st_ino,before.st_mtime_ns),(after.st_ino,after.st_mtime_ns))
        self.assertEqual(len(self.blobs()),1)

    def test_existing_frozen_reference_is_idempotent_and_validated(self):
        frozen=self.media.freeze(session(data_url()))
        with patch('src.core.session_media.os.fsync') as fsync:
            self.assertEqual(self.media.freeze(frozen),frozen)
            fsync.assert_not_called()

    def test_repeated_reference_reads_are_deduplicated_within_one_operation(self):
        original=session(data_url(),data_url())
        frozen=self.media.freeze(original)
        with patch.object(self.media,'_read',wraps=self.media._read) as read:
            self.assertEqual(self.media.thaw(frozen),original)
            self.assertEqual(read.call_count,1)
        with patch.object(self.media,'_read',wraps=self.media._read) as read:
            self.assertEqual(self.media.freeze(original),frozen)
            self.assertEqual(read.call_count,1)

    def test_restart_recovers_exact_session_without_inline_base64_in_json(self):
        original=session(data_url(),data_url('B'))
        frozen=self.media.freeze(original)
        encoded=json.dumps(frozen,ensure_ascii=False)
        self.assertNotIn(data_url(),encoded);self.assertNotIn(data_url('B'),encoded)
        self.assertLess(len(encoded),1000)
        self.context.write_text(encoded,encoding='utf-8')
        reloaded=json.loads(self.context.read_text(encoding='utf-8'))
        self.assertEqual(SessionMedia(self.context).thaw(reloaded),original)
        self.assertEqual(json.loads(self.context.read_text(encoding='utf-8')),frozen)

    def test_same_directory_and_context_stem_do_not_move_original_json(self):
        self.context.write_text('{"unmodified":true}',encoding='utf-8')
        before=self.context.read_bytes()
        self.media.freeze(session(data_url()))
        self.assertEqual(self.context.read_bytes(),before)
        self.assertEqual(self.blobs()[0].parent,self.context.parent/'context-qq-media')

    def test_threshold_is_strictly_more_than_4096_characters(self):
        prefix='data:image/png;base64,'
        exact=prefix+'A'*(INLINE_THRESHOLD-len(prefix))
        original=session(exact,exact+'A')
        frozen=self.media.freeze(original)
        images=frozen['messages'][1]['content']
        self.assertEqual(images[1]['image_url']['url'],exact)
        self.assertTrue(images[2]['image_url']['url'].startswith(PREFIX))
        self.assertEqual(self.media.thaw(frozen),original)

    def test_regular_text_https_and_short_data_urls_are_never_processed(self):
        original=session('https://example.com/'+'A'*7000,'data:image/png;base64,abc')
        original['messages'][0]['content']=data_url()
        original['messages'][1]['content'][0]['text']=data_url()
        original['unknown']['url']=data_url()
        self.assertEqual(self.media.freeze(original),original)
        self.assertEqual(self.media.thaw(original),original)
        self.assertFalse((self.root/'context-qq-media').exists())

    def test_internal_uri_in_ordinary_text_is_not_a_storage_reference(self):
        original={'messages':[{'role':'user','content':PREFIX+'../private'}],
                  'unknown':PREFIX+'not-a-hash'}
        self.assertEqual(self.media.freeze(original),original)
        self.assertEqual(self.media.thaw(original),original)

    def test_none_path_is_deepcopy_only_without_filesystem_access(self):
        original=session(data_url(),PREFIX+'invalid-but-opaque-for-memory-only')
        media=SessionMedia(None)
        with patch('src.core.session_media.os.open',side_effect=AssertionError('no filesystem')):
            for operation in (media.freeze,media.thaw):
                result=operation(original)
                self.assertEqual(result,original)
                result['unknown']['retained'].append(3)
                self.assertEqual(original['unknown']['retained'],[1,2])
        self.assertEqual(list(self.root.iterdir()),[])

    def test_missing_blob_rejects_prefix_without_mutating_input(self):
        frozen=self.media.freeze(session(data_url()));before=deepcopy(frozen)
        self.blobs()[0].unlink()
        with self.assertRaises((ValueError,OSError)):self.media.thaw(frozen)
        self.assertEqual(frozen,before)

    def test_missing_directory_rejects_prefix_without_creating_it(self):
        original=session(PREFIX+'a'*64)
        with self.assertRaises((ValueError,OSError)):self.media.thaw(original)
        self.assertFalse((self.root/'context-qq-media').exists())

    def test_corrupt_blob_rejects_thaw_and_does_not_overwrite_immutable_content(self):
        original=session(data_url());frozen=self.media.freeze(original)
        blob=self.blobs()[0];blob.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):self.media.thaw(frozen)
        with self.assertRaises(ValueError):self.media.freeze(original)
        self.assertEqual(blob.read_bytes(),b'corrupt')

    def test_valid_hash_with_invalid_utf8_or_non_data_payload_is_rejected(self):
        self.media.directory.mkdir()
        for payload in (b'\xff'*5000,b'https://example.com/'+b'A'*5000,b'data:short'):
            digest=hashlib.sha256(payload).hexdigest()
            (self.media.directory/(digest+'.blob')).write_bytes(payload)
            with self.assertRaises((ValueError,OSError)):
                self.media.thaw(session(PREFIX+digest))

    def test_noncanonical_or_traversing_internal_references_are_rejected(self):
        invalid=['../outside','a'*63,'a'*65,'g'*64,'A'*64,'a'*64+'/../outside',
                 'a'*64+'?file=outside','a'*64+'#outside','a'*32+'\\'+'a'*32]
        urls=[PREFIX+value for value in invalid]+['wxbot-session-media:'+'a'*64,'WXBOT-SESSION-MEDIA://'+'a'*64]
        for url in urls:
            for operation in (self.media.freeze,self.media.thaw):
                with self.subTest(url=url,operation=operation.__name__):
                    with self.assertRaises(ValueError):operation(session(url))
        self.assertEqual(list(self.root.iterdir()),[])

    def test_sidecar_directory_symlink_is_rejected_without_external_write(self):
        target=self.root/'unrelated';target.mkdir()
        self.symlink(self.media.directory,target,directory=True)
        with self.assertRaises(ValueError):self.media.freeze(session(data_url()))
        with self.assertRaises(ValueError):self.media.thaw(session(PREFIX+'a'*64))
        self.assertEqual(list(target.iterdir()),[])

    def test_blob_symlink_is_rejected_even_when_target_has_correct_payload(self):
        url=data_url();digest=hashlib.sha256(url.encode()).hexdigest()
        self.media.directory.mkdir();external=self.root/'outside.blob';external.write_text(url)
        self.symlink(self.media.directory/(digest+'.blob'),external)
        with self.assertRaises(ValueError):self.media.freeze(session(url))
        with self.assertRaises(ValueError):self.media.thaw(session(PREFIX+digest))
        self.assertEqual(external.read_text(),url)

    def test_dangling_blob_symlink_is_not_replaced(self):
        url=data_url();digest=hashlib.sha256(url.encode()).hexdigest()
        self.media.directory.mkdir();target=self.root/'does-not-exist'
        link=self.media.directory/(digest+'.blob');self.symlink(link,target)
        with self.assertRaises(ValueError):self.media.freeze(session(url))
        self.assertTrue(link.is_symlink());self.assertFalse(target.exists())

    def test_explicit_user_parent_alias_is_resolved_as_a_fixed_trust_root(self):
        real=self.root/'chosen-real';real.mkdir();alias=self.root/'chosen-alias'
        self.symlink(alias,real,directory=True)
        media=SessionMedia(alias/'context-wechat.json');original=session(data_url())
        frozen=media.freeze(original)
        self.assertEqual(media.thaw(frozen),original)
        self.assertTrue((real/'context-wechat-media').is_dir())
        self.assertFalse((real/'context-wechat-media').is_symlink())

    def test_later_root_redirect_is_rejected(self):
        chosen=self.root/'chosen';chosen.mkdir();media=SessionMedia(chosen/'context.json')
        chosen.rmdir();target=self.root/'elsewhere';target.mkdir();self.symlink(chosen,target,directory=True)
        with self.assertRaises(ValueError):media.freeze(session(data_url()))
        self.assertEqual(list(target.iterdir()),[])

    def test_nonregular_blob_or_sidecar_is_rejected(self):
        self.media.directory.write_bytes(b'not a directory')
        with self.assertRaises((ValueError,OSError)):self.media.freeze(session(data_url()))
        self.media.directory.unlink();self.media.directory.mkdir()
        digest=hashlib.sha256(data_url().encode()).hexdigest()
        (self.media.directory/(digest+'.blob')).mkdir()
        with self.assertRaises(ValueError):self.media.freeze(session(data_url()))

    def test_failed_atomic_write_does_not_publish_blob_or_leave_temporary_files(self):
        with patch('src.core.session_media.os.fsync',side_effect=OSError('fixture disk error')):
            with self.assertRaises(OSError):self.media.freeze(session(data_url()))
        self.assertEqual(list(self.media.directory.iterdir()),[])

    def test_clearing_references_does_not_collect_blobs_or_unrelated_files(self):
        self.media.freeze(session(data_url()));blob=self.blobs()[0];before=blob.read_bytes()
        unrelated=self.media.directory/'user-notes.txt';unrelated.write_text('保留原文件',encoding='utf-8')
        empty={'messages':[],'unknown':{'retained':True}}
        self.assertEqual(self.media.freeze(empty),empty)
        self.assertEqual(self.media.thaw(empty),empty)
        self.assertEqual(blob.read_bytes(),before)
        self.assertEqual(unrelated.read_text(encoding='utf-8'),'保留原文件')

    def test_malformed_non_image_structures_and_unknown_fields_are_copied(self):
        for original in ({'messages':'opaque'},{'messages':[None,{'content':'text'},{'content':[None,{},
                {'type':'image_url','image_url':'not-an-object'},
                {'type':'image_url','image_url':{'url':123}},
                {'type':'other','image_url':{'url':data_url()}}]}],'other':[1,{'future':True}]}):
            self.assertEqual(self.media.freeze(original),original)
            self.assertEqual(self.media.thaw(original),original)


if __name__=='__main__':unittest.main()
