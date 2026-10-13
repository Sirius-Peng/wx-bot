"""端到端冒烟测试 —— 微信官方 iLink 通道 + 人格 + 知识库。

为什么需要它：
  这台机器人最容易坏的地方都在"链路接线"上，而不是算法：
    · iLink 是**拉**模型，忘了跑长轮询 → 登录成功但永远收不到消息
    · 消息归一化字段与 WxMessage 不一致 → 每条消息都被丢掉
    · 回复时必须带回入站消息的 context_token → 否则发不出去
    · 用户配置的人设没拼进系统提示词 → "人设改了没反应"
  这些都不会报错，只会"静默不工作"。所以这里用一个假的 iLink + 假的
  OpenAI 服务端，把整条链路真跑一遍，逐项断言。

用法：
    .venv/bin/python tools/smoke_ilink.py        # 退出码 0 = 全部通过

它**不联网**、不碰真实 data/ 目录（用临时目录），可以随时跑。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# 本机地址不能走系统代理，否则假服务端会被代理劫持成 502
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn                                            # noqa: E402
from fastapi import FastAPI, Request                      # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"

PERSONA = "你是一只测试用的猫娘助手，每次回答都必须以「喵」结尾。"
INBOUND_TEXT = "你好呀"
EXPECTED_REPLY = "收到啦，我会把完整回复整理后一次发给你，不再把一句回答拆成多条消息。后面的内容会一起保留，也会合并普通聊天中多余的换行喵。"
STREAM_REPLY = EXPECTED_REPLY.replace("后面的内容", "\n\n后面的内容")
CTX_TOKEN = "ctx-token-abc123"
BOT_ID = "bot_abc@im.bot"                    # 机器人自己（@im.bot 命名空间）
OWNER_ID = "user_abc@im.wechat"              # 扫码绑定它的那个人（@im.wechat）

# 假服务端记录下来的请求
REC: dict = {"chat": [], "sent": [], "getupdates": 0, "notify": []}
_updates_served = False


def st2_bot_id(runtime) -> str:
    """取当前 iLink 适配器认定的"机器人自己的 id"。"""
    a = getattr(runtime, "wechat_adapter", None)
    return getattr(a, "bot_id", "") or ""


def build_fake_server() -> FastAPI:
    app = FastAPI()

    # ── 假 iLink ──────────────────────────────────────────────
    @app.post("/ilink/bot/msg/{which}")
    async def notify(which: str):
        REC["notify"].append(which)
        return {"ret": 0}

    @app.post("/ilink/bot/getconfig")
    async def getconfig():
        return {"ret": 0, "typing_ticket": "typing-ticket"}

    @app.post("/ilink/bot/sendtyping")
    async def sendtyping():
        return {"ret": 0}

    @app.post("/ilink/bot/getupdates")
    async def getupdates(request: Request):
        global _updates_served
        REC["getupdates"] += 1
        body = await request.json()
        REC.setdefault("cursors", []).append(body.get("get_updates_buf", ""))
        if _updates_served:
            await asyncio.sleep(0.3)          # 模拟长轮询 hold
            return {"ret": 0, "msgs": [], "get_updates_buf": "cur-2"}
        _updates_served = True
        return {
            "ret": 0,
            "get_updates_buf": "cur-1",
            "msgs": [{
                "message_id": "msg-1",
                "message_type": 1,             # 用户发来的
                "from_user_id": OWNER_ID,
                "create_time_ms": int(time.time() * 1000),
                "context_token": CTX_TOKEN,
                "item_list": [{"type": 1, "text_item": {"text": INBOUND_TEXT}}],
            }],
        }

    @app.post("/ilink/bot/sendmessage")
    async def sendmessage(request: Request):
        body = await request.json()
        REC["sent"].append(body)
        return {"ret": 0, "message_id": f"out-{len(REC['sent'])}"}

    # ── 假 OpenAI（主模型 / 判定模型都指到这里）───────────────
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        REC["chat"].append(body)
        reply = STREAM_REPLY

        if not body.get("stream"):
            return JSONResponse({
                "id": "cmpl-1", "object": "chat.completion", "created": 0,
                "model": body.get("model", "fake"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": reply if body.get("tools") else "[]"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

        async def sse():
            for ch in (reply[i:i + 12] for i in range(0, len(reply), 12)):
                chunk = {
                    "id": "cmpl-1", "object": "chat.completion.chunk", "created": 0,
                    "model": body.get("model", "fake"),
                    "choices": [{"index": 0, "delta": {"content": ch},
                                 "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            done = {
                "id": "cmpl-1", "object": "chat.completion.chunk", "created": 0,
                "model": body.get("model", "fake"),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    return app


def start_fake_server() -> uvicorn.Server:
    config = uvicorn.Config(build_fake_server(), host="127.0.0.1", port=PORT,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if getattr(server, "started", False):
            return server
        time.sleep(0.1)
    raise RuntimeError("假服务端启动失败")


# ═══════════════════════════════════════════════════════════════════
#  测试主体
# ═══════════════════════════════════════════════════════════════════

async def run_checks(data_dir: Path) -> list[tuple[str, bool, str]]:
    from src.config_store import ConfigStore
    from src.runtime import BotRuntime
    from src.core.sender import HumanLikeSender

    # 打字延迟是为了拟人，测试里没必要真等 1.5~4 秒
    HumanLikeSender.human_delay = staticmethod(lambda text: 0.01)

    cfg_file = data_dir / "config.json"
    cfg_file.write_text(json.dumps({
        "adapter": {
            "wechat_enabled": True, "qq_enabled": False,
            "ilink_state_dir": "./data/ilink",
            # 旧配置里的停服通道字段，必须被安全忽略而不是报错
            "wechat_type": "gewechat", "gewe_token": "stale",
            "hybrid_enabled": True, "wpp_admin_key": "stale",
        },
        "llm": {
            "primary_base": f"{BASE}/v1", "primary_key": "test-key",
            "primary_model": "fake-model",
            "search_enabled": os.environ.get("WXBOT_SMOKE_SEARCH") == "1",
            "judge_base": f"{BASE}/v1", "judge_key": "test-key",
            "judge_model": "fake-model",
        },
        "bot": {
            "name": "测试助手", "persona": PERSONA,
            "memory_enabled": True, "memory_inject": True,
            "memory_extract_interval": 3,
        },
    }, ensure_ascii=False), encoding="utf-8")

    state = data_dir / "ilink"
    state.mkdir(parents=True, exist_ok=True)
    (state / "account.json").write_text(json.dumps({
        # token 前缀就是机器人的 id（@im.bot 命名空间）
        "bot_token": f"{BOT_ID}:fake-secret",
        "base_url": BASE,               # 指向假服务端
        "bot_id": BOT_ID,
        "owner_user_id": OWNER_ID,
        # 旧字段：早期版本把**扫码人**的 id 存在这里，回读时必须能识别出来
        "bot_user_id": OWNER_ID,
    }), encoding="utf-8")

    store = ConfigStore(cfg_file)
    runtime = BotRuntime(store, data_dir)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    # 旧配置迁移
    cfg = store.load()
    check("旧配置里的停服通道被迁移为官方 iLink",
          cfg.adapter.wechat_type == "ilink", f"wechat_type={cfg.adapter.wechat_type}")

    # 手动加一条"所有人通用"的记忆（界面上留空对象就是这个效果）
    runtime.memory_add(scope="user", owner_id="global", category="fact",
                       content="用户的名字叫小明，是一名后端工程师")

    res = await runtime.start()
    check("微信通道启动成功", res.get("ok") and "wechat" in str(res.get("details")),
          str(res.get("message")))

    deadline = time.time() + 20
    while time.time() < deadline and not REC["sent"]:
        await asyncio.sleep(0.2)

    st = runtime.status()
    check("iLink 长轮询在运行", st.get("ilink_polling") is True,
          f"ilink_polling={st.get('ilink_polling')}")

    # ⚠️ 真实故障回归：扫码人的 id（@im.wechat）曾被当成机器人自己的 id，
    #    导致他自己发的每条消息都被判成"机器人自身消息"直接丢弃。
    check("从 token 推出了机器人自己的 id",
          st.get("bot") is not None and st2_bot_id(runtime) == BOT_ID,
          f"bot_id={st2_bot_id(runtime)}")

    chat_requests = [body for body in REC["chat"] if body.get("stream") or body.get("tools")]
    check("收到并处理了入站消息", bool(chat_requests),
          f"getupdates 调用 {REC['getupdates']} 次，模型调用 {len(REC['chat'])} 次")

    # 系统提示词里必须有人设 —— 这是"人格无法加载"的核心断言
    if chat_requests:
        # Background memory extraction may reach the model before the actual reply.
        # Identify the conversation request (stream or search-tool route), not arrival order.
        msgs = chat_requests[0].get("messages") or []
        sys_text = "\n".join(m.get("content", "") for m in msgs
                             if m.get("role") == "system")
        check("人设写进了系统提示词", PERSONA in sys_text,
              f"系统提示词长度 {len(sys_text)}")
        check("机器人名称写进了系统提示词", "测试助手" in sys_text,
              sys_text.splitlines()[0] if sys_text else "")
        check("通用记忆被注入到上下文", "小明" in sys_text,
              "已注入" if "小明" in sys_text else "system 消息里没有记忆内容")
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        check("用户消息进入上下文",
              bool(user_msgs) and INBOUND_TEXT in user_msgs[-1].get("content", ""),
              str(user_msgs[-1:])[:120])

    # 回复必须带回 context_token，否则微信里根本发不出去
    if REC["sent"]:
        sent = REC["sent"][0].get("msg") or {}
        check("回复带上了入站消息的 context_token",
              sent.get("context_token") == CTX_TOKEN,
              f"context_token={sent.get('context_token')!r}")
        check("回复发给了正确的会话",
              sent.get("to_user_id") == OWNER_ID,
              f"to_user_id={sent.get('to_user_id')!r}")
        check("回复内容非空",
              bool((sent.get("item_list") or [{}])[0].get("text_item", {}).get("text")),
              json.dumps(sent, ensure_ascii=False)[:160])
        text = (sent.get("item_list") or [{}])[0].get("text_item", {}).get("text", "")
        check("工具模式回复只发一条完整消息" if os.environ.get("WXBOT_SMOKE_SEARCH")=="1" else "多块流式回复只发送一条完整消息",
              len(REC["sent"]) == 1 and text == EXPECTED_REPLY,
              f"发送 {len(REC['sent'])} 条，完整正文一致={text == EXPECTED_REPLY}")
        check("普通聊天多余换行已合并", "\n" not in text,
              f"换行数={text.count(chr(10))}")
    else:
        check("回复已发出", False, "没有收到 sendmessage 调用")

    # ⚠️ 真实故障回归：扫码绑定那个人的 id（@im.wechat）曾被当成机器人自己的 id，
    #    于是他自己发的消息全被判成"机器人自身消息"、直接丢弃（界面上就是这么显示的）。
    #    这里直接查消息日志里的判定结果 —— 与用户在界面上看到的是同一份数据。
    inbound = [e for e in runtime.log.query(limit=20)
               if e.get("direction") == "in"]
    check("扫码人的消息按私聊正常放行（非「机器人自身消息」）",
          bool(inbound) and inbound[0].get("trigger_layer") == "private"
          and "自身消息" not in (inbound[0].get("trigger_reason") or ""),
          (f"判定={inbound[0].get('trigger_layer')}/"
           f"{inbound[0].get('trigger_reason')}") if inbound else "没有入站记录")

    stats = runtime.log.stats()
    check("消息日志记录了收发双方",
          stats.get("total_in", 0) >= 1 and stats.get("total_out", 0) >= 1,
          f"stats={stats}")

    await runtime.stop()
    check("停止后长轮询已退出", runtime.status().get("ilink_polling") is False,
          f"ilink_polling={runtime.status().get('ilink_polling')}")
    return results


def main() -> int:
    server = start_fake_server()
    try:
        with tempfile.TemporaryDirectory(prefix="wxbot-smoke-") as tmp:
            results = asyncio.run(run_checks(Path(tmp)))
    finally:
        server.should_exit = True

    width = max(len(n) for n, _, _ in results)
    failed = 0
    print("\n" + "═" * 68)
    print("  iLink 端到端冒烟测试")
    print("═" * 68)
    for name, ok, detail in results:
        if not ok:
            failed += 1
        print(f"  {'✓' if ok else '✗'} {name.ljust(width)}  {detail}")
    print("═" * 68)
    print(f"  {len(results) - failed}/{len(results)} 项通过")
    print("═" * 68 + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
