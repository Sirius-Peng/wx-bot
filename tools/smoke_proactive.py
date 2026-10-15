"""Exercise packaged proactive APIs with isolated contacts and no model/account network."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def check(binary: Path, supported=True):
    with tempfile.TemporaryDirectory(prefix='wxbot-proactive-') as tmp:
        data = Path(tmp)
        (data/'config.json').write_text(json.dumps({'adapter': {'wechat_enabled':False,'qq_enabled':False},
            'llm':{'primary_key':''}, 'bot':{'persona':'frozen-fixture'}}),encoding='utf-8')
        # Prepare valid account state without touching an installed user's directory.
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0,str(root))
        from src.core.proactive import ProactivePlanner
        from src.core.types import WxMessage
        from src.config_store import AppConfig
        async def no_work(*args):raise AssertionError('no model or outbound call expected')
        planner = ProactivePlanner(data/'proactive.json',lambda p,u:p+':'+u,AppConfig,
                                  lambda *a:None,no_work,no_work)
        for platform in ('ilink','onebot'):
            planner.note_in(WxMessage('fixture-'+platform,platform,'private',
                'wx-fixture' if platform=='ilink' else 'p_123','wx-fixture' if platform=='ilink' else '123',
                'fixture-user','fixture-bot','音乐话题',context_token='synthetic-context'))
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        env={**os.environ,'WXBOT_HOST':'127.0.0.1','WXBOT_PORT':str(port),'WXBOT_DATA_DIR':str(data),
             'WXBOT_ADMIN_TOKEN':'proactive-fixture-admin','WXBOT_NO_BROWSER':'1','WXBOT_SKIP_NAPCAT_SYNC':'1',
             'WXBOT_MIGRATE_LEGACY':'0','PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
        opener=build_opener(ProxyHandler({}))
        def request(path,method='GET',body=None):
            req=Request(f'http://127.0.0.1:{port}'+path,
                data=json.dumps(body).encode() if body is not None else None,method=method,
                headers={'Authorization':'Bearer proactive-fixture-admin','Content-Type':'application/json'})
            with opener.open(req,timeout=10) as response:return json.load(response)
        with (data/'backend.log').open('wb') as log:
            process=subprocess.Popen([str(binary)],env=env,cwd=binary.parent,stdout=log,stderr=log)
            try:
                end=time.monotonic()+60
                while time.monotonic()<end:
                    if process.poll() is not None:raise AssertionError('backend exited: '+(data/'backend.log').read_text(errors='replace')[-1500:])
                    try:
                        if request('/healthz')['ok']:break
                    except (URLError,TimeoutError):pass
                    time.sleep(.1)
                else:raise AssertionError('backend startup timed out')
                if not supported:
                    try:request('/api/proactive')
                    except HTTPError as e:
                        assert e.code==404
                        return {'summary':'FROZEN_PROACTIVE BASELINE PASS api=404 feature=ABSENT','exit_status':0}
                    raise AssertionError('baseline unexpectedly exposes proactive API')
                status=request('/api/proactive');assert not status['enabled'] and not status['running']
                assert len(status['contacts'])==2 and all(len(c['accounts'])==1 for c in status['contacts'])
                assert 'synthetic-context' not in json.dumps(status)
                selected=request('/api/proactive/contact','PATCH',{'id':'ilink:wx-fixture','enabled':True})
                assert sum(c['enabled'] for c in selected['contacts'])==1
                config=request('/api/config/section/proactive','PATCH',{'proactive_enabled':True,'proactive_min_hours':2.5,'proactive_max_hours':6})
                assert config['ok'] and not config['requires_restart'] and config['config']['bot']['proactive_min_hours']==2.5
                linked=request('/api/memory/identities','POST',{'wechat_id':'wx-fixture','qq_id':'123'})
                assert linked['ok']
                merged=request('/api/proactive')['contacts'];assert len(merged)==1 and len(merged[0]['accounts'])==2
                request('/api/proactive/contact','PATCH',{'id':merged[0]['id'],'enabled':False})
                assert not request('/api/proactive')['contacts'][0]['enabled']
                assert not request('/healthz')['running']
                return {'summary':'FROZEN_PROACTIVE MODIFIED PASS single_contacts=2 identity_optional=1 linked_plan=1 scoped_settings=1 no_token_leak=1 bot=STOPPED model_calls=0','exit_status':0}
            finally:
                if process.poll() is None:
                    try:request('/api/app/shutdown','POST')
                    except (URLError,TimeoutError):process.terminate()
                    try:process.wait(timeout=15)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--backend',required=True);p.add_argument('--expect',choices=['supported','unsupported'],default='supported');p.add_argument('--report');args=p.parse_args()
    result=check(Path(args.backend).resolve(),args.expect=='supported')
    if args.report:Path(args.report).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(result['summary'])
