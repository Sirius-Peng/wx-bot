"""配置持久化 —— 面向非技术用户，用可读的 JSON 文件。

设计原则：
  · 单文件 JSON，用户可直接备份/迁移/手动改
  · 敏感信息（API Key）落盘时权限收紧到 0600
  · 写入采用「临时文件 + 原子替换」，避免断电导致配置损坏
  · 所有字段都有默认值，缺字段不会崩
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

CONFIG_DIR = Path(os.environ.get("WXBOT_DATA_DIR", "./data"))
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class AdapterConfig:
    """接入通道配置。支持微信与 QQ 同时启用。"""

    # ── 向后兼容：旧版 type 字段 ──────────────────────────────
    # 旧版用 type 单选；新版用 wechat_enabled + qq_enabled 多选。
    # 读取时若检测到旧版 type，自动迁移。
    type: str = "gewechat"            # deprecated，仅用于迁移旧配置

    # ── 微信通道 ──────────────────────────────────────────────
    wechat_enabled: bool = True       # 是否启用微信
    wechat_type: str = "gewechat"     # gewechat | ilink | hybrid

    # GeWe / Gewechat
    gewe_base_url: str = "http://127.0.0.1:2531"
    gewe_token: str = ""
    gewe_app_id: str = ""
    bot_wxid: str = ""
    bot_nickname: str = ""

    # 官方 iLink
    ilink_state_dir: str = "./data/ilink"
    hybrid_enabled: bool = False      # 混合：私聊走官方、群聊走协议

    # ── QQ 通道 ───────────────────────────────────────────────
    qq_enabled: bool = False          # 是否启用 QQ
    onebot_http_url: str = "http://127.0.0.1:3000"
    onebot_token: str = ""
    bot_qq: str = ""
    # NapCat WebUI Token：一键部署时自动生成并写进容器，
    # 保存下来是为了重启后还能登录同一个容器的 WebUI 拿二维码
    napcat_webui_token: str = ""


@dataclass
class LLMConfig:
    """大模型配置。"""
    primary_base: str = "https://api.deepseek.com"
    primary_key: str = ""
    primary_model: str = "deepseek-chat"
    fallback_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    fallback_key: str = ""
    fallback_model: str = "qwen-plus"
    judge_base: str = "https://open.bigmodel.cn/api/paas/v4"
    judge_key: str = ""
    judge_model: str = "glm-4.7-flash"


@dataclass
class BotConfig:
    """机器人行为配置。"""
    name: str = "小助手"
    persona: str = "友好、简洁、乐于助人的群聊助手"
    # 群聊回复策略：
    #   mention_only —— 仅 @ 机器人时回复（默认）
    #   smart        —— 智能插话（启发式 + 小模型判定，约 1-2% 消息被回复）
    group_reply_mode: str = "mention_only"
    reply_prob: float = 0.7           # 概率闸门（仅智能模式生效）
    quiet_hours: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    global_daily_limit: int = 800
    group_reply_at_sender: bool = True  # 群聊回复时 @ 提问者

    # ── 人格知识库 ────────────────────────────────────────────
    memory_enabled: bool = True       # 是否随对话积累记忆
    memory_inject: bool = True        # 是否把记忆注入到回答上下文
    memory_extract_interval: int = 3  # 每多少轮对话抽取一次（省 token）


@dataclass
class GroupConfigItem:
    """单个群的配置。未显式设置时跟随全局策略。"""
    chat_id: str = ""
    name: str = ""
    enabled: bool = True
    mode: str = ""                    # 空 = 跟随全局策略；mention_only | smart | keyword_only
    wake_words: list[str] = field(default_factory=lambda: ["小助手"])
    cooldown_sec: int = 0             # 0 = 不限速
    hourly_limit: int = 60
    reply_prob: float = 1.0
    quiet_hours: list[int] = field(default_factory=list)   # 空 = 不设静默时段
    persona: str = ""


@dataclass
class AppConfig:
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    groups: dict[str, GroupConfigItem] = field(default_factory=dict)
    whitelist_users: list[str] = field(default_factory=list)
    blacklist_users: list[str] = field(default_factory=list)


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
                a["wechat_type"] = "gewechat"
            elif old_type == "ilink":
                a["wechat_enabled"] = True
                a["qq_enabled"] = False
                a["wechat_type"] = "ilink"
            else:
                a["wechat_enabled"] = True
                a["qq_enabled"] = False
                a["wechat_type"] = old_type or "gewechat"

        cfg.adapter = AdapterConfig(**{k: v for k, v in a.items()
                                       if k in AdapterConfig.__dataclass_fields__})

        l = raw.get("llm") or {}
        cfg.llm = LLMConfig(**{k: v for k, v in l.items()
                               if k in LLMConfig.__dataclass_fields__})

        b = raw.get("bot") or {}
        cfg.bot = BotConfig(**{k: v for k, v in b.items()
                               if k in BotConfig.__dataclass_fields__})

        for cid, g in (raw.get("groups") or {}).items():
            if not isinstance(g, dict):
                continue
            item = GroupConfigItem(**{k: v for k, v in g.items()
                                      if k in GroupConfigItem.__dataclass_fields__})
            item.chat_id = cid
            cfg.groups[cid] = item

        cfg.whitelist_users = list(raw.get("whitelist_users") or [])
        cfg.blacklist_users = list(raw.get("blacklist_users") or [])
        return cfg

    # ── 写 ────────────────────────────────────────────────────

    def save(self, cfg: AppConfig) -> None:
        """原子写入，避免中断导致配置损坏。"""
        data = {
            "adapter": asdict(cfg.adapter),
            "llm": asdict(cfg.llm),
            "bot": asdict(cfg.bot),
            "groups": {cid: asdict(g) for cid, g in cfg.groups.items()},
            "whitelist_users": cfg.whitelist_users,
            "blacklist_users": cfg.blacklist_users,
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
            "groups": {cid: asdict(g) for cid, g in cfg.groups.items()},
            "whitelist_users": cfg.whitelist_users,
            "blacklist_users": cfg.blacklist_users,
        }
        for k in ("primary_key", "fallback_key", "judge_key"):
            d["llm"][k] = _mask(d["llm"].get(k, ""))
        d["adapter"]["gewe_token"] = _mask(d["adapter"].get("gewe_token", ""))
        d["adapter"]["onebot_token"] = _mask(d["adapter"].get("onebot_token", ""))
        d["adapter"]["napcat_webui_token"] = _mask(
            d["adapter"].get("napcat_webui_token", "")
        )
        return d


def _mask(v: Optional[str]) -> str:
    if not v:
        return ""
    return "•" * 8 + v[-4:] if len(v) > 4 else "•" * len(v)
