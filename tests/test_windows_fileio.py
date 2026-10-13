import os,tempfile,unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from src.core.fileio import open_shared_read,read_snapshot
from src.core.storage import _lock
class WindowsFileIOTests(unittest.TestCase):
 def test_open_snapshot_allows_delete(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'日志 中文.jsonl';p.write_bytes(b'original')
   with open_shared_read(p) as f:p.unlink();self.assertEqual(f.read(),b'original')
   self.assertFalse(p.exists())
 def test_open_snapshot_allows_atomic_replace(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'log';new=Path(d)/'new';p.write_bytes(b'old');new.write_bytes(b'new')
   snapshot,offset=read_snapshot(p,1024)
   os.replace(new,p);self.assertEqual(snapshot,b'old');self.assertEqual(offset,0)
   self.assertEqual(p.read_bytes(),b'new')
 def test_read_handle_does_not_allow_write(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'log';p.write_bytes(b'value')
   with open_shared_read(p) as f:
    with self.assertRaises(OSError):f.write(b'x')
 def test_missing_raises(self):
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(OSError):open_shared_read(Path(d)/'missing')
 def test_lock_initialization_concurrent_no_prelock_write(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);counter=[]
   def run(_):
    with _lock(root):counter.append(1)
   with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(run,range(16)))
   self.assertEqual(len(counter),16);self.assertEqual((root/'.storage.lock').read_bytes(),b'0')

 def test_snapshot_is_bounded(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'log';p.write_bytes(b'123456789')
   data,offset=read_snapshot(p,4);self.assertEqual(data,b'6789');self.assertEqual(offset,5)
