"""配置持久化 —— 面向非技术用户，用可读的 JSON 文件。

设计原则：
  · 单文件 JSON，用户可直接备份/迁移/手动改
  · 敏感信息（API Key）落盘时权限收紧到 0600
  · 写入采用「临时文件 + 原子替换」，避免断电导致配置损坏
  · 所有字段都有默认值，缺字段不会崩
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .core.personality import PROFILE_IDS, RELATIONSHIP_IDS
from .personas.phrolova import OVERVIEW

from .core.storage import resolve_data_dir

CONFIG_DIR = resolve_data_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class AdapterConfig:
    """接入通道配置。微信与 QQ 可同时启用。"""

    # ── 向后兼容：旧版 type 字段 ──────────────────────────────
    # 旧版用 type 单选；新版用 wechat_enabled + qq_enabled 多选。
    # 读取时若检测到旧版 type，自动迁移。
    type: str = ""                    # deprecated，仅用于迁移旧配置

    # ── 微信通道 ──────────────────────────────────────────────
    # 只保留腾讯 iLink（本项目仅私聊）。个人微信的群聊能力已整体下线：
    # 非官方协议端维护状态不可控，与其留一个随时会坏的通道，不如只保留官方这一条。
    wechat_enabled: bool = True
    bot_nickname: str = ""            # QQ 群 @ 用的昵称（同时作为机器人名的兜底）

    # 官方 iLink Bot API
    ilink_state_dir: str = "./data/ilink"

    # 旧配置里的 wechat_type 已无意义，保留字段只为读旧文件时不报错
    wechat_type: str = "ilink"

    # ── QQ 通道 ───────────────────────────────────────────────
    qq_enabled: bool = False          # 是否启用 QQ
    onebot_http_url: str = "http://127.0.0.1:3000"
    onebot_token: str = ""
    bot_qq: str = ""
    # NapCat WebUI Token：一键部署时自动生成并写进容器，
    # 保存下来是为了重启后还能登录同一个容器的 WebUI 拿二维码
    napcat_webui_token: str = ""
    napcat_image_mirror: str = "docker.m.daocloud.io"


@dataclass
class LLMConfig:
    """大模型配置。"""
    primary_base: str = "https://api.deepseek.com"
    primary_key: str = ""
    primary_model: str = "deepseek-flash"
    fallback_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    fallback_key: str = ""
    fallback_model: str = "qwen-plus"
    judge_base: str = "https://open.bigmodel.cn/api/paas/v4"
    judge_key: str = ""
    judge_model: str = "glm-4.7-flash"
    vision_base: str = ""
    vision_key: str = ""
    vision_model: str = ""  # empty: primary must support images
    search_enabled: bool = True
    search_key: str = ""  # blank: reuse primary only when it is official DeepSeek
    search_model: str = "deepseek-flash"
    context_timezone: str = "Asia/Shanghai"


@dataclass
class BotConfig:
    """机器人行为配置。

    这些字段**必须真正生效**：`name` / `persona` 会拼进系统提示词
    （见 `core/context.py::build_system_prompt`），
    `memory_*` 控制人格知识库。改了界面上的值就必须能看到行为变化。
    """
    name: str = "弗洛洛"
    persona: str = OVERVIEW
    persona_profile: str = "phrolova"
    persona_relationship: str = "neutral"
    # 群聊里回复时是否 @ 提问者（QQ 群用）
    group_reply_at_sender: bool = True

    # ── 人格知识库 ────────────────────────────────────────────
    humanized_wait: bool = True
    typing_indicator: bool = True
    message_pause_timeout: float = 120.0
    message_merge_window: float = 2.0
    message_merge_max_wait: float = 10.0

    # Shared across QQ/WeChat; private contacts are explicitly selected in the UI.
    proactive_enabled: bool = False
    proactive_min_hours: float = 3.0
    proactive_max_hours: float = 8.0
    proactive_daily_limit: int = 2
    proactive_quiet_start: int = 23
    proactive_quiet_end: int = 8

    memory_enabled: bool = True       # 是否随对话积累记忆
    memory_inject: bool = True        # 是否把记忆注入到回答上下文
    memory_extract_interval: int = 3  # 每多少轮对话抽取一次（省 token）


@dataclass
class AppConfig:
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    bot: BotConfig = field(default_factory=BotConfig)


class ConfigStore:
    """配置读写。"""

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── 读 ────────────────────────────────────────────────────

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 配置损坏时回退到默认值，而不是让程序起不来
            return AppConfig()
        return self._from_dict(raw)

    def _from_dict(self, raw: dict[str, Any]) -> AppConfig:
        cfg = AppConfig()

        a = raw.get("adapter") or {}
        # ── 旧版配置迁移 ──────────────────────────────────────
        # 旧版用 type 单选（gewechat/ilink/onebot）；新版用 wechat_enabled + qq_enabled
        old_type = a.get("type", "")
        if old_type and "wechat_enabled" not in a and "qq_enabled" not in a:
            if old_type == "onebot":
                a["wechat_enabled"] = False
                a["qq_enabled"] = True
            else:
                a["wechat_enabled"] = True
                a["qq_enabled"] = False
            a["wechat_type"] = "ilink"

        cfg.adapter = AdapterConfig(**{k: v for k, v in a.items()
                                       if k in AdapterConfig.__dataclass_fields__})

        # ⚠️ 个人微信的群聊通道（GeWe / WeChatPadPro 等非官方协议端）已整体下线，
        # 旧配置一律归到腾讯官方 iLink —— 与其留一个随时会坏的通道，
        # 不如只保留iLink这一条（本项目仅私聊）。
        if cfg.adapter.wechat_type != "ilink":
            cfg.adapter.wechat_type = "ilink"

        l = raw.get("llm") or {}
        cfg.llm = LLMConfig(**{k: v for k, v in l.items()
                               if k in LLMConfig.__dataclass_fields__})

        from .core.reality import valid_timezone
        cfg.llm.context_timezone = valid_timezone(cfg.llm.context_timezone)
        cfg.llm.search_enabled = cfg.llm.search_enabled is True
        if not isinstance(cfg.llm.search_model, str) or not cfg.llm.search_model.strip():
            cfg.llm.search_model = "deepseek-flash"
        if not isinstance(cfg.llm.search_key, str): cfg.llm.search_key = ""

        b = dict(raw.get("bot") or {})
        # 旧的明确自定义人设保持自定义；不擅自覆写其他用户的角色卡。
        if "persona_profile" not in b and ("name" in b or "persona" in b):
            b["persona_profile"] = "custom"
        if not isinstance(b.get("persona_profile", "phrolova"), str) or b.get("persona_profile", "phrolova") not in PROFILE_IDS:
            b["persona_profile"] = "custom"
        if not isinstance(b.get("persona_relationship", "neutral"), str) or b.get("persona_relationship", "neutral") not in RELATIONSHIP_IDS:
            b["persona_relationship"] = "neutral"
        for key,default,lo,hi in [('message_merge_window',2.0,0.0,10.0),('message_merge_max_wait',10.0,0.1,60.0),('message_pause_timeout',120.0,5.0,600.0)]:
            try:value=float(b.get(key,default));value=value if math.isfinite(value) else default
            except (TypeError,ValueError):value=default
            b[key]=min(hi,max(lo,value))
        b['message_merge_max_wait']=max(b['message_merge_window'],b['message_merge_max_wait'])
        b['proactive_enabled'] = b.get('proactive_enabled') is True
        for key, default, lo, hi in [('proactive_min_hours', 3, .25, 168), ('proactive_max_hours', 8, .25, 168),
                                     ('proactive_daily_limit', 2, 1, 10), ('proactive_quiet_start', 23, 0, 23), ('proactive_quiet_end', 8, 0, 23)]:
            try:
                value = float(b.get(key, default))
                if isinstance(b.get(key), bool) or not math.isfinite(value): value = default
            except (TypeError, ValueError): value = default
            value = min(hi, max(lo, value))
            b[key] = int(value) if key in ('proactive_daily_limit', 'proactive_quiet_start', 'proactive_quiet_end') else value
        b['proactive_max_hours'] = max(b['proactive_min_hours'], b['proactive_max_hours'])
        cfg.bot = BotConfig(**{k: v for k, v in b.items()
                               if k in BotConfig.__dataclass_fields__})

        return cfg

    # ── 写 ────────────────────────────────────────────────────

    def save(self, cfg: AppConfig) -> None:
        """原子写入，避免中断导致配置损坏。"""
        data = {
            "adapter": asdict(cfg.adapter),
            "llm": asdict(cfg.llm),
            "bot": asdict(cfg.bot),
        }
        text = json.dumps(data, ensure_ascii=False, indent=2)

        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o600)      # 含 API Key，权限收紧
            os.replace(tmp, self.path)   # 原子替换
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ── 对外展示（脱敏）──────────────────────────────────────

    def masked(self, cfg: AppConfig) -> dict:
        """返回给前端的配置：密钥只显示后四位。"""
        d = {
            "adapter": asdict(cfg.adapter),
            "llm": asdict(cfg.llm),
            "bot": asdict(cfg.bot),
        }
        for k in ("primary_key", "fallback_key", "judge_key", "vision_key", "search_key"):
            d["llm"][k] = _mask(d["llm"].get(k, ""))
        d["adapter"]["onebot_token"] = _mask(d["adapter"].get("onebot_token", ""))
        d["adapter"]["napcat_webui_token"] = _mask(
            d["adapter"].get("napcat_webui_token", "")
        )
        return d


def _mask(v: Optional[str]) -> str:
    if not v:
        return ""
    return "•" * 8 + v[-4:] if len(v) > 4 else "•" * len(v)
