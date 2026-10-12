"""机器人运行时 —— 管理生命周期、状态、消息日志与扫码登录。

这一层把「引擎」和「界面」解耦：
  · Web UI 只调用这里暴露的方法，不直接碰适配器/编排层
  · 未配置或启动失败时不会拖垮 Web 服务，界面始终可访问
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .adapters.base import BaseAdapter
from .adapters.gewechat import GewechatAdapter
from .adapters.hybrid import HybridAdapterRouter
from .adapters.ilink import ILinkAdapter
from .config_store import AppConfig, ConfigStore, GroupConfigItem
from .core.context import ContextManager
from .core.llm import JudgeLLM, LLMGateway, ProviderCfg
from .core.sender import HumanLikeSender
from .core.trigger import GroupConfig, TriggerRouter, default_group_config
from .core.types import WxMessage
from .orchestrator import Orchestrator
from .store_memory import MemoryStore

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  消息日志（供界面展示）
# ═══════════════════════════════════════════════════════════════════

@dataclass
class LogEntry:
    ts: int
    direction: str          # in | out
    chat_type: str
    chat_id: str
    chat_name: str
    sender: str
    content: str
    trigger_layer: str = ""
    trigger_reason: str = ""
    replied: bool = False   # 这条入站消息最终是否被回复

    def to_dict(self) -> dict:
        return {
            "ts": self.ts, "direction": self.direction,
            "chat_type": self.chat_type, "chat_id": self.chat_id,
            "chat_name": self.chat_name, "sender": self.sender,
            "content": self.content,
            "trigger_layer": self.trigger_layer,
            "trigger_reason": self.trigger_reason,
            "replied": self.replied,
            "time": time.strftime("%H:%M:%S", time.localtime(self.ts / 1000)),
        }


class MessageLog:
    """内存环形缓冲 + JSONL 落盘。界面读内存，重启后从文件恢复。"""

    def __init__(self, path: Path, maxlen: int = 2000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._buf: deque[LogEntry] = deque(maxlen=maxlen)
        self._stats = {"in": 0, "out": 0, "replied": 0, "silent": 0,
                       "groups": set(), "privates": set()}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines()[-2000:]:
                if not line.strip():
                    continue
                d = json.loads(line)
                e = LogEntry(**d)
                self._buf.append(e)
                self._recount(e)      # 重启后统计也要能对上历史数据
        except Exception:
            log.warning("消息日志加载失败，忽略", exc_info=True)

    def _recount(self, e: LogEntry) -> None:
        """重启后从历史记录重建统计。"""
        if e.direction == "out":
            self._stats["out"] += 1
            return
        self._stats["in"] += 1
        if e.replied:
            self._stats["replied"] += 1
        else:
            self._stats["silent"] += 1
        (self._stats["groups"] if e.chat_type == "group"
         else self._stats["privates"]).add(e.chat_id)

    def _append_file(self, entry: LogEntry) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": entry.ts, "direction": entry.direction,
                    "chat_type": entry.chat_type, "chat_id": entry.chat_id,
                    "chat_name": entry.chat_name, "sender": entry.sender,
                    "content": entry.content,
                    "trigger_layer": entry.trigger_layer,
                    "trigger_reason": entry.trigger_reason,
                    "replied": entry.replied,
                }, ensure_ascii=False) + "\n")
        except Exception:
            log.debug("消息日志写盘失败", exc_info=True)

    def add_in(self, msg: WxMessage, layer: str = "", reason: str = "",
               replied: bool = False) -> None:
        e = LogEntry(
            ts=msg.timestamp or int(time.time() * 1000), direction="in",
            chat_type=msg.chat_type, chat_id=msg.chat_id,
            chat_name=msg.sender_name if not msg.is_group else "",
            sender=msg.sender_name or msg.sender_id, content=msg.content,
            trigger_layer=layer, trigger_reason=reason, replied=replied,
        )
        self._buf.append(e)
        self._append_file(e)
        self._recount(e)

    def add_out(self, msg: WxMessage, text: str) -> None:
        e = LogEntry(
            ts=int(time.time() * 1000), direction="out",
            chat_type=msg.chat_type, chat_id=msg.chat_id,
            chat_name="", sender="机器人", content=text,
        )
        self._buf.append(e)
        self._append_file(e)
        self._recount(e)

    def query(self, limit: int = 100, chat_id: str = "",
              chat_type: str = "", direction: str = "") -> list[dict]:
        items = list(self._buf)[::-1]
        if chat_id:
            items = [e for e in items if e.chat_id == chat_id]
        if chat_type:
            items = [e for e in items if e.chat_type == chat_type]
        if direction:
            items = [e for e in items if e.direction == direction]
        return [e.to_dict() for e in items[:limit]]

    def stats(self) -> dict:
        return {
            "total_in": self._stats["in"],
            "total_out": self._stats["out"],
            "replied": self._stats["replied"],
            "silent": self._stats["silent"],
            "group_count": len(self._stats["groups"]),
            "private_count": len(self._stats["privates"]),
            "reply_rate": round(
                self._stats["replied"] / max(1, self._stats["in"]) * 100, 1
            ),
        }

    def clear(self) -> None:
        self._buf.clear()
        self._stats = {"in": 0, "out": 0, "replied": 0, "silent": 0,
                       "groups": set(), "privates": set()}
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
#  运行时
# ═══════════════════════════════════════════════════════════════════

class BotRuntime:
    """机器人运行时。所有对外的启停/状态/登录都从这里走。"""

    def __init__(self, store: ConfigStore, data_dir: Path):
        self.config_store = store
        self.data_dir = Path(data_dir)
        self.log = MessageLog(self.data_dir / "messages.jsonl")

        self.cfg: AppConfig = store.load()
        self.adapter: Optional[BaseAdapter] = None
        self.orchestrator: Optional[Orchestrator] = None
        self.running = False
        self.last_error = ""
        self.started_at = 0.0

        self._login: dict = {"status": "idle", "qrcode": "", "image": "",
                             "uuid": "", "app_id": "", "message": ""}
        self._poll_task: Optional[asyncio.Task] = None

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self) -> dict:
        if self.running:
            return {"ok": True, "message": "机器人已在运行"}
        try:
            self.cfg = self.config_store.load()
            self._validate()
            self.adapter = self._build_adapter()
            self.orchestrator = self._build_orchestrator(self.adapter)
            await self.adapter.start()
            self.running = True
            self.last_error = ""
            self.started_at = time.time()
            log.info("机器人已启动 platform=%s", self.adapter.platform)
            return {"ok": True, "message": f"机器人已启动（{self.adapter.platform}）"}
        except Exception as e:
            self.last_error = str(e)
            self.running = False
            log.exception("启动失败")
            return {"ok": False, "message": f"启动失败：{e}"}

    async def stop(self) -> dict:
        if not self.running or not self.adapter:
            self.running = False
            return {"ok": True, "message": "机器人未在运行"}
        try:
            await self.adapter.stop()
        except Exception:
            log.warning("关闭适配器异常", exc_info=True)
        self.running = False
        self.orchestrator = None
        self.adapter = None
        return {"ok": True, "message": "机器人已停止"}

    async def restart(self) -> dict:
        await self.stop()
        await asyncio.sleep(0.3)
        return await self.start()

    def status(self) -> dict:
        return {
            "running": self.running,
            "platform": self.adapter.platform if self.adapter else "",
            "supports_group": self.adapter.supports_group if self.adapter else False,
            "uptime": int(time.time() - self.started_at) if self.running else 0,
            "last_error": self.last_error,
            "login": self._login,
            "configured": bool(self.cfg.llm.primary_key),
            "stats": self.log.stats(),
        }

    # ── 构建 ──────────────────────────────────────────────────

    def _validate(self) -> None:
        a, l = self.cfg.adapter, self.cfg.llm
        if a.type == "gewechat" and not a.gewe_token:
            raise ValueError("未填写 GeWe Token，请到「接入设置」完善")
        if not l.primary_key:
            raise ValueError("未填写大模型 API Key，请到「模型设置」完善")

    def _build_adapter(self) -> BaseAdapter:
        a = self.cfg.adapter
        gewe = GewechatAdapter(
            base_url=a.gewe_base_url, token=a.gewe_token, app_id=a.gewe_app_id,
            bot_wxid=a.bot_wxid, bot_nickname=a.bot_nickname or self.cfg.bot.name,
        )
        if a.type == "ilink" or a.hybrid_enabled:
            official = ILinkAdapter(state_dir=a.ilink_state_dir, bot_agent="WxBot/1.0")
            if a.type == "ilink":
                return official
            return HybridAdapterRouter(private_adapter=official, group_adapter=gewe)
        return gewe

    def _build_orchestrator(self, adapter: BaseAdapter) -> Orchestrator:
        cfg = self.cfg
        judge_gw = LLMGateway(ProviderCfg(
            name="judge", base_url=cfg.llm.judge_base, api_key=cfg.llm.judge_key,
            model=cfg.llm.judge_model, timeout=3.0, max_tokens=8,
        ))
        chat_gw = LLMGateway(
            primary=ProviderCfg(
                name="primary", base_url=cfg.llm.primary_base,
                api_key=cfg.llm.primary_key, model=cfg.llm.primary_model, timeout=60.0,
            ),
            fallback=ProviderCfg(
                name="fallback", base_url=cfg.llm.fallback_base,
                api_key=cfg.llm.fallback_key, model=cfg.llm.fallback_model,
            ) if cfg.llm.fallback_key else None,
        )
        judge = JudgeLLM(judge_gw, timeout=3.0)
        store = self._build_store()
        router = TriggerRouter(
            store, judge, persona=cfg.bot.persona,
            default_mode=cfg.bot.group_reply_mode or "mention_only",
        )

        orch = Orchestrator(
            adapter=adapter, store=store, llm=chat_gw, judge=judge,
            ctx=ContextManager(store), router=router,
            sender=HumanLikeSender(adapter),
        )
        self._wire_logging(orch, adapter)
        return orch

    def _build_store(self) -> MemoryStore:
        """构建会话存储，并把配置里的群策略注入进去。"""
        store = MemoryStore()
        global_mode = self.cfg.bot.group_reply_mode or "mention_only"
        store.set_default_mode(global_mode)

        for cid, g in self.cfg.groups.items():
            base = default_group_config(cid, g.mode or global_mode)
            store.set_group_config(GroupConfig(
                chat_id=cid,
                enabled=g.enabled,
                mode=g.mode or global_mode,
                wake_words=list(g.wake_words) or base.wake_words,
                cooldown_sec=g.cooldown_sec,
                hourly_limit=g.hourly_limit,
                reply_prob=g.reply_prob,
                quiet_hours=list(g.quiet_hours) if g.quiet_hours is not None else base.quiet_hours,
            ))
        return store

    def _wire_logging(self, orch: Orchestrator, adapter: BaseAdapter) -> None:
        """挂日志钩子，让界面能看到每条消息的触发判定结果。

        做法：包一层 TriggerRouter.decide 记录决策，再包一层 _process
        把「入站消息 + 决策 + 是否真的回复了」写进日志。
        """
        last: dict = {"layer": "", "reason": ""}

        orig_decide = orch.router.decide

        async def decide_wrapped(msg: WxMessage, recent, cfg=None):
            d = await orig_decide(msg, recent, cfg)
            last["layer"], last["reason"] = d.layer, d.reason
            return d

        orch.router.decide = decide_wrapped        # type: ignore[method-assign]

        orig_process = orch._process

        async def process_wrapped(msg: WxMessage) -> None:
            out_before = self.log._stats["out"]
            try:
                await orig_process(msg)
            finally:
                replied = self.log._stats["out"] > out_before
                self.log.add_in(msg, last.get("layer", ""),
                                last.get("reason", ""), replied)

        orch._process = process_wrapped             # type: ignore[method-assign]

        # 出站消息也记一笔
        orig_send = adapter.send_text

        async def send_wrapped(chat_id: str, text: str, at=None) -> str:
            mid = await orig_send(chat_id, text, at=at)
            self.log.add_out(WxMessage(
                msg_id="", platform=adapter.platform,
                chat_type="group" if "@chatroom" in chat_id else "private",
                chat_id=chat_id, sender_id="", sender_name="", bot_id="",
                content=text,
            ), text)
            return mid

        adapter.send_text = send_wrapped            # type: ignore[method-assign]

    # ── 扫码登录 ──────────────────────────────────────────────

    async def login_qrcode(self) -> dict:
        """获取登录二维码。

        GeWe 自建网关：POST {base}/v2/api/login/getLoginQrCode
        云端 SaaS：    POST https://api.geweapi.com/gewe/v2/api/login/getLoginQrCode
        （用户只需把「网关地址」填对即可，两种部署方式都兼容）

        接口命名在不同版本间略有差异，这里按候选列表依次尝试。
        """
        a = self.cfg.adapter
        if a.type == "ilink":
            return {"ok": False, "message": "官方通道请用命令行扫码：openclaw channels login"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=25.0) as c:
                data = None
                last_err = ""
                for ep in ("login/getLoginQrCode", "login/getQrCode", "login/getQr"):
                    try:
                        r = await c.post(
                            f"{a.gewe_base_url.rstrip('/')}/v2/api/{ep}",
                            json={"appId": a.gewe_app_id, "proxyIp": ""},
                            headers={"X-GEWE-TOKEN": a.gewe_token},
                        )
                        if r.status_code != 200:
                            last_err = f"HTTP {r.status_code}"
                            continue
                        j = r.json()
                        if j.get("ret") == 200:
                            data = j.get("data") or {}
                            break
                        last_err = f"ret={j.get('ret')} {j.get('msg')}"
                    except Exception as e:
                        last_err = str(e)
                if data is None:
                    return {"ok": False, "message": f"获取二维码失败：{last_err}"}

            self._login = {
                "status": "waiting",
                "qrcode": data.get("qrData", ""),
                "image": data.get("qrImgBase64") or data.get("qrData", ""),
                "uuid": data.get("uuid", ""),
                "app_id": data.get("appId") or a.gewe_app_id,
                "message": "请用微信扫码（二维码 150 秒内有效）",
            }
            return {"ok": True, **self._login}
        except Exception as e:
            return {"ok": False, "message": f"获取二维码失败：{e}"}

    async def login_check(self, captcha: str = "") -> dict:
        """轮询登录状态。

        真实参数（已核对官方文档）：{appId, proxyIp, uuid, autoSliding}
        返回 data.status：1 = 已扫码待确认；成功时 data 里会带 wxid / nickName。
        若返回 data.url，表示需要人脸/滑块验证。
        """
        a = self.cfg.adapter
        if not self._login.get("uuid"):
            return {"ok": False, "message": "请先获取二维码"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=25.0) as c:
                r = await c.post(
                    f"{a.gewe_base_url.rstrip('/')}/v2/api/login/checkLogin",
                    json={
                        "appId": self._login["app_id"],
                        "proxyIp": "",
                        "uuid": self._login["uuid"],
                        "autoSliding": False,
                    },
                    headers={"X-GEWE-TOKEN": a.gewe_token},
                )
                r.raise_for_status()
                d = r.json()
            data = d.get("data") or {}

            # 需要人脸/滑块验证
            if data.get("url") and not data.get("wxid"):
                self._login["status"] = "verify"
                self._login["message"] = "需要人脸/滑块验证，请在手机端完成"
                self._login["verify_url"] = data["url"]
                return {"ok": True, **self._login}

            if data.get("wxid"):
                self._login["status"] = "confirmed"
                self._login["message"] = "登录成功"
                # 登录后自动回填配置，免去用户手抄 wxid
                cfg = self.config_store.load()
                cfg.adapter.bot_wxid = data.get("wxid", "")
                cfg.adapter.gewe_app_id = self._login["app_id"]
                if data.get("nickName"):
                    cfg.adapter.bot_nickname = cfg.adapter.bot_nickname or data["nickName"]
                self.config_store.save(cfg)
                self.cfg = cfg
                return {"ok": True, **self._login}

            return {"ok": True, "status": self._login["status"],
                    "message": self._login.get("message") or "等待扫码…"}
        except Exception as e:
            self._login["status"] = "failed"
            self._login["message"] = str(e)
            return {"ok": False, "message": f"检查登录失败：{e}"}

    # ── 群配置 ────────────────────────────────────────────────

    def upsert_group(self, chat_id: str, data: dict) -> dict:
        cfg = self.config_store.load()
        item = cfg.groups.get(chat_id) or GroupConfigItem(chat_id=chat_id)
        for k, v in data.items():
            if k in GroupConfigItem.__dataclass_fields__ and k != "chat_id":
                setattr(item, k, v)
        item.chat_id = chat_id
        cfg.groups[chat_id] = item
        self.config_store.save(cfg)
        self.cfg = cfg
        return {"ok": True}

    def remove_group(self, chat_id: str) -> dict:
        cfg = self.config_store.load()
        cfg.groups.pop(chat_id, None)
        self.config_store.save(cfg)
        self.cfg = cfg
        return {"ok": True}
