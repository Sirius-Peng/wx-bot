"""端到端测试：模拟一个 OneBot 11 网关，验证 QQ 链路是否真的通。

运行：
    python -m src.verify_qq

做的事：
  1. 起一个假的 OneBot 网关（HTTP API + 反向 WebSocket）
  2. 起本程序，配置成 QQ 通道
  3. 假网关连上本程序的 /ws/onebot，推送一条群消息（@机器人）
  4. 检查本程序是否通过 HTTP API 把回复发回假网关
  5. 检查知识库是否从对话中积累了记忆
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import uvicorn

# 假网关收到的消息（供断言）
RECEIVED: list[dict] = []
LOGIN_INFO = {"user_id": 10001, "nickname": "测试机器人"}


class MockOneBotHandler(BaseHTTPRequestHandler):
    """假的 OneBot HTTP API。"""

    def log_message(self, *a):        # 静音
        pass

    def do_POST(self):
        action = self.path.lstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")

        # 校验 Token（与真实网关行为一致）
        if TOKEN:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {TOKEN}":
                self._json({"status": "failed", "retcode": 403,
                            "message": "token 错误"})
                return

        if action == "get_login_info":
            self._json({"status": "ok", "retcode": 0, "data": LOGIN_INFO})
        elif action in ("send_group_msg", "send_private_msg"):
            RECEIVED.append({"action": action, **body})
            self._json({"status": "ok", "retcode": 0,
                        "data": {"message_id": 9999}})
        elif action == "get_group_member_info":
            self._json({"status": "ok", "retcode": 0, "data": {
                "user_id": body.get("user_id"), "nickname": "测试用户",
                "card": "测试-群名片", "role": "member"}})
        else:
            self._json({"status": "ok", "retcode": 0, "data": {}})

    def _json(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


TOKEN = "test-token-abc"


def start_mock_gateway(port: int) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), MockOneBotHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def push_event_via_ws(ws_url: str, event: dict) -> bool:
    """假网关通过反向 WebSocket 推送一条事件。"""
    try:
        import websockets
    except ImportError:
        print("  ⚠ 未安装 websockets，跳过 WS 测试")
        return False

    try:
        async with websockets.connect(ws_url,
                                      additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            await ws.send(json.dumps(event))
            await asyncio.sleep(3)          # 给程序处理时间
            return True
    except Exception as e:
        print(f"  ✗ WebSocket 连接失败：{e}")
        return False


async def main() -> None:
    print("=" * 70)
    print("QQ（OneBot 11）链路端到端验证")
    print("=" * 70)

    data_dir = Path(tempfile.mkdtemp())
    gw_port = 3010
    app_port = 8941

    # ── 1. 起假网关 ──
    srv = start_mock_gateway(gw_port)
    print(f"\n[1] 假 OneBot 网关已启动 :{gw_port}")

    # ── 2. 起本程序 ──
    import os
    os.environ.update({
        "WXBOT_DATA_DIR": str(data_dir),
        "WXBOT_NO_BROWSER": "1",
        "WXBOT_PORT": str(app_port),
    })
    from src.server import app
    cfg = uvicorn.Config(app, host="127.0.0.1", port=app_port, log_level="warning")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    for _ in range(40):
        await asyncio.sleep(0.25)
        try:
            async with httpx.AsyncClient(timeout=2) as c:
                if (await c.get(f"http://127.0.0.1:{app_port}/healthz")).status_code == 200:
                    break
        except Exception:
            pass
    print(f"[2] 本程序已启动 :{app_port}")

    base = f"http://127.0.0.1:{app_port}"
    async with httpx.AsyncClient(timeout=10) as c:
        # ── 3. 配置成 QQ 通道 ──
        await c.put(f"{base}/api/config", json={
            "adapter": {
                "type": "onebot",
                "onebot_http_url": f"http://127.0.0.1:{gw_port}",
                "onebot_token": TOKEN,
            },
            "llm": {"primary_key": "sk-fake"},
            "bot": {"name": "小助手", "memory_enabled": True,
                    "memory_extract_interval": 1},
            "groups": {},
        })
        print("[3] 已配置为 QQ / OneBot 通道")

        # ── 4. 测试连接 ──
        r = (await c.post(f"{base}/api/qq/test", json={
            "http_url": f"http://127.0.0.1:{gw_port}", "token": TOKEN,
        })).json()
        mark = "✓" if r.get("ok") else "✗"
        print(f"[4] {mark} 连接测试：{r.get('message')}")

        # Token 错误时应正确识别
        r2 = (await c.post(f"{base}/api/qq/test", json={
            "http_url": f"http://127.0.0.1:{gw_port}", "token": "wrong",
        })).json()
        mark2 = "✓" if not r2.get("ok") and r2.get("stage") == "auth" else "✗"
        print(f"[5] {mark2} 错误 Token 被识别：{r2.get('message')}")

    # ── 6. 启动机器人（适配器才会真正接管事件）──
    async with httpx.AsyncClient(timeout=30) as c:
        r = (await c.post(f"{base}/api/bot/start")).json()
        mark = "✓" if r.get("ok") else "✗"
        print(f"[6] {mark} 启动机器人：{r.get('message')}")

    # ── 7. 通过反向 WS 推送群消息 ──
    print("[7] 通过反向 WebSocket 推送群消息…")
    event = {
        "time": int(time.time()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 12345,
        "group_id": 67890,
        "user_id": 12345,
        "raw_message": "[CQ:at,qq=10001] 你好",
        "message": [
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "text", "data": {"text": "你好，我叫老王，在字节做后端"}},
        ],
        "sender": {"user_id": 12345, "nickname": "老王", "card": "后端-老王",
                   "role": "member"},
    }
    ws_ok = await push_event_via_ws(
        f"ws://127.0.0.1:{app_port}/ws/onebot", event)

    if ws_ok:
        # primary_key 是假的 → LLM 调用会失败 → 走兜底文案；
        # 关键看程序有没有真的回调 send_group_msg 把消息发出去
        await asyncio.sleep(5)
        if RECEIVED:
            msg = RECEIVED[-1]
            print(f"  ✓ 程序已回调网关发送消息")
            print(f"     action={msg.get('action')} group_id={msg.get('group_id')}")
            content = msg.get("message") or []
            text = "".join(s.get("data", {}).get("text", "") for s in content
                           if isinstance(s, dict) and s.get("type") == "text")
            ats = [s.get("data", {}).get("qq") for s in content
                   if isinstance(s, dict) and s.get("type") == "at"]
            print(f"     内容={text[:60]!r}")
            print(f"     @对象={ats}（群聊回复会 @ 提问者）")
        else:
            print("  ✗ 未观察到发送 —— 链路可能有问题")

    # ── 8. 检查知识库 ──
    async with httpx.AsyncClient(timeout=10) as c:
        st = (await c.get(f"{base}/api/memory/stats")).json()
        print(f"[8] 知识库：启用={st.get('enabled')} 记忆数={st.get('total')}"
              f"（抽取需真实 LLM，此处为 0 属预期）")

    print("\n" + "=" * 70)
    print("验证完成")
    print("=" * 70)

    server.should_exit = True
    await asyncio.sleep(0.5)
    task.cancel()
    srv.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
