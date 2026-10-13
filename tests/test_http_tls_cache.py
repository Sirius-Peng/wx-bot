import asyncio,os,ssl,tempfile,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import certifi
import src.core.http as module
class TLSCacheTests(unittest.TestCase):
 def setUp(self):
  self.env=patch.dict(os.environ,{},clear=False);self.env.start()
  for k in ['SSL_CERT_FILE','SSL_CERT_DIR','SSLKEYLOGFILE']:os.environ.pop(k,None)
  with module._TLS_LOCK:module._TLS_CONTEXTS.clear()
 def tearDown(self):
  with module._TLS_LOCK:module._TLS_CONTEXTS.clear()
  self.env.stop()
 def test_default_context_reused_without_disabling_validation(self):
  a=module.verified_context();b=module.verified_context();self.assertIs(a,b);self.assertTrue(a.check_hostname);self.assertEqual(a.verify_mode,ssl.CERT_REQUIRED)
 def test_concurrent_initialization_once(self):
  with ThreadPoolExecutor(max_workers=8) as pool:contexts=list(pool.map(lambda _:module.verified_context(),range(16)))
  self.assertTrue(all(c is contexts[0] for c in contexts))
 def test_changed_ca_file_invalidates(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'ca.pem';p.write_bytes(Path(certifi.where()).read_bytes());os.environ['SSL_CERT_FILE']=str(p);a=module.verified_context();p.write_bytes(p.read_bytes()+b'\n');self.assertIsNot(a,module.verified_context())
 def test_env_ca_respected_but_local_proxy_bypass_keeps_previous_contract(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'ca.pem';p.write_bytes(Path(certifi.where()).read_bytes());os.environ['SSL_CERT_FILE']=str(p);a=module.verified_context();b=module.verified_context(trust_env=False);self.assertIsNot(a,b)
 def test_invalid_custom_ca_does_not_silently_fallback(self):
  os.environ['SSL_CERT_FILE']='/missing/ca.pem'
  with self.assertRaises(OSError):module.verified_context()
 def test_changed_cert_directory_invalidates(self):
  with patch.object(module.ssl,'create_default_context',side_effect=lambda **kw:object()) as create:
   with tempfile.TemporaryDirectory() as d:
    os.environ['SSL_CERT_DIR']=d
    with patch.object(module,'_ca_revision',side_effect=[(0,1),(0,2)]):self.assertIsNot(module.verified_context(),module.verified_context())
   self.assertEqual(create.call_args.kwargs['capath'],d)
 def test_bounded_configuration_cache(self):
  with patch.object(module.ssl,'create_default_context',side_effect=lambda **kw:object()):
   for i in range(20):
    with patch.dict(os.environ,{'SSL_CERT_FILE':f'/synthetic/ca{i}.pem'}):module.verified_context()
  self.assertLessEqual(len(module._TLS_CONTEXTS),8)
 def test_factory_passes_strict_context_and_preserves_timeout_proxy(self):
  with patch.object(module.httpx,'AsyncClient') as client:
   module.make_client('http://127.0.0.1:3000',timeout=7)
   kwargs=client.call_args.kwargs;self.assertFalse(kwargs['trust_env']);self.assertTrue(kwargs['verify'].check_hostname);self.assertEqual(kwargs['timeout'].connect,7)
   module.make_client('https://api.example.com',timeout=20);self.assertTrue(client.call_args.kwargs['trust_env'])
