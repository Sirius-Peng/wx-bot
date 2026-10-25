"""Representative version-shaped fixtures, never installed-user directories."""
import hashlib,json,tempfile,time,unittest
from pathlib import Path
from src.config_store import ConfigStore
from src.core.memory import PersonaMemory
from src.core.storage import initialize_storage,verify_backup,_snapshot

class CoverUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_overwrite_keeps_data_and_evidence_clock_for_supported_versions(self):
        for version,memory_version in [('1.3.6',1),('2.1.4',2),('2.1.7',3)]:
            with self.subTest(version=version),tempfile.TemporaryDirectory() as folder:
                base=Path(folder);root=base/'用户 自定义'/'data';root.mkdir(parents=True)
                custom=base/'外部 登录';custom.mkdir()
                now=time.time();anchor=now-8*86400
                cfg={'bot':{'persona':'旧版调好的完整自定义人格：保持标点、换行\n第二段','persona_profile':'custom','name':'fixture'},
                     'adapter':{'ilink_state_dir':str(custom)},'llm':{'primary_key':'fixture-only-key','primary_model':'fixture-original-model'},
                     'unknown_plugin':{'path':str(base/'user path')}}
                (root/'config.json').write_text(json.dumps(cfg,ensure_ascii=False))
                auto=dict(id='auto',scope='user',owner_id='fixture',platform='ilink',category='preference',content='用户喜欢绿茶',
                    source_chat='fixture',evidence='我喜欢绿茶',created_at=anchor,updated_at=anchor,valid_from=anchor,hits=999)
                manual=dict(auto,id='manual',source_chat='manual',content='固定的重要称呼')
                if memory_version==3:
                    auto.update(phase='stable',last_user_evidence_at=anchor);manual.update(phase='pinned',pinned=True,last_user_evidence_at=anchor)
                (root/'memory.json').write_text(json.dumps({'version':memory_version,'items':[auto,manual]}))
                retained={'context-ilink.json':b'{"version":2,"contexts":{},"summaries":{}}',
                          'ilink/outbox.json':b'{"replies":{"fixture":{"text":"original body","client_id":"fixed-id"}}}',
                          'ilink/account.json':b'{"bot_token":"fixture-only-token"}',
                          'qq-bursts.json':b'{"version":2,"pending":{}}',
                          'messages.jsonl':b'fixture-only raw log\n','custom/unknown.bin':b'\x00user-owned\xff'}
                for name,data in retained.items():
                    path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
                archive=_snapshot(root,base/'backups','v3-upgrade')
                self.assertTrue(verify_backup(archive)['files'])
                initialize_storage(root,migrate=False)
                store=ConfigStore(root/'config.json');loaded=store.load();store.save(loaded)
                self.assertEqual(loaded.bot.persona,cfg['bot']['persona']);self.assertEqual(loaded.bot.persona_profile,'custom')
                self.assertEqual(loaded.adapter.ilink_state_dir,str(custom));self.assertEqual(loaded.llm.primary_key,'fixture-only-key')
                self.assertEqual(loaded.llm.primary_model,'fixture-original-model')
                self.assertEqual(json.loads((root/'config.json').read_text())['unknown_plugin'],cfg['unknown_plugin'])
                memory=PersonaMemory(root/'memory.json')
                try:
                    self.assertEqual(memory._items['auto'].last_user_evidence_at,anchor)
                    self.assertEqual(memory._items['auto'].lifecycle(now)[0],'dormant')
                    self.assertEqual(memory._items['manual'].lifecycle(now)[0],'pinned')
                finally:await memory.learner.close();await memory.close_usage()
                for name,data in retained.items():self.assertEqual((root/name).read_bytes(),data,name)

if __name__=='__main__':unittest.main()
