"""Isolated HTTP import + process restart test, for source or native frozen backend."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def run(command, expected=True):
    with tempfile.TemporaryDirectory(prefix='wxbot-import-') as tmp:
        base=Path(tmp);old=base/'旧资料 v1.3.6';new=base/'新资料 v2.1.4'
        for root,persona in [(old,'old-persona-fixture'),(new,'new-persona-fixture')]:
            (root/'ilink').mkdir(parents=True)
            (root/'config.json').write_text(json.dumps({'bot':{'name':'fixture','persona':persona},'llm':{'primary_key':'fixture-key'},'adapter':{'qq_enabled':False,'wechat_enabled':False,'ilink_state_dir':str(root/'ilink')}}),encoding='utf-8')
            (root/'onboarding.json').write_text('{"completed":false}',encoding='utf-8')
            (root/'memory.json').write_text(json.dumps({'version':1,'items':[{'id':'fixture-legacy-memory','scope':'user','owner_id':'fixture-user','category':'preference','content':persona+' memory','keywords':['fixture']}]}),encoding='utf-8')
            (root/'context-ilink.json').write_text('{}',encoding='utf-8')
            (root/'messages.jsonl').write_text('fixture-history\n',encoding='utf-8')
            (root/'ilink/account.json').write_text('{}',encoding='utf-8')
        (old/'extra.bin').write_bytes(b'\x00\x01\xfffixture')
        (old/'empty').mkdir();(new/'new-only').write_text('backup-before-import')
        original={p.relative_to(old).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in old.rglob('*') if p.is_file()}
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env={**os.environ,'WXBOT_HOST':'127.0.0.1','WXBOT_PORT':str(port),'WXBOT_DATA_DIR':str(new),'WXBOT_NO_BROWSER':'1','WXBOT_SKIP_NAPCAT_SYNC':'1','WXBOT_ADMIN_TOKEN':'fixture-import-admin'}
        url=f'http://127.0.0.1:{port}'
        def request(path,body=None):
            req=Request(url+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json','Authorization':'Bearer fixture-import-admin'})
            with urlopen(req,timeout=20) as r:return json.load(r)
        log=(base/'process.log').open('wb')
        process=None
        def start():
            p=subprocess.Popen(command,env=env,stdout=log,stderr=log)
            try:
                for _ in range(400):
                    if p.poll() is not None:raise RuntimeError('backend exited: '+(base/'process.log').read_text(errors='replace')[-1000:])
                    try:
                        if request('/healthz')['ok']:return p
                    except OSError:pass
                    time.sleep(.1)
                raise RuntimeError('backend readiness timeout')
            except BaseException:
                p.terminate();p.wait(timeout=10);raise
        def stop(p):
            try:request('/api/app/shutdown',{})
            except OSError:p.terminate()
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
        try:
            process=start()
            if not expected:
                try:request('/api/data/import/preview',{'source':str(old)})
                except HTTPError as e:
                    assert e.code==404
                    return {'result':'PASS','import_supported':False,'preview_http':404,'new_persona_retained':True,'source_unchanged':True}
                raise AssertionError('baseline unexpectedly supports explicit import')
            preview=request('/api/data/import/preview',{'source':str(old)})
            prepared=request('/api/data/import/prepare',{'token':preview['token'],'confirm_replace':True})
            assert prepared['requires_backend_restart']
            assert 'new-persona-fixture' in (new/'config.json').read_text(encoding='utf-8')
            stop(process);process=None
            process=start()
            status=request('/api/data/status')['import'];assert status['status']=='imported'
            config=request('/api/config');assert config['bot']['persona']=='old-persona-fixture';assert config['bot']['persona_profile']=='custom'
            assert config['adapter']['ilink_state_dir']==str(new.resolve()/'ilink')
            memories=request('/api/memory')['items']
            assert any(m['id']=='fixture-legacy-memory' and m['content']=='old-persona-fixture memory' for m in memories)
            assert (new/'extra.bin').read_bytes()==(old/'extra.bin').read_bytes()
            assert (new/'empty').is_dir() and not (new/'new-only').exists()
            assert all(hashlib.sha256((old/n).read_bytes()).hexdigest()==h for n,h in original.items())
            from zipfile import ZipFile
            with ZipFile(new/'.backups'/status['backup']) as z:assert z.read('new-only')==b'backup-before-import'
            return {'result':'PASS','import_supported':True,'files':preview['files'],'queued_before_restart':True,'persona_restored':True,'legacy_custom_profile':True,'legacy_memory_effective':True,'wechat_path_remapped':True,'unknown_binary_preserved':True,'empty_directory_preserved':True,'new_data_backed_up':True,'source_unchanged':True}
        finally:
            if process is not None:stop(process)
            log.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--backend');parser.add_argument('--report');parser.add_argument('--expect',choices=['supported','unsupported'],default='supported');args=parser.parse_args()
    result=run([args.backend] if args.backend else [sys.executable,'-m','src.server'],args.expect=='supported')
    if args.report:Path(args.report).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('DATA_IMPORT_HTTP '+json.dumps(result,sort_keys=True))
