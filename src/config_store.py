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
import copy
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
    # NapCat WebUI Token：本机内置网关首次启动时生成，更新保持原值。
    napcat_webui_token: str = ""
    napcat_image_mirror: str = "docker.m.daocloud.io"  # legacy, unused by native gateway
    napcat_mode: str = "native"  # native (bundled Windows x64) | external OneBot


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
    search_backend: str = "tools"  # tools: direct Search/Fetch; native: legacy model search
    search_model: str = "deepseek-flash"  # used only by native; keep older settings intact
    context_timezone: str = "Asia/Shanghai"
    context_window_tokens: int = 0   # 0: resolve known provider/model, otherwise 32K.
    context_compact_ratio: float = 0.4


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
    reply_mode: str = "natural"      # natural: 克制接话；always: 每轮必回
    chat_mode: str = "fast"          # fast: direct response; deep: explicit reasoning
    conversation_policy_version: int = 3
    late_reply_enabled: bool = True   # 普通分享可在1～3分钟内重判一次
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
class CompanionConfig:
    """Optional desktop surface; model assets remain outside the installation."""
    enabled: bool = False
    model_id: str = ""
    owner_id: str = "local"
    always_on_top: bool = True
    click_through: bool = False
    window_bounds: dict = field(default_factory=dict)


@dataclass
class VoiceConfig:
    """Optional WeChat TTS; independent from language-model and persona settings."""
    enabled: bool = False
    provider: str = "fish"
    base_url: str = "https://api.fish.audio"
    api_key: str = ""
    model_id: str = "5b432f747d00419aba7cb12fa50e4af0"
    fish_model: str = "s2.1-pro"
    reference_audio: str = ""
    reference_text: str = ""
    prompt_language: str = "zh"
    text_language: str = "zh"
    reply_rate: float = .25
    cooldown_seconds: float = 300
    max_chars: int = 160
    timeout_seconds: float = 20


@dataclass
class AppConfig:
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    companion: CompanionConfig = field(default_factory=CompanionConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)


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
            raise ValueError('配置文件需要恢复，原文件已保留；请从备份恢复后启动')
        return self._from_dict(raw)

    def _from_dict(self, raw: dict[str, Any]) -> AppConfig:
        cfg = AppConfig()
        raw = raw if isinstance(raw, dict) else {}
        # Unknown future/plugin keys survive a load/edit/save round trip without
        # making them executable options or exposing possible secrets in masked().
        cfg._unknown = copy.deepcopy(raw)

        a = dict(raw.get("adapter") or {}) if isinstance(raw.get('adapter'),dict) else {}
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

        # Keep pre-existing gateway installations external until explicitly selected.
        if 'napcat_mode' not in a and (a.get('qq_enabled') is True or a.get('napcat_webui_token') or a.get('onebot_token') or a.get('bot_qq') or
                (a.get('onebot_http_url') and a['onebot_http_url']!='http://127.0.0.1:3000')):
            a['napcat_mode']='external'
        if a.get('napcat_mode','native') not in ('native','external'): a['napcat_mode']='external'
        cfg.adapter = AdapterConfig(**{k: v for k, v in a.items()
                                       if k in AdapterConfig.__dataclass_fields__})

        # ⚠️ 个人微信的群聊通道（GeWe / WeChatPadPro 等非官方协议端）已整体下线，
        # 旧配置一律归到腾讯官方 iLink —— 与其留一个随时会坏的通道，
        # 不如只保留iLink这一条（本项目仅私聊）。
        if cfg.adapter.wechat_type != "ilink":
            cfg.adapter.wechat_type = "ilink"

        l = raw.get("llm") if isinstance(raw.get('llm'),dict) else {}
        l = l or {}
        cfg.llm = LLMConfig(**{k: v for k, v in l.items()
                               if k in LLMConfig.__dataclass_fields__})

        from .core.reality import valid_timezone
        cfg.llm.context_timezone = valid_timezone(cfg.llm.context_timezone)
        cfg.llm.search_enabled = cfg.llm.search_enabled is True
        if not isinstance(cfg.llm.search_backend, str) or cfg.llm.search_backend not in ("tools", "native"):
            cfg.llm.search_backend = "tools"
        if not isinstance(cfg.llm.search_model, str) or not cfg.llm.search_model.strip():
            cfg.llm.search_model = "deepseek-flash"
        if not isinstance(cfg.llm.search_key, str): cfg.llm.search_key = ""
        window = cfg.llm.context_window_tokens
        if isinstance(window, bool) or not isinstance(window, int) or window < 0:
            cfg.llm.context_window_tokens = 0
        ratio = cfg.llm.context_compact_ratio
        if (isinstance(ratio, bool) or not isinstance(ratio, (int, float))
                or not .1 <= ratio <= .8):
            cfg.llm.context_compact_ratio = .4
        else:
            cfg.llm.context_compact_ratio = float(ratio)

        b = dict(raw.get("bot") or {}) if isinstance(raw.get('bot'),dict) else {}
        # 旧的明确自定义人设保持自定义；不擅自覆写其他用户的角色卡。
        if "persona_profile" not in b and ("name" in b or "persona" in b):
            b["persona_profile"] = "custom"
        if not isinstance(b.get("persona_profile", "phrolova"), str) or b.get("persona_profile", "phrolova") not in PROFILE_IDS:
            b["persona_profile"] = "custom"
        if not isinstance(b.get("persona_relationship", "neutral"), str) or b.get("persona_relationship", "neutral") not in RELATIONSHIP_IDS:
            b["persona_relationship"] = "neutral"
        # v2's default was four seconds; non-default custom windows stay intact.
        # Once marked v3, choosing four seconds is an explicit user preference.
        if 'conversation_policy_version' not in b and b.get('message_merge_window') == 4:
            b['message_merge_window'] = 2.0
        b['conversation_policy_version'] = 3
        b['chat_mode'] = b.get('chat_mode') if b.get('chat_mode') in ('fast','deep') else 'fast'
        for key,default,lo,hi in [('message_merge_window',2.0,0.0,10.0),('message_merge_max_wait',10.0,0.1,60.0),('message_pause_timeout',120.0,5.0,600.0)]:
            try:value=float(b.get(key,default));value=value if math.isfinite(value) else default
            except (TypeError,ValueError):value=default
            b[key]=min(hi,max(lo,value))
        b['reply_mode'] = b.get('reply_mode', 'natural') if b.get('reply_mode', 'natural') in ('natural', 'always') else 'natural'
        b['late_reply_enabled'] = b.get('late_reply_enabled', True) is True
        b['memory_extract_interval'] = max(3, min(50, int(b.get('memory_extract_interval', 3)))) if isinstance(b.get('memory_extract_interval', 3), (int, float)) and not isinstance(b.get('memory_extract_interval', 3), bool) and math.isfinite(b.get('memory_extract_interval', 3)) else 3
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

        c = raw.get('companion') if isinstance(raw.get('companion'),dict) else {}
        c = dict(c or {})
        for key,default in [('enabled',False),('always_on_top',True),('click_through',False)]:
            c[key] = c.get(key,default) is True
        for key,default in [('model_id',''),('owner_id','local')]:
            c[key] = c.get(key,default) if isinstance(c.get(key,default),str) else default
        if not isinstance(c.get('window_bounds'),dict):c['window_bounds']={}
        cfg.companion = CompanionConfig(**{k:v for k,v in c.items() if k in CompanionConfig.__dataclass_fields__})

        v = dict(raw.get('voice') or {}) if isinstance(raw.get('voice'),dict) else {}
        defaults = VoiceConfig()
        v['enabled'] = v.get('enabled',False) is True
        provider=v.get('provider',defaults.provider)
        if isinstance(provider,str) and provider in ('gpt_sovits','fish'):
            v['provider']=provider
        else:
            # A typo in an explicitly stored service is not consent to switch
            # to the default billable provider. Keep the invalid choice visible.
            v['provider']=provider if isinstance(provider,str) and len(provider)<=256 else ''
            v['enabled']=False
        for key,limit in [('base_url',2048),('api_key',4096),('model_id',256),('reference_audio',4096),('reference_text',4000)]:
            value=v.get(key,getattr(defaults,key))
            v[key]=value.strip() if isinstance(value,str) and len(value)<=limit else getattr(defaults,key)
        engines={'s1','s2-pro','s2.1-pro','s2.1-pro-free','drama-3'}
        engine=v.get('fish_model',defaults.fish_model)
        if isinstance(engine,str) and engine in engines:
            v['fish_model']=engine
        else:
            # Especially do not turn a misspelled free engine into paid Pro.
            # Missing fields retain defaults, but explicit invalid fields fail closed.
            v['fish_model']=engine if isinstance(engine,str) and len(engine)<=256 else ''
            v['enabled']=False
        languages={'zh','en','ja','ko','yue','auto','all_zh','all_ja','all_ko','all_yue'}
        for key in ('prompt_language','text_language'):
            value=v.get(key,'zh');v[key]=value if isinstance(value,str) and value in languages else 'zh'
        for key,lo,hi in [('reply_rate',0,1),('cooldown_seconds',0,3600),('max_chars',20,500),('timeout_seconds',3,60)]:
            value=v.get(key,getattr(defaults,key))
            if (isinstance(value,bool) or not isinstance(value,(int,float)) or not lo<=value<=hi
                    or not math.isfinite(value) or (key=='max_chars' and not isinstance(value,int))):
                value=getattr(defaults,key)
            v[key]=int(value) if key=='max_chars' else float(value)
        cfg.voice = VoiceConfig(**{k:value for k,value in v.items() if k in VoiceConfig.__dataclass_fields__})

        return cfg

    # ── 写 ────────────────────────────────────────────────────

    def save(self, cfg: AppConfig) -> None:
        """原子写入，避免中断导致配置损坏。"""
        # Also retain unknown keys when an older API client reconstituted cfg
        # through dataclasses.asdict(), which intentionally omits _unknown.
        try:previous=json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError,ValueError):previous={}
        data = copy.deepcopy(previous) if isinstance(previous,dict) else {}
        for key,value in getattr(cfg,'_unknown',{}).items():
            if key not in AppConfig.__dataclass_fields__:data[key]=copy.deepcopy(value)
        for section in ('adapter','llm','bot','companion','voice'):
            values=data.get(section) if isinstance(data.get(section),dict) else {}
            original=getattr(cfg,'_unknown',{}).get(section,{})
            if isinstance(original,dict):values.update(copy.deepcopy(original))
            values.update(asdict(getattr(cfg,section)))
            data[section]=values
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
            "companion": asdict(cfg.companion),
            "voice": asdict(cfg.voice),
        }
        for k in ("primary_key", "fallback_key", "judge_key", "vision_key", "search_key"):
            d["llm"][k] = _mask(d["llm"].get(k, ""))
        d["voice"]["api_key"] = _mask(d["voice"].get("api_key", ""))
        d["adapter"]["onebot_token"] = _mask(d["adapter"].get("onebot_token", ""))
        d["adapter"]["napcat_webui_token"] = _mask(
            d["adapter"].get("napcat_webui_token", "")
        )
        return d


def _mask(v: Optional[str]) -> str:
    if not v:
        return ""
    return "•" * 8 + v[-4:] if len(v) > 4 else "•" * len(v)
