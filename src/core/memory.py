"""人格知识库 —— 让机器人随着对话不断了解用户。

═══════════════════════════════════════════════════════════════════
四个环节构成闭环：

  ① 抽取  每轮对话后，用便宜模型从对话里挑出"值得长期记住"的信息
  ② 融合  新信息与已有记忆冲突/重复时**更新**而非追加，避免越攒越乱
  ③ 检索  回答前按相关度挑出该用的记忆（关键词 + 置信度 + 时效）
  ④ 注入  把挑出的记忆作为系统提示喂给模型

与普通"对话历史"的区别：
  历史是**短期**的（几十条就丢），知识库是**长期**的（跨会话留存），
  并且会**演化** —— 用户换了工作，旧版本保留作历史，回答当前情况时只取新版本。

存储用本地 JSON 文件，不需要额外数据库；自动抽取使用配置的模型服务。
记忆量在单用户几百条量级，简单打分检索足够；真到几千条再考虑向量检索。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import copy
import shutil
import json
import os
import tempfile
import logging
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 记忆类别
CATEGORIES = {
    "fact": "事实",         # 身份、职业、城市、生日、宠物名…
    "preference": "偏好",   # 喜欢/讨厌什么、习惯
    "relation": "关系",     # 与其他人/群/组织的关系
    "event": "事件",        # 待办、约定、计划
    "style": "风格",        # 说话风格、称呼习惯
}

# 作用域
SCOPE_USER = "user"     # 跟着人走，跨群共享
SCOPE_GROUP = "group"   # 只在某个群生效

# owner_id 取这些值时，表示"对所有人都生效"（界面上手动添加、留空对象时用）
# ⚠️ 没有这一步，"手动添加"的记忆永远不会被召回 —— 用户会认为知识库坏了
GLOBAL_OWNERS = {"", "global", "*", "all"}

# 检索参数
MAX_INJECT = 6          # 每次最多注入几条记忆
MAX_ITEMS = 3000        # 总量上限，超出后淘汰低分项


@dataclass
class MemoryItem:
    id: str
    scope: str
    owner_id: str
    category: str
    content: str
    keywords: list[str] = field(default_factory=list)
    confidence: float = 0.7      # 置信度 0~1
    hits: int = 0                # 被引用次数（越常用越重要）
    created_at: float = 0.0
    updated_at: float = 0.0
    source_chat: str = ""        # 来源会话，便于溯源

    platform: str = ""
    fact_key: str = ""
    status: str = "active"
    superseded_by: str = ""
    valid_from: float = 0.0
    valid_until: float = 0.0
    expires_at: float = 0.0
    importance: float = 1.0
    evidence: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stateLabel"] = "历史版本" if self.status != "active" else ("已过期" if self.expires_at and self.expires_at < time.time() else "当前")
        d["categoryLabel"] = CATEGORIES.get(self.category, self.category)
        d["createdAt"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created_at))
        d["updatedAt"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated_at))
        return d


EXTRACT_PROMPT = """从下面这段对话中，抽取值得**长期记住**的信息。

【只抽取】稳定的、跨会话仍然有用的信息：
- 事实：身份、职业、所在城市、生日、宠物名、项目名、常用工具
- 偏好：喜欢什么、讨厌什么、习惯、口头禅
- 关系：与某人的关系、在某个群里的角色
- 事件：明确的约定、待办、计划（带时间）
- 风格：说话方式、希望被怎么称呼

【不要抽取】
- 一次性的提问或请求（"帮我查下天气"）
- 寒暄、情绪化表达（"哈哈哈"、"谢谢"）
- 很快就会过时的临时信息
- 助手的角色设定、示例、台词和推测（只记用户亲自明确表达的信息）
- 假设、引用、角色扮演、用户描述的虚构人物；例如“我是漂泊者”不等于现实身份
- 用户明确说不要记忆的内容；昵称/账号ID不作为未确认身份

【输出格式】严格输出 JSON 数组，不要任何其他内容：
[{{"category":"fact","content":"一句话描述","keywords":["关键词"]}}]
category 必须是 fact、preference、relation、event、style 之一。
还可以提供 fact_key（具体事实槽，例如 occupation、city、preferred_name、preference:coffee；不同偏好不要共享笼统的key）、evidence（逐字摘取用户的依据）、importance（1~3）、expires_at（仅明确时间的事件，Unix秒）。
更新职业/城市/称呼时使用同一fact_key；不把相似关键词的不同事实合成同一条。不把助手说过的话当成用户证据。

没有值得记的就输出 []

【对话】
{conversation}
"""


class MemoryExtractionError(RuntimeError):
    """Malformed/empty model output is a failure, not a successful empty extraction."""
    def __init__(self, reason: str):
        self.safe_reason = reason
        super().__init__(reason)


class PersonaMemory:
    """人格知识库。线程内使用，写盘为原子操作。"""

    supports_platform_scope = True

    def __init__(self, path: Path, llm=None, enabled: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.llm = llm                  # 用于抽取记忆（走便宜模型）
        self.enabled = enabled
        self.inject = True
        self._generation=0
        self._revisions = {}
        self._items: dict[str, MemoryItem] = {}
        self.identities: list[dict] = []
        self._load()
        from .memory_learning import MemoryLearningQueue
        self.learner = MemoryLearningQueue(self, self.path.with_name("memory-learning.json"))

    # ══ 持久化 ═══════════════════════════════════════════════

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get('version',1)==1:
                backup=self.path.with_name(self.path.stem+'.v1-backup.json')
                if not backup.exists():shutil.copy2(self.path,backup);os.chmod(backup,0o600)
            self.identities = raw.get("identities", [])
            loaded = {}
            for d in raw.get("items", []):
                item = MemoryItem(**{k: v for k, v in d.items()
                                     if k in MemoryItem.__dataclass_fields__})
                loaded[item.id] = item
            self._items = loaded
            log.info("知识库已加载 %d 条记忆", len(self._items))
        except Exception:
            log.error("知识库加载失败，原文件保留，等待恢复", exc_info=True)
            raise ValueError("persistent memory requires recovery")

    def _save(self) -> None:
        data = {"version": 2, "identities": self.identities, "items": [asdict(i) for i in self._items.values()]}
        fd, temp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=1)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.path)
        finally:
            Path(temp).unlink(missing_ok=True)

    def identity_key(self, platform: str, owner_id: str) -> str:
        for identity in self.identities:
            if {'platform': platform, 'owner_id': owner_id} in identity['accounts']:
                return 'person:' + identity['id']
        return platform + ':' + owner_id

    def link_accounts(self, wechat_id: str, qq_id: str, label: str = '') -> dict:
        accounts = [{'platform': 'ilink', 'owner_id': wechat_id.strip()},
                    {'platform': 'onebot', 'owner_id': qq_id.strip()}]
        if any(not a['owner_id'] or len(a['owner_id']) > 200 or a['owner_id'] in GLOBAL_OWNERS for a in accounts):
            raise ValueError('请填写消息发送者的微信 ID 和 QQ 号')
        before_links = copy.deepcopy(self.identities)
        before_items = copy.deepcopy(self._items)
        matching = [i for i in self.identities if any(a in i['accounts'] for a in accounts)]
        for identity in matching:
            accounts += [a for a in identity['accounts'] if a not in accounts]
        identity = {'id': matching[0]['id'] if matching else uuid.uuid4().hex[:12],
                    'label': label.strip()[:80], 'accounts': accounts}
        self.identities = [i for i in self.identities if i not in matching] + [identity]
        try:
            self._reconcile_slots()
            self._save()
        except Exception:
            self.identities, self._items = before_links, before_items
            raise
        return identity

    def unlink_accounts(self, identity_id: str) -> bool:
        before_links = copy.deepcopy(self.identities)
        before_items = copy.deepcopy(self._items)
        self.identities = [i for i in self.identities if i['id'] != identity_id]
        if self.identities == before_links:
            return False
        try:
            self._reconcile_slots()
            self._save()
        except Exception:
            self.identities, self._items = before_links, before_items
            raise
        return True

    def _reconcile_slots(self) -> None:
        slots = {}
        for item in self._items.values():
            slot = item.fact_key or self._fact_slot(item.category, item.content)
            if slot and item.scope == SCOPE_USER:
                key = (self.identity_key(item.platform, item.owner_id), item.category, slot)
                slots.setdefault(key, []).append(item)
        for versions in slots.values():
            versions.sort(key=lambda i: (i.valid_from or i.created_at, i.updated_at, i.id))
            for index, item in enumerate(versions):
                successor = versions[index + 1] if index + 1 < len(versions) else None
                item.status = 'superseded' if successor else 'active'
                item.superseded_by = successor.id if successor else ''
                item.valid_until = (successor.valid_from or successor.created_at) if successor else 0

    # ══ ① 抽取 ═══════════════════════════════════════════════

    async def observe(self, chat_id: str, chat_type: str, user_id: str,
                      user_name: str, history: list[dict], platform: str = "") -> list[MemoryItem]:
        """从一段对话中抽取记忆并融合进知识库。

        @param history 最近几轮问答，形如 [{"user": "...", "assistant": "..."}]
        """
        if not self.enabled:
            raise MemoryExtractionError("automatic accumulation paused")
        if not self.llm:
            raise MemoryExtractionError("extraction provider not configured")
        if not history:
            return []
        generation = self._generation
        identity = self.identity_key(platform, user_id)
        revision = self._revisions.get(identity, 0)
        observed_at = max((t.get('observed_at', 0) for t in history[-6:] if isinstance(t.get('observed_at'), (int, float))), default=0) or time.time()
        convo = "当前时间：" + time.strftime("%Y-%m-%d %H:%M %Z") + "\n" + "\n".join(f"用户({user_name}): {t.get('user', '')}" for t in history[-6:])
        raw = await self.llm.complete(EXTRACT_PROMPT.format(conversation=convo), max_tokens=1200)
        if generation != self._generation or revision != self._revisions.get(identity, 0):return []
        extracted = self._parse_json_array(raw, strict=True)
        if not self.enabled:
            raise MemoryExtractionError("automatic accumulation paused")
        user_text = "\n".join(t.get("user", "") for t in history[-6:])
        valid = []
        for entry in extracted:
            content = entry.get("content")
            keywords = entry.get("keywords", [])
            if (isinstance(content, str) and 0 < len(content.strip()) <= 200
                    and isinstance(entry.get("category"), str) and entry["category"] in CATEGORIES and isinstance(keywords, list)
                    and all(isinstance(k, str) for k in keywords)):
                evidence = entry.get("evidence", "")
                if evidence and (not isinstance(evidence, str) or evidence not in user_text):
                    raise MemoryExtractionError("unsupported memory evidence")
                valid.append(entry)
        if extracted and not valid:
            raise MemoryExtractionError("invalid memory schema")
        before = copy.deepcopy(self._items)
        added = []
        try:
            for entry in valid:
                added.append(self._merge_or_create(
                    scope=SCOPE_USER, owner_id=user_id, category=entry["category"],
                    content=entry["content"].strip(), keywords=entry.get("keywords", []),
                    source_chat=chat_id,platform=platform,
                    fact_key=str(entry.get('fact_key') or '')[:80],
                    evidence=(str(entry.get('evidence') or '')[:200] if str(entry.get('evidence') or '') in user_text else ''),
                    importance=entry.get('importance',1.0),expires_at=entry.get('expires_at',0), observed_at=observed_at))
            if added:
                self._trim()
                self._save()
                log.info("知识库更新：新增/合并 %d 条", len(added))
        except Exception:
            self._items = before
            raise
        return added

    @staticmethod
    def _parse_json_array(raw: str, strict: bool = False) -> list[dict]:
        if not raw or not raw.strip():
            if strict:
                raise MemoryExtractionError("empty model response")
            return []
        s = re.sub(r"^```[a-zA-Z]*\s*", "", raw.strip())
        s = re.sub(r"\s*```$", "", s)
        i, j = s.find("["), s.rfind("]")
        try:
            if i < 0 or j <= i:
                raise ValueError("missing array")
            arr = json.loads(s[i:j + 1])
            if not isinstance(arr, list) or not all(isinstance(x, dict) for x in arr):
                raise ValueError("invalid array elements")
            return arr
        except (ValueError, TypeError) as exc:
            if strict:
                raise MemoryExtractionError("invalid JSON array") from exc
            return []

    # ══ ② 融合 ═══════════════════════════════════════════════

    @staticmethod
    def _fact_slot(category,content,key=''):
        key=key.strip().lower()
        key={'profession':'occupation','job':'occupation','location':'city','name':'preferred_name'}.get(key,key)
        if key and key not in ('fact','preference','relation','event','style','unknown') and re.fullmatch(r'[a-z0-9_:\-]{1,80}',key):return key
        if category=='fact' and re.search(r'(?:用户|我)(?:目前|现在|已)?(?:是|从事|转行做|改行做).{0,12}(?:工程师|程序员|医生|教师|老师|设计师)|职业(?:是|为)',content):return 'occupation'
        if category=='fact' and re.search(r'(?:用户|我)(?:目前|现在)?(?:居住|住在|搬到)|所在城市(?:是|为)',content):return 'city'
        if category in ('fact','style') and re.search(r'叫我|称呼我|我叫|昵称',content):return 'preferred_name'
        return ''

    def _merge_or_create(self,scope,owner_id,category,content,keywords,source_chat,
                         platform='',fact_key='',evidence='',importance=1.0,expires_at=0,observed_at=0):
        now=time.time();observed_at=observed_at or now;slot=self._fact_slot(category,content,fact_key)
        candidates=[i for i in self._items.values() if i.status=='active' and i.scope==scope
                    and i.category == category and ((scope == SCOPE_USER and
                    self.identity_key(i.platform, i.owner_id) == self.identity_key(platform, owner_id))
                    or (scope != SCOPE_USER and i.owner_id == owner_id and i.platform == platform))]
        same=next((i for i in candidates if i.platform == platform and i.owner_id == owner_id and i.content.strip()==content.strip()),None)
        if same:
            same.confidence=min(1.0,same.confidence+.15);same.updated_at=now
            same.keywords=list(dict.fromkeys(same.keywords+keywords))[:12]
            if evidence:same.evidence=evidence
            if slot:same.fact_key=slot
            return same
        try:importance=float(importance);importance=importance if math.isfinite(importance) else 1
        except (TypeError,ValueError):importance=1
        try:expires_at=float(expires_at);expires_at=expires_at if math.isfinite(expires_at) and expires_at>=0 else 0
        except (TypeError,ValueError):expires_at=0
        if category=='event' and not expires_at:expires_at=observed_at+30*86400
        item=MemoryItem(id=uuid.uuid4().hex[:12],scope=scope,owner_id=owner_id,category=category,
                        content=content,keywords=keywords[:12],confidence=.7,created_at=now,updated_at=now,
                        source_chat=source_chat,platform=platform,fact_key=slot,evidence=evidence,
                        importance=min(3,max(.5,importance)),expires_at=expires_at,valid_from=observed_at)
        self._items[item.id]=item
        if slot and scope == SCOPE_USER:
            self._reconcile_slots()
        return item

    # ══ ③ 检索 ═══════════════════════════════════════════════

    def recall(self, user_id: str, chat_id: str, query: str,
               limit: int = MAX_INJECT, platform: str = "") -> list[MemoryItem]:
        """按相关度挑出该用的记忆。

        打分 = 关键词命中 + 置信度 + 使用频率 + 时效性
        """
        if not self._items:
            return []

        q = (query or "").lower()
        q_chars = set(q)
        now = time.time()
        historical=bool(re.search(r'以前|曾经|过去|当时|之前|原来|去年|上次',q))
        aliases={'occupation':['工作','职业','工程师','上班','转行'],'city':['住哪','城市','搬家','住在'],
                 'preferred_name':['称呼','名字','叫我','姓名']}
        scored: list[tuple[float, MemoryItem]] = []

        for it in self._items.values():
            linked = (it.scope == SCOPE_USER and platform and it.platform and
                      self.identity_key(it.platform, it.owner_id) == self.identity_key(platform, user_id))
            if platform and it.platform and it.platform != platform and not linked:
                continue
            # Legacy automatic facts have no trusted channel namespace; preserve them
            # in management, but do not silently attach them to a same-numbered account.
            if platform and not it.platform and it.source_chat != 'manual':
                continue
            if not historical and (it.status!='active' or (it.expires_at and it.expires_at<=now)):continue
            # 取该用户本人的、全局通用的、以及该群作用域的记忆
            if not (
                (it.scope == SCOPE_USER and it.owner_id in GLOBAL_OWNERS) or
                (it.scope == SCOPE_USER and (linked or (it.owner_id == user_id and (not platform or not it.platform or it.platform == platform)))) or
                (it.scope == SCOPE_GROUP and it.owner_id == chat_id)
            ):
                continue

            score = 0.0
            # 关键词命中（最重要）
            for kw in it.keywords:
                if kw and kw.lower() in q:
                    score += 3.0
            slot=it.fact_key or self._fact_slot(it.category,it.content)
            if any(alias in q for alias in aliases.get(slot,[])):score+=3
            pairs=lambda t:{t[i:i+2] for i in range(len(t)-1) if t[i:i+2].strip()}
            qb=pairs(q);ib=pairs(it.content.lower())
            score+=3*len(qb&ib)/max(1,len(qb))
            core=slot=='preferred_name' or it.category=='style' or it.owner_id in GLOBAL_OWNERS
            if score==0 and not core:continue
            # 内容字面命中
            content_chars = set(it.content.lower())
            if q_chars:
                overlap = len(q_chars & content_chars) / len(q_chars)
                score += overlap * 2.0
            # 置信度与使用频率
            score += it.confidence * 1.0
            score += it.importance*.3
            score += min(it.hits, 10) * 0.1
            # 时效：越新越优先（半衰期 90 天）
            age_days = (now - it.updated_at) / 86400
            score += 1.5 * math.exp(-age_days / 90)

            scored.append((score, it))

        scored.sort(key=lambda x: x[0], reverse=True)

        picked: list[MemoryItem] = []
        for _, it in scored[:limit]:
            it.hits += 1
            picked.append(it)
        if picked:
            try:
                self._save()
            except OSError:
                log.warning("记忆引用次数写盘失败，仍使用已加载的内容")
        return picked

    # ══ ④ 注入 ═══════════════════════════════════════════════

    def build_context(self, user_id: str, chat_id: str, query: str, platform: str = "") -> str:
        """生成注入到系统提示里的记忆文本。"""
        items = self.recall(user_id, chat_id, query,platform=platform)
        if not items:
            return ""

        lines = []
        for it in items:
            tag = CATEGORIES.get(it.category, "")
            # 低置信度的标注出来，让模型知道这条不一定准
            mark = "（不确定）" if it.confidence < 0.6 else ""
            stamp=time.strftime('%Y-%m-%d',time.localtime(it.valid_from or it.created_at))
            old = ('（历史信息，已被更新）' if it.status != 'active' else
                   '（历史事件，已过期）' if it.expires_at and it.expires_at <= time.time() else '')
            line=f"- [{tag}，{stamp}] {it.content}{mark}{old}"
            if sum(len(l) for l in lines)+len(line)>800:break
            lines.append(line)
        return (
            "以下是你之前了解到的关于这位用户的信息，回答时可参考，"
            "但不要生硬地全部复述出来：\n" + "\n".join(lines)
        )

    # ══ 管理接口（供界面使用）═══════════════════════════════

    def list_items(self, scope: str = "", owner_id: str = "",
                   category: str = "", keyword: str = "", include_history: bool = False) -> list[dict]:
        out = []
        for it in self._items.values():
            if not include_history and (it.status!="active" or (it.expires_at and it.expires_at<time.time())):continue
            if scope and it.scope != scope:
                continue
            if owner_id and it.owner_id != owner_id:
                continue
            if category and it.category != category:
                continue
            if keyword:
                k = keyword.lower()
                if k not in it.content.lower() and not any(k in w.lower() for w in it.keywords):
                    continue
            out.append(it.to_dict())
        out.sort(key=lambda x: x["updated_at"], reverse=True)
        return out

    def delete(self, item_id: str) -> bool:
        if item_id in self._items:
            before = copy.deepcopy(self._items)
            item = self._items[item_id]
            identity = self.identity_key(item.platform, item.owner_id)
            self._revisions[identity] = self._revisions.get(identity, 0) + 1
            del self._items[item_id]
            try:
                self._save()
            except Exception:
                self._items = before
                raise
            self.learner.clear(item.scope, item.owner_id)
            return True
        return False

    def clear(self, scope: str = "", owner_id: str = "") -> int:
        """清空记忆。不带参数则全清。"""
        if not scope and not owner_id:
            self._generation += 1
        else:
            for item in self._items.values():
                if (not scope or item.scope == scope) and (not owner_id or item.owner_id == owner_id):
                    identity = self.identity_key(item.platform, item.owner_id)
                    self._revisions[identity] = self._revisions.get(identity, 0) + 1
        before = copy.deepcopy(self._items)
        if not scope and not owner_id:
            n = len(self._items)
            self._items.clear()
        else:
            ids = [i.id for i in self._items.values()
                   if (not scope or i.scope == scope) and
                      (not owner_id or i.owner_id == owner_id)]
            n = len(ids)
            for i in ids:
                del self._items[i]
        try:
            self._save()
        except Exception:
            self._items = before
            raise
        self.learner.clear(scope, owner_id)
        return n

    def add_manual(self, scope: str, owner_id: str, category: str,
                   content: str, keywords: list[str] | None = None, platform: str = "", fact_key: str = "") -> dict:
        """手动添加一条记忆（用户在界面上补录）。"""
        identity = self.identity_key(platform, owner_id)
        self._revisions[identity] = self._revisions.get(identity, 0) + 1
        before = copy.deepcopy(self._items)
        item = self._merge_or_create(
            scope=scope, owner_id=owner_id, category=category,
            content=content.strip(), keywords=keywords or [],
            source_chat="manual", platform=platform, fact_key=fact_key,
        )
        item.confidence = 1.0        # 手动添加的视为确定
        try:
            self._save()
        except Exception:
            self._items = before
            raise
        return item.to_dict()

    def stats(self) -> dict:
        by_cat: dict[str, int] = {}
        by_scope: dict[str, int] = {}
        owners: set[str] = set()
        for it in self._items.values():
            if it.status != "active" or (it.expires_at and it.expires_at <= time.time()):
                continue
            by_cat[it.category] = by_cat.get(it.category, 0) + 1
            by_scope[it.scope] = by_scope.get(it.scope, 0) + 1
            owners.add(f"{it.scope}:{self.identity_key(it.platform,it.owner_id)}")
        return {
            "total": sum(i.status=="active" and (not i.expires_at or i.expires_at>time.time()) for i in self._items.values()),
            "historical":sum(i.status!="active" or (i.expires_at and i.expires_at<=time.time()) for i in self._items.values()),
            "schema_version":2,
            "enabled": bool(self.enabled), "inject": bool(self.inject),
            "learning": self.learner.status(),
            "byCategory": by_cat,
            "byScope": by_scope,
            "owners": len(owners),
            "avgConfidence": round(
                sum(i.confidence for i in self._items.values()) / len(self._items), 2
            ) if self._items else 0,
        }

    def _trim(self) -> None:
        """超出上限时淘汰最不重要的记忆（低置信度 + 久未使用 + 陈旧）。"""
        if len(self._items) <= MAX_ITEMS:
            return
        now = time.time()

        def weight(i: MemoryItem) -> float:
            age_days = (now - i.updated_at) / 86400
            return (0 if i.status=="active" else -10)+i.confidence*2+i.importance+min(i.hits,10)*.3-age_days/365

        ordered = sorted(self._items.values(), key=weight)
        for it in ordered[: len(self._items) - MAX_ITEMS]:
            del self._items[it.id]
