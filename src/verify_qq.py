"""Offline QQ end-to-end test: fake OneBot + fake LLM, real HTTP and WS routes.

Run: python -m src.verify_qq. Failed assertions produce a nonzero exit status.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import httpx
import uvicorn
import websockets

TOKEN = "test-onebot-token"
ADMIN_TOKEN = "test-admin-token"
LOGIN_INFO = {"user_id": 10001, "nickname": "测试机器人"}
RECEIVED: list[dict] = []


class MockOneBotHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_POST(self):
        action = self.path.lstrip("/")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._json({"status": "failed", "retcode": 403, "message": "token 错误"})
        elif action == "get_login_info":
            self._json({"status": "ok", "retcode": 0, "data": LOGIN_INFO})
        elif action in ("send_group_msg", "send_private_msg"):
            RECEIVED.append({"action": action, **body})
            self._json({"status": "ok", "retcode": 0, "data": {"message_id":len(RECEIVED)}})
        else:
            self._json({"status": "ok", "retcode": 0, "data": {}})

    def _json(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_until(predicate, timeout=5):
    deadline = asyncio.get_running_loop().time()+timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate(): return
        await asyncio.sleep(0.02)
    raise AssertionError("expected QQ event/reply was not observed")


async def fake_stream(self, messages, **kwargs):
    yield "离线模型回复第一句，内容超过首段发送阈值，现在仍需等待生成结束。\n\n"
    yield "第二句会合并到同一条回复里。"


async def fake_complete(self, prompt, **kwargs):
    return '[{"category":"fact","content":"用户是后端工程师","keywords":["后端"]}]'


async def run(data_dir):
    from .core.llm import LLMGateway
    from .core.sender import HumanLikeSender
    RECEIVED.clear()
    gateway = HTTPServer(("127.0.0.1", 0), MockOneBotHandler)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    app_port = free_port()
    env = {"WXBOT_DATA_DIR":str(data_dir),"WXBOT_PORT":str(app_port),
           "WXBOT_HOST":"127.0.0.1","WXBOT_NO_BROWSER":"1",
           "WXBOT_ADMIN_TOKEN":ADMIN_TOKEN,"WXBOT_SKIP_NAPCAT_SYNC":"1"}
    with patch.dict(os.environ,env), patch.object(LLMGateway,"stream",fake_stream), \
            patch.object(LLMGateway,"complete",fake_complete), \
            patch.object(HumanLikeSender,"human_delay",staticmethod(lambda text:0)):
        from .server import app, runtime
        runtime.set_http_port(app_port)
        from .core.docker import DockerState
        runtime.docker.detect = lambda: DockerState()
        runtime.docker.napcat_status = lambda: {"running": False}
        server = uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=app_port,
                                               log_level="warning",access_log=False))
        task = asyncio.create_task(server.serve())
        try:
            await wait_until(lambda:server.started)
            base = f"http://127.0.0.1:{app_port}"
            async with httpx.AsyncClient(base_url=base,trust_env=False,timeout=10,
                    headers={"Authorization":f"Bearer {ADMIN_TOKEN}"}) as c:
                assert (await c.get('/api/config',headers={'Authorization':'Bearer wrong'})).status_code==401
                r=await c.put('/api/config',json={
                    'adapter':{'wechat_enabled':False,'qq_enabled':True,
                               'onebot_http_url':f'http://127.0.0.1:{gateway.server_port}',
                               'onebot_token':TOKEN},
                    'llm':{'primary_key':'offline-key','judge_key':'offline-key'},
                    'bot':{'name':'测试助手','memory_enabled':True,'memory_extract_interval':1}})
                assert r.status_code==200
                good=(await c.post('/api/qq/test',json={})).json()
                assert good['ok']
                bad=(await c.post('/api/qq/test',json={'token':'wrong'})).json()
                assert not bad['ok'] and bad['stage']=='auth'
                assert (await c.post('/api/bot/start')).json()['ok']
                assert (await c.post('/api/qq/sync-ws')).json()['skipped']
                async with websockets.connect(f'ws://127.0.0.1:{app_port}/ws/onebot',
                        additional_headers={'Authorization':f'Bearer {TOKEN}'},proxy=None) as ws:
                    await wait_until(lambda:runtime.qq_adapter.reverse_ws_connected)
                    status=(await c.get('/api/qq/status')).json()
                    assert status['reverseWsConnected'] and str(app_port) in status['wsUrl']
                    event={'time':1,'self_id':10001,'post_type':'message','message_type':'group',
                           'sub_type':'normal','message_id':1,'group_id':67890,'user_id':12345,
                           'message':[{'type':'at','data':{'qq':'10001'}},
                                      {'type':'text','data':{'text':'我是后端工程师'}}],
                           'sender':{'nickname':'测试用户','card':'后端用户'}}
                    await ws.send(json.dumps(event))
                    await wait_until(lambda:len(RECEIVED)==1)
                    reply=RECEIVED[0]
                    assert reply['action']=='send_group_msg' and reply['group_id']==67890
                    assert reply['message'][0]=={'type':'at','data':{'qq':'12345'}}
                    assert '离线模型回复' in reply['message'][1]['data']['text']
                    assert '第二句' in reply['message'][1]['data']['text']
                    assert '\n' not in reply['message'][1]['data']['text']
                    quiet={**event,'message_id':2,'message':[{'type':'text','data':{'text':'闲聊'}}]}
                    await ws.send(json.dumps(quiet))
                    await wait_until(lambda:runtime.qq_adapter.event_count>=2)
                    await asyncio.sleep(0.05)
                    assert len(RECEIVED)==1
                    private={**event,'message_id':3,'message_type':'private','user_id':55555,
                             'message':[{'type':'text','data':{'text':'你好'}}]}
                    await ws.send(json.dumps(private))
                    await wait_until(lambda:len(RECEIVED)==2)
                    assert RECEIVED[-1]['action']=='send_private_msg'
                    before=runtime.qq_adapter.event_count
                    await ws.send(json.dumps(private))
                    await wait_until(lambda:runtime.qq_adapter.event_count>before)
                    assert len(RECEIVED)==2
                    # 抽取是同步链路末尾，等待归档完成再核对知识库。
                    await wait_until(lambda:runtime.memory.stats()['total']>=2)
                    assert (await c.get('/api/memory/stats')).json()['total']>=2
                assert (await c.post('/api/bot/stop')).json()['ok']
                assert not (await c.get('/api/status')).json()['running']
            print('PASS QQ: auth, gateway token, WS status, group mention, silence, private reply, dedup, memory, stop (offline)')
        finally:
            server.should_exit=True
            try:await asyncio.wait_for(task,5)
            except asyncio.TimeoutError:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
            gateway.shutdown();gateway.server_close();thread.join(timeout=2)


def main():
    with tempfile.TemporaryDirectory(prefix='wxbot-qq-test-') as d:
        asyncio.run(run(Path(d)))


if __name__=='__main__':main()
