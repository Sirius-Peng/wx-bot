"""Isolated real HTTP/WS integration with a deterministic local LLM stub."""
from __future__ import annotations
import asyncio,json,os,socket,subprocess,sys,tempfile,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx,websockets
ROOT=Path(__file__).resolve().parents[1]

class Model(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if body.get('stream'):
            chunks=[{'choices':[{'delta':{'content':s}}]} for s in ['听见了。','慢慢说，','我在听。']]
            data=''.join('data: '+json.dumps(c,ensure_ascii=False)+'\n\n' for c in chunks)+'data: [DONE]\n\n'
            content=data.encode();self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
        else:
            data={'choices':[{'message':{'content':'[]'}}],'usage':{'prompt_tokens':10,'completion_tokens':1}}
            content=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)

def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

async def run():
    from src.config_store import ConfigStore,AppConfig
    persistent=os.environ.get('WXBOT_SMOKE_DATA');tmp=None
    if persistent:root=Path(persistent);root.mkdir(parents=True,exist_ok=True)
    else:tmp=tempfile.TemporaryDirectory();root=Path(tmp.name)
    http_port=int(os.environ.get('WXBOT_SMOKE_PORT') or port());model_port=port()
    server=ThreadingHTTPServer(('127.0.0.1',model_port),Model);threading.Thread(target=server.serve_forever,daemon=True).start()
    cfg=AppConfig();cfg.adapter.wechat_enabled=False;cfg.adapter.qq_enabled=False
    cfg.llm.primary_base=f'http://127.0.0.1:{model_port}/v1';cfg.llm.primary_key='local-test-placeholder';cfg.llm.search_enabled=False
    cfg.bot.reply_mode='always';cfg.bot.memory_enabled=False;cfg.bot.humanized_wait=False;cfg.bot.message_merge_window=.03;cfg.companion.enabled=True
    ConfigStore(root/'config.json').save(cfg)
    env={**os.environ,'WXBOT_DATA_DIR':str(root),'WXBOT_PORT':str(http_port),'WXBOT_NO_BROWSER':'1','WXBOT_HOST':'127.0.0.1'}
    log=(root/'smoke-backend.log').open('w');process=subprocess.Popen([sys.executable,'-m','src.server'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
    base=f'http://127.0.0.1:{http_port}'
    try:
        async with httpx.AsyncClient(base_url=base,trust_env=False) as client:
            for _ in range(100):
                try:
                    if (await client.get('/healthz')).status_code==200:break
                except httpx.HTTPError:pass
                await asyncio.sleep(.1)
            await client.get('/')
            cookie='; '.join(f'{k}={v}' for k,v in client.cookies.items())
            manual=await client.post('/api/memory',json={'scope':'user','owner_id':'local','platform':'desktop','category':'fact','content':'只属于初始人格空间的测试记忆'})
            assert manual.status_code==200,manual.text
            initial=(await client.get('/api/memory')).json()['space']
            events=[]
            async with websockets.connect(base.replace('http','ws')+'/ws/companion',proxy=None,origin=base,additional_headers={'Cookie':cookie}) as ws:
                await ws.send(json.dumps({'type':'message','client_id':'smoke-v3','text':'你好，帮我检查桌面对话'}))
                while True:
                    event=json.loads(await asyncio.wait_for(ws.recv(),10));events.append(event)
                    if event['type']=='completed':break
                assert ''.join(e.get('text','') for e in events if e['type']=='delta')=='听见了。慢慢说，我在听。',events
                assert sum(e['type']=='completed' for e in events)==1
                await ws.send(json.dumps({'type':'message','client_id':'smoke-v3','text':'你好，帮我检查桌面对话'}))
                while True:
                    event=json.loads(await asyncio.wait_for(ws.recv(),2))
                    assert event['type']!='completed'
                    if event['type']=='ack':assert event['duplicate'];break
            async with websockets.connect(base.replace('http','ws')+'/ws/companion',proxy=None,origin=base,additional_headers={'Cookie':cookie}) as ws:
                await ws.send(json.dumps({'type':'sync','after':0}))
                replay=[]
                while not any(e['type']=='completed' for e in replay):replay.append(json.loads(await asyncio.wait_for(ws.recv(),2)))
                assert not any(e['type']=='delta' for e in replay)
                changed=await client.patch('/api/config/section/persona',json={'persona_relationship':'trusted'})
                assert changed.status_code==200,changed.text
                assert changed.json()['requires_restart'] is False,changed.text
                newer=(await client.get('/api/memory')).json()
                assert newer['space']['id']!=initial['id'] and not newer['items'],newer
                await ws.send(json.dumps({'type':'message','client_id':'smoke-new-space','text':'你好，新人格空间的输入','persona_space':newer['space']['id']}))
                current=[]
                while not any(e['type']=='completed' and e.get('persona_space')==newer['space']['id'] for e in current):
                    current.append(json.loads(await asyncio.wait_for(ws.recv(),10)))
                assert sum(e['type']=='completed' for e in current)==1,current
                back=await client.patch('/api/config/section/persona',json={'persona_relationship':'neutral'})
                assert back.status_code==200,back.text
                restored=(await client.get('/api/memory')).json()
                assert restored['space']['id']==initial['id'] and len(restored['items'])==1,restored
            # A browser-origin spoof cannot access local conversations.
            try:
                async with websockets.connect(base.replace('http','ws')+'/ws/companion',proxy=None,origin='https://evil.invalid',additional_headers={'Cookie':cookie}):raise AssertionError('bad origin admitted')
            except websockets.exceptions.InvalidStatus:pass
            sdk=Path.home()/'.cache/wxbot-live2d-v3/sdk/CubismSdkForWeb-5-r.5/Samples/Resources/Haru'
            if sdk.is_dir():
                result=await client.post('/api/companion/model/import',json={'path':str(sdk)})
                assert result.status_code==200,result.text
            print(json.dumps({'result':'COMPANION_SMOKE PASS','stream_one_bubble':True,'duplicate_guard':True,'reconnect_final_only':True,'origin_guard':True,'persona_space_switch_and_restore':True,'url':base,'data':str(root)},ensure_ascii=False),flush=True)
            if os.environ.get('WXBOT_SMOKE_HOLD'):
                print('BROWSER_QA_READY',base,flush=True)
                while True:await asyncio.sleep(10)
    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        server.shutdown();log.close()
        if tmp:tmp.cleanup()

if __name__=='__main__':asyncio.run(run())
