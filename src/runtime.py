"""机器人运行时 —— 管理生命周期、状态、消息日志与扫码登录。

这一层把「引擎」和「界面」解耦：
  · Web UI 只调用这里暴露的方法，不直接碰适配器/编排层
  · 未配置或启动失败时不会拖垮 Web 服务，界面始终可访问
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from .adapters.base import BaseAdapter
from .adapters.gewechat import GewechatAdapter
from .adapters.hybrid import HybridAdapterRouter
from .adapters.ilink import ILinkAdapter
from .adapters.onebot import OneBotAdapter
from .adapters.wechatpadpro import WeChatPadProAdapter, WePadProError
from .config_store import AppConfig, ConfigStore, GroupConfigItem
from .core.context import ContextManager
from .core.docker import (NAPCAT_CONTAINER, NAPCAT_HTTP_PORT,
                          NAPCAT_WEBUI_PORT, DockerManager, GATEWAY_URL)
from .core.napcat import NapCatWebUI
from .core.qr import QRDataTooLong, data_url
from .core.http import explain_error, make_client
from .core.llm import JudgeLLM, LLMGateway, ProviderCfg
from .core.memory import CATEGORIES, SCOPE_GROUP, SCOPE_USER, PersonaMemory
from .core.sender import HumanLikeSender
from .core.trigger import GroupConfig, TriggerRouter, default_group_config
from .core.types import WxMessage
from .orchestrator import Orchestrator
from .store_memory import MemoryStore

log = logging.getLogger(__name__)


def _first_key(d: dict, *names: str) -> object:
    """按候选键名取第一个非空值（协议端字段大小写不统一）。"""
    for n in names:
        if d.get(n) not in (None, ""):
            return d[n]
    return ""



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
    replied: bool = False
    platform: str = ""      # wechat | qq

    def to_dict(self) -> dict:
        return {
            "ts": self.ts, "direction": self.direction,
            "chat_type": self.chat_type, "chat_id": self.chat_id,
            "chat_name": self.chat_name, "sender": self.sender,
            "content": self.content,
            "trigger_layer": self.trigger_layer,
            "trigger_reason": self.trigger_reason,
            "replied": self.replied,
            "platform": self.platform,
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
                    "platform": entry.platform,
                }, ensure_ascii=False) + "\n")
        except Exception:
            log.debug("消息日志写盘失败", exc_info=True)

    def add_in(self, msg: WxMessage, layer: str = "", reason: str = "",
               replied: bool = False, platform: str = "") -> None:
        e = LogEntry(
            ts=msg.timestamp or int(time.time() * 1000), direction="in",
            chat_type=msg.chat_type, chat_id=msg.chat_id,
            chat_name=msg.sender_name if not msg.is_group else "",
            sender=msg.sender_name or msg.sender_id, content=msg.content,
            trigger_layer=layer, trigger_reason=reason, replied=replied,
            platform=platform or msg.platform,
        )
        self._buf.append(e)
        self._append_file(e)
        self._recount(e)

    def add_out(self, msg: WxMessage, text: str, platform: str = "") -> None:
        e = LogEntry(
            ts=int(time.time() * 1000), direction="out",
            chat_type=msg.chat_type, chat_id=msg.chat_id,
            chat_name="", sender="机器人", content=text,
            platform=platform or msg.platform,
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
    """机器人运行时 —— 支持微信与 QQ 多通道并行。"""

    def __init__(self, store: ConfigStore, data_dir: Path):
        self.config_store = store
        self.data_dir = Path(data_dir)
        self.log = MessageLog(self.data_dir / "messages.jsonl")
        self.docker = DockerManager()
        self._napcat_webui_token = ""        # 由部署流程生成
        self._napcat_webui: Optional[NapCatWebUI] = None
        self.memory: Optional[PersonaMemory] = None

        self.cfg: AppConfig = store.load()

        # 双通道（独立适配器 + 独立编排器）
        self.wechat_adapter: Optional[BaseAdapter] = None
        self.wechat_orch: Optional[Orchestrator] = None
        self.qq_adapter: Optional[BaseAdapter] = None
        self.qq_orch: Optional[Orchestrator] = None

        self.running = False
        self.last_error = ""
        self.started_at = 0.0
        self.platforms_running: set[str] = set()   # {"wechat", "qq"}

        self._login: dict = {"status": "idle", "qrcode": "", "image": "",
                             "uuid": "", "app_id": "", "message": "",
                             "channel": ""}      # gewechat | ilink

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self) -> dict:
        if self.running:
            return {"ok": True, "message": "机器人已在运行"}
        self.cfg = self.config_store.load()
        try:
            self._validate()
        except ValueError as e:
            self.last_error = str(e)
            log.warning("启动验证未通过：%s", e)
            return {"ok": False, "message": str(e)}

        results: list[dict] = []
        a = self.cfg.adapter

        if a.wechat_enabled:
            try:
                self.wechat_adapter = self._build_wechat_adapter()
                self.wechat_orch = self._build_orchestrator(
                    self.wechat_adapter, platform="wechat"
                )
                await self.wechat_adapter.start()
                self.platforms_running.add("wechat")
                results.append({"ok": True, "platform": "wechat",
                                "message": "微信通道已启动"})
                log.info("微信通道已启动")
            except Exception as e:
                log.exception("微信通道启动失败")
                results.append({"ok": False, "platform": "wechat",
                                "message": f"微信通道启动失败：{e}"})

        if a.qq_enabled:
            try:
                self.qq_adapter = self._build_qq_adapter()
                self.qq_orch = self._build_orchestrator(
                    self.qq_adapter, platform="qq"
                )
                await self.qq_adapter.start()
                self.platforms_running.add("qq")
                results.append({"ok": True, "platform": "qq",
                                "message": "QQ 通道已启动"})
                log.info("QQ 通道已启动")
            except Exception as e:
                log.exception("QQ 通道启动失败")
                results.append({"ok": False, "platform": "qq",
                                "message": f"QQ 通道启动失败：{e}"})

        any_ok = any(r["ok"] for r in results)
        self.running = any_ok
        if any_ok:
            self.last_error = ""
            self.started_at = time.time()
            msgs = [r["message"] for r in results]
            return {"ok": True, "message": "；".join(msgs),
                    "details": results}
        # 全失败了
        err = "；".join(r["message"] for r in results)
        self.last_error = err
        return {"ok": False, "message": err, "details": results}

    async def stop(self) -> dict:
        for name, adapter in (("wechat", self.wechat_adapter),
                               ("qq", self.qq_adapter)):
            if adapter is not None:
                try:
                    await adapter.stop()
                except Exception:
                    log.warning("%s 通道关闭异常", name, exc_info=True)
        self.wechat_adapter = None
        self.wechat_orch = None
        self.qq_adapter = None
        self.qq_orch = None
        self.running = False
        self.platforms_running.clear()
        return {"ok": True, "message": "机器人已停止"}

    async def restart(self) -> dict:
        await self.stop()
        await asyncio.sleep(0.3)
        return await self.start()

    def status(self) -> dict:
        st = {
            "running": self.running,
            "platforms": list(self.platforms_running),
            "uptime": int(time.time() - self.started_at) if self.running else 0,
            "last_error": self.last_error,
            "login": self._login,
            "configured": bool(self.cfg.llm.primary_key),
            "stats": self.log.stats(),
        }
        # 各通道独立状态
        for name, adapter in (("wechat", self.wechat_adapter),
                               ("qq", self.qq_adapter)):
            if adapter is not None:
                st[f"{name}_platform"] = adapter.platform
                st[f"{name}_supports_group"] = adapter.supports_group
                st[f"{name}_running"] = name in self.platforms_running
            else:
                st[f"{name}_running"] = False
        return st

    # ── 构建 ──────────────────────────────────────────────────

    def _validate(self) -> None:
        a, l = self.cfg.adapter, self.cfg.llm
        if a.wechat_enabled:
            # 各通道的凭据要求完全不同，别互相串
            if a.wechat_type == "gewechat" and not a.gewe_token:
                raise ValueError("微信通道：未填写 GeWe Token（该通道已停服，建议改用 WeChatPadPro）")
            if a.wechat_type == "wechatpadpro" and not a.wpp_auth_key:
                raise ValueError("微信协议端：还没有授权码，请到「接入设置」点「获取授权码」")
            if a.wechat_type == "ilink" and not self.ilink_status().get("loggedIn"):
                raise ValueError("官方通道还没扫码登录，请到「接入设置」点「获取登录二维码」")
        if a.qq_enabled and not a.onebot_http_url:
            raise ValueError("QQ 通道：未填写 OneBot 网关地址")
        if not l.primary_key:
            raise ValueError("未填写大模型 API Key，请到「模型设置」完善")

    def _build_group_adapter(self) -> BaseAdapter:
        """群聊通道。

        GeWe 已停服（README 明确写「技术归档、不再提供镜像」），
        所以协议端换成 WeChatPadPro。两者接口形状相近，
        但**鉴权模型完全不同**：GeWe 用 `X-GEWE-TOKEN` 请求头，
        WeChatPadPro 用 `?key=<授权码>` 查询参数。
        """
        a = self.cfg.adapter
        if a.wechat_type == "gewechat":
            # 保留兼容：老配置仍能跑（但上游已停服，界面会提示）
            return GewechatAdapter(
                base_url=a.gewe_base_url, token=a.gewe_token, app_id=a.gewe_app_id,
                bot_wxid=a.bot_wxid, bot_nickname=a.bot_nickname or self.cfg.bot.name,
            )
        return WeChatPadProAdapter(
            base_url=a.wpp_base_url or "http://127.0.0.1:1238",
            auth_key=a.wpp_auth_key,
            bot_wxid=a.bot_wxid, bot_nickname=a.bot_nickname or self.cfg.bot.name,
        )

    def _build_wechat_adapter(self) -> BaseAdapter:
        a = self.cfg.adapter
        if a.wechat_type == "ilink":
            return ILinkAdapter(state_dir=str(self._ilink_state_dir()),
                                bot_agent="WxBot/1.0")
        group = self._build_group_adapter()
        if a.hybrid_enabled:
            official = ILinkAdapter(state_dir=str(self._ilink_state_dir()),
                                    bot_agent="WxBot/1.0")
            return HybridAdapterRouter(private_adapter=official, group_adapter=group)
        return group

    def _build_qq_adapter(self) -> BaseAdapter:
        a = self.cfg.adapter
        return OneBotAdapter(
            http_url=a.onebot_http_url, access_token=a.onebot_token,
            bot_qq=a.bot_qq, bot_nickname=a.bot_nickname or self.cfg.bot.name,
        )

    def _build_orchestrator(self, adapter: BaseAdapter,
                            platform: str = "") -> Orchestrator:
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

        memory = PersonaMemory(
            self.data_dir / "memory.json", llm=judge_gw,
            enabled=bool(cfg.bot.memory_enabled),
        )
        memory.extract_interval = max(1, int(cfg.bot.memory_extract_interval or 3))
        memory.inject = bool(cfg.bot.memory_inject)
        self.memory = memory

        orch = Orchestrator(
            adapter=adapter, store=store, llm=chat_gw, judge=judge,
            ctx=ContextManager(store), router=router,
            sender=HumanLikeSender(adapter), memory=memory,
        )
        self._wire_logging(orch, adapter, platform)
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

    def _wire_logging(self, orch: Orchestrator, adapter: BaseAdapter,
                      platform: str = "") -> None:
        """挂日志钩子，让界面能看到每条消息的触发判定结果。"""
        last: dict = {"layer": "", "reason": ""}
        plat = platform or adapter.platform

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
                                last.get("reason", ""), replied,
                                platform=plat)

        orch._process = process_wrapped             # type: ignore[method-assign]

        # 出站消息也记一笔
        orig_send = adapter.send_text

        async def send_wrapped(chat_id: str, text: str, at=None) -> str:
            mid = await orig_send(chat_id, text, at=at)
            self.log.add_out(WxMessage(
                msg_id="", platform=plat,
                chat_type="group" if "@chatroom" in chat_id or chat_id.startswith("qq:group:") else "private",
                chat_id=chat_id, sender_id="", sender_name="", bot_id="",
                content=text,
            ), text, platform=plat)
            return mid

        adapter.send_text = send_wrapped            # type: ignore[method-assign]

    # ══ QQ / OneBot 连接测试 ═══════════════════════════════════

    async def test_onebot(self, http_url: str = "", token: str = "") -> dict:
        """测试 OneBot 网关连通性与 Token 有效性。"""
        a = self.cfg.adapter
        base = (http_url or a.onebot_http_url or "").rstrip("/")
        tok = token if token else a.onebot_token

        if not base:
            return {"ok": False, "stage": "config", "message": "请先填写 HTTP 接口地址"}

        probe = OneBotAdapter(http_url=base, access_token=tok)
        try:
            r = await probe._call("get_login_info")

            if r.get("status") == "failed":
                code = r.get("retcode")
                if code == 403:
                    return {"ok": False, "stage": "auth",
                            "message": "网关可以连接，但 Access Token 不正确",
                            "detail": "网关返回 403",
                            "hint": "请检查 Token 是否与网关配置里的一致。"}
                return {"ok": False, "stage": "connect",
                        "message": f"连不上 OneBot 网关 {base}",
                        "detail": r.get("message", ""),
                        "hint": "请确认网关已启动、HTTP 服务已开启，"
                                "且地址与端口填写正确。"}

            data = r.get("data") or {}
            qq = str(data.get("user_id") or "")
            nick = data.get("nickname") or ""

            # 顺便把 QQ 号回填，省得用户手填
            if qq:
                cfg = self.config_store.load()
                cfg.adapter.bot_qq = qq
                if nick and not cfg.adapter.bot_nickname:
                    cfg.adapter.bot_nickname = nick
                self.config_store.save(cfg)
                self.cfg = cfg

            return {
                "ok": True, "stage": "ok",
                "message": f"网关连接正常，已登录：{nick}（{qq}）" if qq else "网关连接正常",
                "qq": qq, "nickname": nick,
                "hint": "下一步：在网关里把反向 WebSocket 指向本程序，"
                        "然后就可以在 QQ 里对话了。",
            }
        finally:
            try:
                await probe._client.aclose()
            except Exception:
                pass

    # ══ QQ / NapCat 自动探测 ═══════════════════════════════════

    QQ_PROBE_CANDIDATES = [
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:8080",
        "http://localhost:3000",
        "http://localhost:3001",
    ]

    async def detect_napcat(self) -> dict:
        """自动探测本地是否已有 OneBot / NapCat 网关在运行。"""
        found = []
        for url in self.QQ_PROBE_CANDIDATES:
            try:
                async with make_client(url, timeout=3.0) as c:
                    r = await c.post(f"{url}/get_login_info", json={},
                                     headers={"Authorization": "Bearer "})
                    if r.status_code == 200:
                        d = r.json()
                        if d.get("status") == "ok":
                            data = d.get("data") or {}
                            found.append({
                                "url": url,
                                "qq": str(data.get("user_id") or ""),
                                "nickname": data.get("nickname") or "",
                            })
                            break  # 找到一个就够了
            except Exception:
                continue

        if found:
            # 自动回填到配置
            cfg = self.config_store.load()
            cfg.adapter.onebot_http_url = found[0]["url"]
            qq = found[0]["qq"]
            if qq and not cfg.adapter.bot_qq:
                cfg.adapter.bot_qq = qq
            self.config_store.save(cfg)
            self.cfg = cfg

        return {
            "ok": bool(found),
            "found": found,
            "message": (
                f"发现 {len(found)} 个本地 OneBot 网关"
                if found else
                "没有探测到正在运行的 OneBot 网关。"
                "若尚未部署，请按下方指引启动一个。"
            ),
        }

    # ══ QQ / NapCat 一键部署 ══════════════════════════════════
    #
    # 与 GeWe 一样：用户只点一个按钮，程序负责拉镜像、灌配置、起容器、扫码。
    # 差别在于 NapCat 还要过 WebUI 鉴权（见 core/napcat.py）。

    def _napcat_ws_url(self) -> str:
        """NapCat 反向连过来的地址。

        容器内访问宿主机必须用 `host.docker.internal`（127.0.0.1 是容器自己）。
        macOS / Windows 的 Docker Desktop 自带这个域名；
        Linux 由 `ensure_napcat(extra_hosts=True)` 加 `--add-host` 映射。
        """
        port = int(os.environ.get("WXBOT_PORT", "8000"))
        return f"ws://host.docker.internal:{port}/ws/onebot"

    def _ensure_napcat_webui_token(self) -> str:
        """WebUI Token 持久化在配置里，重启后仍能登录同一个容器。"""
        cfg = self.config_store.load()
        tok = getattr(cfg.adapter, "napcat_webui_token", "") or ""
        if not tok:
            tok = secrets.token_hex(16)
            cfg.adapter.napcat_webui_token = tok
            self.config_store.save(cfg)
            self.cfg = cfg
        self._napcat_webui_token = tok
        return tok

    def _webui_client(self) -> NapCatWebUI:
        if self._napcat_webui is None:
            self._napcat_webui = NapCatWebUI(
                token=self._ensure_napcat_webui_token(), port=NAPCAT_WEBUI_PORT,
            )
        return self._napcat_webui

    async def qq_status(self) -> dict:
        """QQ 通道状态：容器 + WebUI 登录态。供前端轮询。"""
        cfg = self.config_store.load()
        napcat = self.docker.napcat_status()
        st = {
            "docker": self.docker.detect().to_dict(),
            "napcat": napcat,
            "task": dict(self.docker.task),
            "onebotUrl": cfg.adapter.onebot_http_url,
            "botQq": cfg.adapter.bot_qq,
            "stage": "idle",
            "loggedIn": False,
            "loginMessage": "",
        }
        # 只读状态接口不要每次都去登录 WebUI（慢且会触发限速），
        # 只有容器在跑、且确实需要判断登录态时才查。
        if napcat.get("running") and cfg.adapter.onebot_http_url:
            try:
                r = await self.test_onebot(cfg.adapter.onebot_http_url,
                                           cfg.adapter.onebot_token)
                st["loggedIn"] = bool(r.get("ok"))
                st["loginMessage"] = r.get("message", "")
                st["stage"] = "ready" if r.get("ok") else "waiting_login"
            except Exception:
                log.debug("查询 QQ 登录态失败", exc_info=True)
        return st

    async def qq_setup(self) -> dict:
        """一键部署 QQ 网关（非阻塞，前端轮询 `/api/qq/status`）。"""
        token = self._ensure_napcat_webui_token()
        ws = self._napcat_ws_url()
        cfg = self.config_store.load()
        if not cfg.adapter.onebot_http_url:
            cfg.adapter.onebot_http_url = f"http://127.0.0.1:{NAPCAT_HTTP_PORT}"
            self.config_store.save(cfg)
            self.cfg = cfg
        return await self.docker.ensure_napcat(token, ws)

    async def qq_login_qrcode(self) -> dict:
        """取 QQ 登录二维码（SVG data URL）。"""
        return await self._webui_client().qrcode()

    async def qq_login_check(self) -> dict:
        """查 QQ 扫码状态；登录成功后顺手把 QQ 号回填到配置。"""
        r = await self._webui_client().check_login()
        if r.get("loggedIn"):
            info = await self.test_onebot()
            if info.get("ok") and info.get("qq"):
                r["qq"] = info["qq"]
                r["nickname"] = info.get("nickname", "")
        return r

    async def qq_stop(self) -> dict:
        return await self.docker.stop_napcat()

    async def qq_logs(self, lines: int = 100) -> dict:
        return await self.docker.napcat_logs(lines)

    # ══ 人格知识库 ════════════════════════════════════════════

    def _ensure_memory(self) -> PersonaMemory:
        """知识库实例在机器人启动时创建；未启动时按需创建一个只读实例，
        这样用户不启动机器人也能查看/编辑已有记忆。"""
        if self.memory is None:
            self.memory = PersonaMemory(
                self.data_dir / "memory.json", llm=None,
                enabled=bool(self.cfg.bot.memory_enabled),
            )
        return self.memory

    def memory_list(self, scope: str = "", owner_id: str = "",
                    category: str = "", keyword: str = "") -> dict:
        m = self._ensure_memory()
        return {
            "items": m.list_items(scope, owner_id, category, keyword),
            "stats": m.stats(),
            "categories": CATEGORIES,
        }

    def memory_add(self, scope: str, owner_id: str, category: str,
                   content: str, keywords: list[str] | None = None) -> dict:
        m = self._ensure_memory()
        if not content.strip():
            return {"ok": False, "message": "内容不能为空"}
        item = m.add_manual(
            scope=scope if scope in (SCOPE_USER, SCOPE_GROUP) else SCOPE_USER,
            owner_id=owner_id, category=category if category in CATEGORIES else "fact",
            content=content, keywords=keywords,
        )
        return {"ok": True, "item": item}

    def memory_delete(self, item_id: str) -> dict:
        ok = self._ensure_memory().delete(item_id)
        return {"ok": ok, "message": "已删除" if ok else "未找到该条记忆"}

    def memory_clear(self, scope: str = "", owner_id: str = "") -> dict:
        n = self._ensure_memory().clear(scope, owner_id)
        return {"ok": True, "message": f"已清除 {n} 条记忆"}

    def memory_stats(self) -> dict:
        m = self._ensure_memory()
        s = m.stats()
        s["enabled"] = bool(self.cfg.bot.memory_enabled)
        s["inject"] = bool(self.cfg.bot.memory_inject)
        s["categories"] = CATEGORIES
        return s

    # ══ Docker 环境与网关容器 ═════════════════════════════════
    #
    # GeWe 接入恒定走本地 Docker：不引导用户去用云端服务，
    # 也不要求用户自己敲 docker 命令。用户只需点「一键配置」。

    def docker_status(self) -> dict:
        """Docker + 网关的完整状态快照。

        ⚠️ `gateway.running`（端口通）只代表容器起来了，
        **不代表配置完成** —— Token 仍可能是空的。
        所以额外给一个 `tokenConfigured`，前端不要自己从"网关在跑"推断可用。
        """
        d = self.docker.detect()
        gw = self.docker.gateway_status()
        cfg = self.config_store.load()
        gw = dict(gw)
        gw["tokenConfigured"] = bool(cfg.adapter.gewe_token)
        gw["baseUrl"] = cfg.adapter.gewe_base_url or GATEWAY_URL
        return {
            "docker": d.to_dict(),
            "gateway": gw,
            "task": dict(self.docker.task),
            "plan": self.docker.install_plan() if not d.running else {"needed": False},
            "gatewayUrl": GATEWAY_URL,
        }

    async def docker_prepare(self) -> dict:
        """一键准备 Docker 环境（按需安装 / 启动）。"""
        return await self.docker.prepare()

    async def docker_start(self) -> dict:
        """仅启动 Docker 服务。"""
        return await self.docker.start_daemon()

    async def gateway_setup(self) -> dict:
        """拉取并启动网关容器，成功后自动回填网关地址。"""
        return await self.docker.ensure_gateway()

    async def gateway_stop(self) -> dict:
        return await self.docker.stop_gateway()

    async def gateway_logs(self, lines: int = 100) -> dict:
        return await self.docker.gateway_logs(lines)

    def apply_gateway_url(self) -> None:
        """把网关地址回填到配置（用户无需手填）。"""
        cfg = self.config_store.load()
        if cfg.adapter.gewe_base_url != GATEWAY_URL:
            cfg.adapter.gewe_base_url = GATEWAY_URL
            self.config_store.save(cfg)
            self.cfg = cfg

    # ══ 网关连接：自动探测 + 连通性测试 ═══════════════════════
    #
    # 对非技术用户来说，"填写网关地址和 Token"是最容易卡住的一步。
    # 所以这里提供两个能力，让界面可以引导用户而不是让他自己猜：
    #   · detect_gateway()  自动探测常见地址，告诉用户"哪个能用"
    #   · test_gateway()    验证连通性与 Token，出错时给出人话解释

    # 恒定使用本地 Docker 网关，只探测本机地址
    GATEWAY_CANDIDATES = [
        (GATEWAY_URL, "本机 Docker 网关", "容器默认端口 2531"),
        ("http://localhost:2531", "本机 Docker 网关", "等价写法"),
        ("http://127.0.0.1:2532", "本机 Docker 网关（备用端口）", "部分部署用 2532"),
    ]

    async def _gateway_call(self, endpoint: str, body: dict,
                            base_url: str = "", token: str = "",
                            timeout: float = 25.0,
                            allow_empty_token: bool = False) -> dict:
        """调用 GeWe 网关。

        统一处理两件容易出错的事：
          1. **代理绕过** —— 本机地址若走系统代理会返回 502（见 core/http.py）
          2. **错误翻译** —— 把技术异常转成用户能看懂并知道怎么处理的话

        `allow_empty_token=True` 用于 `tools/getTokenId` 这类**换取 Token 的接口**
        —— 它们本来就还没 Token，不能提前拦下来。

        返回 {"ok": bool, "data": dict, "error": str, "status": int}
        """
        a = self.cfg.adapter
        base = (base_url or a.gewe_base_url or "").rstrip("/")
        tok = token or a.gewe_token
        if not base:
            return {"ok": False, "status": 0, "error": "还没有填写网关地址"}
        if not tok and not allow_empty_token:
            return {"ok": False, "status": 0, "error": "还没有填写 Token"}

        url = f"{base}/v2/api/{endpoint}"
        try:
            async with make_client(base, timeout=timeout) as c:
                r = await c.post(url, json=body, headers={"X-GEWE-TOKEN": tok})
                if r.status_code >= 400:
                    return {
                        "ok": False, "status": r.status_code,
                        "error": explain_error(
                            httpx.HTTPStatusError(
                                f"HTTP {r.status_code}", request=r.request, response=r
                            ), url,
                        ),
                    }
                return {"ok": True, "status": r.status_code, "data": r.json()}
        except httpx.HTTPStatusError as e:
            return {"ok": False, "status": e.response.status_code,
                    "error": explain_error(e, url)}
        except Exception as e:
            return {"ok": False, "status": 0, "error": explain_error(e, url)}

    # ══ GeWe Token 自动获取 ══════════════════════════════════
    #
    # 接口：POST {base}/v2/api/tools/getTokenId   body {}
    # 响应：{"ret":200,"msg":"操作成功","data":"<token>"}
    #
    # ⚠️ 诚实提示：上游 Devo919/Gewechat 仓库已声明停止提供运行服务与镜像，
    # 只作技术归档。本地已部署的旧镜像仍可能可用，但不保证 ——
    # 所以这里失败时要把原因说清楚，不能承诺"一定拿得到"。

    async def fetch_gewe_token(self, base_url: str = "") -> dict:
        """向本地网关换取 Token，并写回配置。"""
        base = (base_url or self.cfg.adapter.gewe_base_url or "").rstrip("/")
        if not base:
            return {"ok": False, "message": "请先填写网关地址"}

        r = await self._gateway_call(
            "tools/getTokenId", {}, base_url=base, timeout=15.0,
            allow_empty_token=True,
        )
        if not r.get("ok"):
            if r.get("status", 0) == 0:
                return {
                    "ok": False, "stage": "connect",
                    "message": f"连不上网关 {base}",
                    "detail": r.get("error", ""),
                    "hint": "请先确认网关容器已启动（可以点「一键完成配置」）。",
                }
            return {
                "ok": False, "stage": "http",
                "message": f"网关返回 HTTP {r.get('status')}",
                "detail": r.get("error", ""),
                "hint": "这个网关镜像可能不支持自动获取 Token，"
                        "请到 GeWe 控制台手动复制后填到输入框里。",
            }

        body = r.get("data") or {}
        ret = body.get("ret")
        token = body.get("data")
        if ret not in (200, 0) or not isinstance(token, str) or not token.strip():
            return {
                "ok": False, "stage": "api",
                "message": "网关没有返回 Token",
                "detail": f"ret={ret} msg={body.get('msg') or ''} data={token!r}",
                "hint": "部分网关版本未开放该接口，"
                        "请到 GeWe 控制台手动复制 Token 填入输入框。",
            }

        token = token.strip()
        cfg = self.config_store.load()
        cfg.adapter.gewe_base_url = base
        cfg.adapter.gewe_token = token
        self.config_store.save(cfg)
        self.cfg = cfg

        # 顺手验一下这个 Token 是否真的能用
        check = await self._gateway_call(
            "login/checkLogin", {"appId": cfg.adapter.gewe_app_id},
            base_url=base, token=token, timeout=10.0,
        )
        verified = bool(check.get("ok")) and (check.get("data") or {}).get("ret") in (200, 0)
        return {
            "ok": True,
            "tokenConfigured": True,
            "verified": verified,
            "message": "Token 已自动获取并保存" + ("，且验证有效" if verified else ""),
            "hint": "" if verified else "Token 已保存，但暂时没能验证有效性，可稍后点「测试连接」确认。",
        }

    async def detect_gateway(self) -> dict:
        """自动探测可用的网关地址。

        对每个候选地址做一次轻量请求：
          · 拿到任何 HTTP 响应 → 该地址上有服务在运行
          · 连接失败        → 跳过
        """
        results = []
        for url, label, note in self.GATEWAY_CANDIDATES:
            r = await self._gateway_call(
                "login/checkLogin", {"appId": self.cfg.adapter.gewe_app_id},
                base_url=url, timeout=6.0,
            )
            reachable = r.get("status", 0) > 0
            results.append({
                "url": url, "label": label, "note": note,
                "reachable": reachable,
                "status": r.get("status", 0),
                "error": "" if reachable else r.get("error", ""),
            })

        found = [x for x in results if x["reachable"]]
        return {
            "ok": True,
            "found": found,
            "all": results,
            "message": (
                f"发现 {len(found)} 个可用的网关地址"
                if found else
                "没有探测到正在运行的网关服务。若尚未部署，请先按下方指引启动一个。"
            ),
        }

    async def test_gateway(self, base_url: str = "", token: str = "",
                           app_id: str = "") -> dict:
        """测试网关连通性与 Token 有效性。"""
        a = self.cfg.adapter
        base = (base_url or a.gewe_base_url or "").rstrip("/")
        tok = token or a.gewe_token

        if not base:
            return {"ok": False, "stage": "config", "message": "请先填写网关地址"}
        if not tok:
            return {"ok": False, "stage": "config", "message": "请先填写 Token"}

        # 第 1 步：能不能连上
        r = await self._gateway_call(
            "login/checkLogin", {"appId": app_id or a.gewe_app_id},
            base_url=base, token=tok, timeout=10.0,
        )
        if r.get("status", 0) == 0:
            return {
                "ok": False, "stage": "connect",
                "message": f"连不上网关 {base}",
                "detail": r.get("error", ""),
                "hint": "请确认网关服务已启动；若你本机装了代理软件，"
                        "本机地址不应经过代理（系统已自动绕过，但请确认地址填写正确）。",
            }

        # 第 2 步：Token 是否有效
        body = r.get("data") or {}
        ret = body.get("ret")
        if ret not in (200, 0):
            return {
                "ok": False, "stage": "auth",
                "message": "网关可以连接，但 Token 校验未通过",
                "detail": f"ret={ret} msg={body.get('msg') or ''}",
                "hint": "请到 GeWe 控制台重新复制 Token（注意不要带多余空格）。",
            }

        # 第 3 步：设备是否已登录
        data = body.get("data") or {}
        logged_in = bool(data.get("wxid") or data.get("nickName"))
        return {
            "ok": True, "stage": "ok",
            "message": "网关连接正常，Token 有效" + ("，且已登录微信" if logged_in else ""),
            "logged_in": logged_in,
            "wxid": data.get("wxid", ""),
            "nickName": data.get("nickName", ""),
            "detail": "",
            "hint": "" if logged_in else "下一步：点击「获取登录二维码」完成微信登录。",
        }

    # ── 扫码登录 ──────────────────────────────────────────────

    # ══ WeChatPadPro 微信协议端 ═══════════════════════════════
    #
    # 用于微信群聊（官方 iLink 不开放群聊，这是服务端限制）。
    # 鉴权两步走：ADMIN_KEY → /admin/GenAuthKey1 → authKey → 业务接口 ?key=
    #
    # 分发说明（重要）：这是第三方闭源二进制，**不在本仓库分发**。
    # 程序按以下顺序找它：配置里的路径 → 数据目录 → 常见下载目录。

    WPP_BINARY_HINTS = (
        "wechatpadpro-macos-arm64",
        "wechatpadpro-macos-x64",
        "wechatpadpro-linux-amd64",
        "wechatpadpro",
    )

    def find_wpp_binary(self) -> str:
        """找协议端可执行文件。找不到返回空串（不是报错）。

        查找顺序：
          ① 配置里显式指定的路径
          ② 数据目录下的 `wechatpadpro/`
          ③ 用户「下载」/「桌面」目录里解压出来的 wechatpadpro* 文件夹

        ⚠️ 这个二进制是**第三方闭源程序，不在本仓库分发**，
        所以只能"帮用户找到他自己下载的那份"。
        """
        raw = (self.cfg.adapter.wpp_binary or "").strip()
        if raw:
            p = Path(raw).expanduser()
            if p.is_file():
                return str(p)

        roots = [self.data_dir / "wechatpadpro",
                 Path.home() / "Downloads",
                 Path.home() / "Desktop"]
        for root in roots:
            if not root.is_dir():
                continue
            # 两种常见布局：
            #   <root>/wechatpadpro-xxx/wechatpadpro-macos-arm64-xxx   （解压后一层）
            #   <root>/<任意名>/wechatpadpro-macos-arm64-xxx
            for hint in self.WPP_BINARY_HINTS:
                for pat in (f"{hint}/{hint}*", f"*/{hint}*", f"{hint}*"):
                    try:
                        for p in sorted(root.glob(pat)):
                            if p.is_file() and os.access(p, os.X_OK):
                                return str(p)
                    except OSError:
                        continue
        return ""

    async def _wpp_admin_call(self, path: str, body: dict | None = None,
                              timeout: float = 20.0) -> dict:
        """用 ADMIN_KEY 调管理接口，返回解析后的 JSON（失败返回 ok=False）。"""
        a = self.cfg.adapter
        base = (a.wpp_base_url or "http://127.0.0.1:1238").rstrip("/")
        admin = a.wpp_admin_key
        if not admin:
            return {"ok": False, "message": "请先填写协议端的管理员密钥（ADMIN_KEY）",
                    "hint": "管理员密钥在协议端的 .env 文件里（项名 ADMIN_KEY）。"}
        url = f"{base}{path}"
        try:
            async with make_client(base, timeout=timeout) as c:
                if body is None:
                    r = await c.get(url, params={"key": admin})
                else:
                    r = await c.post(url, params={"key": admin}, json=body)
                data = r.json()
        except Exception as e:
            return {"ok": False, "stage": "connect",
                    "message": "连不上微信协议端",
                    "detail": explain_error(e, url),
                    "hint": "请先点「启动微信协议端」。"}
        if data.get("Code") != 200:
            return {"ok": False, "stage": "auth",
                    "message": data.get("Text") or "管理接口调用失败",
                    "hint": "请确认管理员密钥与协议端 .env 里的 ADMIN_KEY 一致。"}
        return {"ok": True, "data": data.get("Data") or {}}

    async def wpp_list_devices(self) -> dict:
        return await self._wpp_admin_call("/admin/GetAllDevices", timeout=15.0)

    async def wpp_generate_auth_key(self, days: int = 365) -> dict:
        """拿到一个可用授权码并保存。

        ⚠️ 实践发现：**重复生成新授权码反而可能不可用**。
        协议端会累积多台"设备"，而业务接口只认其中一条
        （实测：第二次生成的 key 调业务接口返回
         `{"Code":-2,"Text":"<key> 该链接不存在！"}`，第一条才正常）。

        所以顺序是：**先复用已有的有效授权码，没有再生成**。
        """
        # ① 已有授权码 → 直接复用（不重复生成）
        a = self.cfg.adapter
        if a.wpp_auth_key:
            return {"ok": True, "authKey": a.wpp_auth_key,
                    "message": "已有授权码，继续使用", "reused": True}

        # ② 没有 → 看协议端登记过哪些，挑一个复用
        dev = await self.wpp_list_devices()
        if dev.get("ok"):
            keys = [d.get("authKey") for d in (dev["data"].get("devices") or [])
                    if d.get("authKey")]
            if keys:
                cfg = self.config_store.load()
                cfg.adapter.wpp_auth_key = keys[0]
                cfg.adapter.wpp_base_url = (
                    a.wpp_base_url or "http://127.0.0.1:1238").rstrip("/")
                self.config_store.save(cfg)
                self.cfg = cfg
                return {"ok": True, "authKey": keys[0],
                        "message": "已复用协议端里已有的授权码", "reused": True}

        # ③ 都没有 → 新生成一个
        gen = await self._wpp_admin_call(
            "/admin/GenAuthKey1",
            {"Count": 1, "Days": days, "Remark": "wxbot"}, timeout=25.0)
        if not gen.get("ok"):
            return gen
        keys = (gen["data"].get("authKeys") or [])
        if not keys:
            return {"ok": False, "message": "协议端没有返回授权码"}

        cfg = self.config_store.load()
        cfg.adapter.wpp_auth_key = keys[0]
        cfg.adapter.wpp_base_url = (a.wpp_base_url or "http://127.0.0.1:1238").rstrip("/")
        self.config_store.save(cfg)
        self.cfg = cfg
        return {"ok": True, "authKey": keys[0],
                "message": "授权码已生成并保存"}

    def _wpp_adapter(self) -> WeChatPadProAdapter:
        a = self.cfg.adapter
        return WeChatPadProAdapter(
            base_url=a.wpp_base_url or "http://127.0.0.1:1238",
            auth_key=a.wpp_auth_key, bot_wxid=a.bot_wxid,
            bot_nickname=a.bot_nickname or self.cfg.bot.name,
        )

    async def wpp_status(self) -> dict:
        """协议端状态：进程是否在跑、授权码是否有效、是否已登录。"""
        a = self.cfg.adapter
        base = (a.wpp_base_url or "http://127.0.0.1:1238").rstrip("/")
        binary = self.find_wpp_binary()
        out = {
            "baseUrl": base,
            "binaryFound": bool(binary),
            "binary": binary,
            "hasAdminKey": bool(a.wpp_admin_key),
            "hasAuthKey": bool(a.wpp_auth_key),
            "reachable": False,
            "authValid": False,
            "loggedIn": False,
            "message": "",
        }
        if not a.wpp_auth_key:
            out["message"] = "还没有授权码"
            return out

        st = await self._wpp_adapter().login_status()
        out.update({
            "reachable": st.get("reachable", False),
            "authValid": not st.get("authInvalid", False) and st.get("reachable", False),
            "loggedIn": st.get("loggedIn", False),
            "wxid": st.get("wxid", ""),
            "nickname": st.get("nickname", ""),
            "message": st.get("message", ""),
            "hint": st.get("hint", ""),
        })
        return out

    async def wpp_login_qrcode(self) -> dict:
        """取 Pad 协议登录二维码。

        协议端直接返回 `qrCodeBase64`（data URL），不需要我们自己编码。
        """
        a = self.cfg.adapter
        base = (a.wpp_base_url or "http://127.0.0.1:1238").rstrip("/")
        if not a.wpp_auth_key:
            gen = await self.wpp_generate_auth_key()
            if not gen.get("ok"):
                return gen
        adapter = self._wpp_adapter()
        try:
            data = await adapter._call_ok(
                "POST", "/login/GetLoginQrCodePad",
                {"Check": True, "Proxy": ""}, timeout=40.0)
        except WePadProError as e:
            return {"ok": False, "message": str(e),
                    "hint": "请确认协议端已启动、授权码有效。"}
        finally:
            await adapter.stop()

        img = str(_first_key(data, "qrCodeBase64", "QrCodeBase64") or "")
        link = str(_first_key(data, "QrLink", "QrCodeUrl") or "")
        if not img:
            return {"ok": False, "message": "协议端没有返回二维码图片"}
        return {
            "ok": True, "image": img, "url": link,
            "expiredIn": data.get("expiredTime") or 0,
            "channel": "wechatpadpro",
            "message": "请用微信扫码登录",
            "hint": "这是 iPad 协议通道，可用于群聊；请用小号，注意风控。",
        }

    async def wpp_login_check(self) -> dict:
        adapter = self._wpp_adapter()
        try:
            st = await adapter.login_status()
        finally:
            await adapter.stop()
        if st.get("loggedIn"):
            cfg = self.config_store.load()
            if st.get("wxid"):
                cfg.adapter.bot_wxid = st["wxid"]
            if st.get("nickname"):
                cfg.adapter.bot_nickname = cfg.adapter.bot_nickname or st["nickname"]
            self.config_store.save(cfg)
            self.cfg = cfg
        return {"ok": True, **st}

    # ══ 官方 iLink 扫码登录（应用内完成，无需命令行）══════════
    #
    # 接口（已对官方服务实测）：
    #   GET /ilink/bot/get_bot_qrcode?bot_type=3
    #       → {ret:0, qrcode:"<id>", qrcode_img_content:"<URL>"}
    #         `qrcode_img_content` 是**二维码内容 URL**，需要自己转成二维码图片
    #   GET /ilink/bot/get_qrcode_status?qrcode=<id>
    #       → 服务端会 hold 住连接（最长 35 秒），所以超时要放宽
    #         status: confirmed | binded_redirect | expired | cancelled
    #
    # ⚠️ 官方通道**不需要 GeWe Token**，这里绝不能去碰 GeWe 的任何配置。

    def _ilink_state_dir(self) -> Path:
        """官方 iLink 的凭证目录。

        ⚠️ 配置里的默认值是相对路径 `./data/ilink`，但**相对的是进程工作目录**，
        而桌面版会从任意目录启动进程（数据实际在应用数据目录里）。
        所以这里要把它归一到 `self.data_dir` 下，否则会出现
        "明明扫码成功了，重启后又提示未登录"。
        """
        raw = (self.cfg.adapter.ilink_state_dir or "").strip()
        if not raw:
            return self.data_dir / "ilink"
        d = Path(raw).expanduser()
        if d.is_absolute():
            # 只有在"配置里显式写了绝对路径"时才原样使用
            return d
        # 相对路径一律以数据目录为基准（去掉可能残留的前缀 ./data/）
        name = d.name or "ilink"
        return self.data_dir / name

    async def ilink_qrcode(self) -> dict:
        """取官方 iLink 登录二维码（返回 SVG data URL）。"""
        base = "https://ilinkai.weixin.qq.com"
        url = f"{base}/ilink/bot/get_bot_qrcode"
        try:
            async with make_client(base, timeout=20.0) as c:
                r = await c.get(url, params={"bot_type": 3})
                if r.status_code >= 400:
                    return {"ok": False, "stage": "http",
                            "message": f"官方接口返回 HTTP {r.status_code}",
                            "hint": "请稍后重试；若持续失败，可能是官方服务临时不可用。"}
                body = r.json()
        except Exception as e:
            return {"ok": False, "stage": "connect",
                    "message": "连不上微信官方接口",
                    "detail": explain_error(e, url),
                    "hint": "请检查本机网络；官方接口在公网，需要能访问 "
                            "ilinkai.weixin.qq.com。"}

        if body.get("ret") not in (0, None):
            return {"ok": False, "stage": "api",
                    "message": f"官方接口返回 ret={body.get('ret')}",
                    "detail": str(body.get("errmsg") or ""),
                    "hint": "请稍后重试。"}

        qid = body.get("qrcode") or ""
        content = (body.get("qrcode_img_content") or "").strip()
        if not qid or not content:
            return {"ok": False, "message": "官方接口没有返回二维码内容"}

        # 二维码内容是一段 URL，离线转成 SVG（不依赖任何第三方服务）
        try:
            image = data_url(content, ec="M", scale=6, border=4)
        except QRDataTooLong:
            return {"ok": False, "message": "二维码内容过长，无法生成"}
        except Exception:
            log.exception("生成官方二维码失败")
            return {"ok": False, "message": "生成二维码失败"}

        self._login = {
            "status": "waiting", "channel": "ilink",
            "qrcode": qid, "image": image, "uuid": "", "app_id": "",
            "message": "请用微信扫码（二维码几分钟内有效）",
        }
        return {"ok": True, **self._login,
                "hint": "这是腾讯官方通道，只能用于私聊，零封号风险。"}

    async def ilink_login_check(self) -> dict:
        """轮询官方扫码状态；成功后把凭证落到本地。"""
        if self._login.get("channel") != "ilink" or not self._login.get("qrcode"):
            return {"ok": False, "status": "idle", "message": "请先获取二维码"}

        base = "https://ilinkai.weixin.qq.com"
        url = f"{base}/ilink/bot/get_qrcode_status"
        try:
            # 服务端会 hold 住连接最长 35 秒，超时必须比它大
            async with make_client(base, timeout=50.0) as c:
                r = await c.get(url, params={"qrcode": self._login["qrcode"]})
                if r.status_code >= 400:
                    return {"ok": False, "status": "failed",
                            "message": f"查询失败：HTTP {r.status_code}"}
                st = r.json()
        except Exception as e:
            # 长轮询偶发超时属正常，前端下一轮会再查
            return {"ok": True, "status": self._login.get("status", "waiting"),
                    "message": self._login.get("message") or "等待扫码…",
                    "detail": explain_error(e, url)}

        status = st.get("status") or ""
        if status == "confirmed":
            ok = self._save_ilink_credentials(st)
            self._login.update({"status": "confirmed", "message": "登录成功"})
            return {"ok": True, "status": "confirmed", "message": "登录成功",
                    "saved": ok}
        if status == "binded_redirect":
            # 该账号此前已绑定过，本地凭证通常仍然有效
            self._login.update({"status": "confirmed",
                                "message": "该账号已连接过，凭证可继续使用"})
            return {"ok": True, "status": "confirmed",
                    "message": "该账号已连接过，凭证可继续使用"}
        if status in ("expired", "cancelled"):
            self._login.update({"status": "failed", "message": "二维码已失效，请重新获取"})
            return {"ok": True, "status": "failed",
                    "message": "二维码已失效，请重新获取"}

        return {"ok": True, "status": "waiting", "message": "等待扫码…"}

    def _save_ilink_credentials(self, st: dict) -> bool:
        """把官方返回的凭证写入 state_dir（iLinkAdapter 启动时会读它）。"""
        token = st.get("bot_token")
        if not token:
            log.warning("官方接口未返回 bot_token，凭证未保存")
            return False
        try:
            d = Path(self._ilink_state_dir())
            d.mkdir(parents=True, exist_ok=True)
            f = d / "account.json"
            f.write_text(json.dumps({
                "bot_token": token,
                "base_url": st.get("baseurl") or "https://ilinkai.weixin.qq.com",
                "bot_user_id": st.get("bot_user_id") or st.get("ilink_user_id") or "",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(f, 0o600)
            log.info("官方 iLink 凭证已保存到 %s", f)
            return True
        except Exception:
            log.exception("保存官方凭证失败")
            return False

    def ilink_status(self) -> dict:
        """官方通道是否已有可用凭证（决定"已登录"徽标）。"""
        f = Path(self._ilink_state_dir()) / "account.json"
        if not f.exists():
            return {"loggedIn": False}
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {"loggedIn": False}
        return {"loggedIn": bool(d.get("bot_token")),
                "botUserId": d.get("bot_user_id") or ""}

    # ── 扫码登录（按通道分流）─────────────────────────────────

    async def login_qrcode(self) -> dict:
        """获取登录二维码。官方 iLink 与 GeWe 走完全不同的路径。"""
        a = self.cfg.adapter
        if a.wechat_type == "ilink":
            return await self.ilink_qrcode()
        if not a.gewe_base_url:
            return {"ok": False, "message": "请先填写网关地址（可点「自动探测」）"}
        if not a.gewe_token:
            return {
                "ok": False,
                "message": "请先填写 GeWe Token",
                "hint": "网关已就绪，但 Token 还没填。"
                         "请去 GeWe 控制台复制 Token 贴到上面的输入框里，"
                         "然后再点「获取二维码」。",
            }

        data, last_error = None, ""
        for ep in ("login/getLoginQrCode", "login/getQrCode", "login/getQr"):
            r = await self._gateway_call(
                ep, {"appId": a.gewe_app_id, "proxyIp": ""}, timeout=25.0,
            )
            if not r.get("ok"):
                last_error = r.get("error", "")
                if r.get("status", 0) == 0:
                    break
                continue
            body = r.get("data") or {}
            if body.get("ret") == 200:
                data = body.get("data") or {}
                break
            last_error = f"网关返回 ret={body.get('ret')} {body.get('msg') or ''}"

        if data is None:
            return {
                "ok": False,
                "message": last_error or "获取二维码失败",
                "hint": "可先点「测试连接」确认网关是否正常。",
            }

        self._login = {
            "status": "waiting",
            "qrcode": data.get("qrData", ""),
            "image": data.get("qrImgBase64") or data.get("qrData", ""),
            "uuid": data.get("uuid", ""),
            "app_id": data.get("appId") or a.gewe_app_id,
            "message": "请用微信扫码（二维码 150 秒内有效）",
        }
        return {"ok": True, **self._login}

    async def login_check(self, captcha: str = "") -> dict:
        """轮询登录状态。

        真实参数（已核对官方文档）：{appId, proxyIp, uuid, autoSliding}
        返回 data.status：1 = 已扫码待确认；成功时 data 里会带 wxid / nickName。
        若返回 data.url，表示需要人脸/滑块验证。
        """
        if self._login.get("channel") == "ilink":
            return await self.ilink_login_check()
        if not self._login.get("uuid"):
            return {"ok": False, "message": "请先获取二维码"}

        r = await self._gateway_call("login/checkLogin", {
            "appId": self._login["app_id"],
            "proxyIp": "",
            "uuid": self._login["uuid"],
            "autoSliding": False,
        }, timeout=25.0)

        if not r.get("ok"):
            self._login["status"] = "failed"
            self._login["message"] = r.get("error", "检查登录失败")
            return {"ok": False, "message": r.get("error", "检查登录失败")}

        data = (r.get("data") or {}).get("data") or {}

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
