"""Windows Docker ingress: callback-only port, never exposes management routes."""
from contextlib import nullcontext
import asyncio,socket,secrets
from fastapi import FastAPI,Request,WebSocket
from fastapi.responses import JSONResponse
import uvicorn

class OneBotIngress:
    def __init__(self):self.port=0;self.server=None;self.task=None;self.sock=None
    async def start(self,ws_handler,http_handler,load_config):
        if self.task and not self.task.done():return self.port
        app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
        def allowed(headers):
            cfg=load_config().adapter;token=cfg.onebot_token
            authorization=headers.get('authorization','')
            return bool(cfg.qq_enabled and token and any(secrets.compare_digest(authorization,allowed) for allowed in (token,'Bearer '+token)))
        @app.websocket('/ws/onebot')
        async def ws(websocket:WebSocket):
            if not allowed(websocket.headers):await websocket.close(code=1008);return
            await ws_handler(websocket)
        @app.post('/onebot/event')
        async def event(request:Request):
            if not allowed(request.headers):return JSONResponse({'status':'failed','retcode':403},status_code=403)
            return await http_handler(request)
        self.sock=socket.socket();self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        self.sock.bind(('0.0.0.0',0));self.sock.listen(128);self.sock.setblocking(False);self.port=self.sock.getsockname()[1]
        self.server=uvicorn.Server(uvicorn.Config(app,lifespan='off',log_level='warning',timeout_graceful_shutdown=2))
        if hasattr(self.server,'capture_signals'):self.server.capture_signals=lambda:nullcontext()
        self.server.install_signal_handlers=lambda:None
        self.task=asyncio.create_task(self.server.serve(sockets=[self.sock]))
        for _ in range(200):
            if self.server.started:return self.port
            if self.task.done():self.task.result();raise RuntimeError('OneBot入口未启动')
            await asyncio.sleep(.01)
        await self.close();raise TimeoutError('OneBot入口启动超时')
    async def close(self):
        if self.server:self.server.should_exit=True
        if self.task:
            try:await asyncio.wait_for(self.task,3)
            except TimeoutError:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        if self.sock:self.sock.close()
        self.port=0;self.task=None;self.server=None;self.sock=None
