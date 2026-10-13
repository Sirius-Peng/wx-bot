"""人格知识库 —— 让机器人随着对话不断了解用户。

═══════════════════════════════════════════════════════════════════
四个环节构成闭环：

  ① 抽取  每轮对话后，用便宜模型从对话里挑出"值得长期记住"的信息
  ② 融合  新信息与已有记忆冲突/重复时**更新**而非追加，避免越攒越乱
  ③ 检索  回答前按相关度挑出该用的记忆（关键词 + 置信度 + 时效）
  ④ 注入  把挑出的记忆作为系统提示喂给模型

与普通"对话历史"的区别：
  历史是**短期**的（几十条就丢），知识库是**长期**的（跨会话留存），
  并且会**演化** —— 用户换了工作，旧记忆会被更新而不是并存两条矛盾信息。

存储用 JSON 文件，延续本项目"零外部依赖"的设计（不需要数据库）。
记忆量在单用户几百条量级，简单打分检索足够；真到几千条再考虑向量检索。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import copy
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

    def to_dict(self) -> dict:
        d = asdict(self)
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

    def __init__(self, path: Path, llm=None, enabled: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.llm = llm                  # 用于抽取记忆（走便宜模型）
        self.enabled = enabled
        self.inject = True
        self._items: dict[str, MemoryItem] = {}
        self._load()
        from .memory_learning import MemoryLearningQueue
        self.learner = MemoryLearningQueue(self, self.path.with_name("memory-learning.json"))

    # ══ 持久化 ═══════════════════════════════════════════════

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for d in raw.get("items", []):
                item = MemoryItem(**{k: v for k, v in d.items()
                                     if k in MemoryItem.__dataclass_fields__})
                self._items[item.id] = item
            log.info("知识库已加载 %d 条记忆", len(self._items))
        except Exception:
            log.warning("知识库加载失败，从空开始", exc_info=True)

    def _save(self) -> None:
        data = {"version": 1, "items": [asdict(i) for i in self._items.values()]}
        fd, temp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=1)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.path)
        finally:
            Path(temp).unlink(missing_ok=True)

    # ══ ① 抽取 ═══════════════════════════════════════════════

    async def observe(self, chat_id: str, chat_type: str, user_id: str,
                      user_name: str, history: list[dict]) -> list[MemoryItem]:
        """从一段对话中抽取记忆并融合进知识库。

        @param history 最近几轮问答，形如 [{"user": "...", "assistant": "..."}]
        """
        if not self.enabled:
            raise MemoryExtractionError("automatic accumulation paused")
        if not self.llm:
            raise MemoryExtractionError("extraction provider not configured")
        if not history:
            return []
        convo = "\n".join(f"用户({user_name}): {t.get('user', '')}" for t in history[-6:])
        raw = await self.llm.complete(EXTRACT_PROMPT.format(conversation=convo), max_tokens=1200)
        extracted = self._parse_json_array(raw, strict=True)
        if not self.enabled:
            raise MemoryExtractionError("automatic accumulation paused")
        valid = []
        for entry in extracted:
            content = entry.get("content")
            keywords = entry.get("keywords", [])
            if (isinstance(content, str) and 0 < len(content.strip()) <= 200
                    and isinstance(entry.get("category"), str) and entry["category"] in CATEGORIES and isinstance(keywords, list)
                    and all(isinstance(k, str) for k in keywords)):
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
                    source_chat=chat_id))
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

    def _merge_or_create(self, scope: str, owner_id: str, category: str,
                         content: str, keywords: list[str],
                         source_chat: str) -> MemoryItem:
        """与已有记忆融合。

        判定"同一条"的依据：同作用域 + 同类别 + 关键词重叠度高。
        命中则更新（内容取新的、置信度提升、保留创建时间），
        否则新建。这样避免同一件事被反复记录成多条互相矛盾的记忆。
        """
        now = time.time()
        best: Optional[MemoryItem] = None
        best_sim = 0.0

        for it in self._items.values():
            if it.scope != scope or it.owner_id != owner_id or it.category != category:
                continue
            sim = self._similarity(it, content, keywords)
            if sim > best_sim:
                best, best_sim = it, sim

        # 相似度阈值：够像就认为是同一件事
        if best is not None and best_sim >= 0.5:
            best.content = content                      # 用最新表述覆盖
            best.keywords = list(dict.fromkeys(best.keywords + keywords))[:12]
            best.confidence = min(1.0, best.confidence + 0.15)   # 反复出现 → 更可信
            best.updated_at = now
            return best

        item = MemoryItem(
            id=uuid.uuid4().hex[:12], scope=scope, owner_id=owner_id,
            category=category, content=content, keywords=keywords[:12],
            confidence=0.7, hits=0, created_at=now, updated_at=now,
            source_chat=source_chat,
        )
        self._items[item.id] = item
        return item

    @staticmethod
    def _similarity(item: MemoryItem, content: str, keywords: list[str]) -> float:
        """粗略相似度：关键词重叠 + 字面包含。"""
        old_kw = {k.lower() for k in item.keywords}
        new_kw = {k.lower() for k in keywords}
        kw_score = 0.0
        if old_kw or new_kw:
            inter = len(old_kw & new_kw)
            union = len(old_kw | new_kw) or 1
            kw_score = inter / union                       # Jaccard

        # 字面包含：新内容是旧内容的子串或反之，多半是同一件事的不同说法
        a, b = item.content.strip(), content.strip()
        contain = 0.0
        if a and b:
            if a in b or b in a:
                contain = 0.8
            else:
                # 公共字符比例
                common = len(set(a) & set(b))
                contain = common / max(len(set(a) | set(b)), 1) * 0.5

        return max(kw_score, contain)

    # ══ ③ 检索 ═══════════════════════════════════════════════

    def recall(self, user_id: str, chat_id: str, query: str,
               limit: int = MAX_INJECT) -> list[MemoryItem]:
        """按相关度挑出该用的记忆。

        打分 = 关键词命中 + 置信度 + 使用频率 + 时效性
        """
        if not self._items:
            return []

        q = (query or "").lower()
        q_chars = set(q)
        now = time.time()
        scored: list[tuple[float, MemoryItem]] = []

        for it in self._items.values():
            # 取该用户本人的、全局通用的、以及该群作用域的记忆
            if not (
                (it.scope == SCOPE_USER and it.owner_id in GLOBAL_OWNERS) or
                (it.scope == SCOPE_USER and it.owner_id == user_id) or
                (it.scope == SCOPE_GROUP and it.owner_id == chat_id)
            ):
                continue

            score = 0.0
            # 关键词命中（最重要）
            for kw in it.keywords:
                if kw and kw.lower() in q:
                    score += 3.0
            # 内容字面命中
            content_chars = set(it.content.lower())
            if q_chars:
                overlap = len(q_chars & content_chars) / len(q_chars)
                score += overlap * 2.0
            # 置信度与使用频率
            score += it.confidence * 2.0
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

    def build_context(self, user_id: str, chat_id: str, query: str) -> str:
        """生成注入到系统提示里的记忆文本。"""
        items = self.recall(user_id, chat_id, query)
        if not items:
            return ""

        lines = []
        for it in items:
            tag = CATEGORIES.get(it.category, "")
            # 低置信度的标注出来，让模型知道这条不一定准
            mark = "（不确定）" if it.confidence < 0.6 else ""
            lines.append(f"- [{tag}] {it.content}{mark}")
        return (
            "以下是你之前了解到的关于这位用户的信息，回答时可参考，"
            "但不要生硬地全部复述出来：\n" + "\n".join(lines)
        )

    # ══ 管理接口（供界面使用）═══════════════════════════════

    def list_items(self, scope: str = "", owner_id: str = "",
                   category: str = "", keyword: str = "") -> list[dict]:
        out = []
        for it in self._items.values():
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
            del self._items[item_id]
            self._save()
            return True
        return False

    def clear(self, scope: str = "", owner_id: str = "") -> int:
        """清空记忆。不带参数则全清。"""
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
        self.learner.clear(scope, owner_id)
        self._save()
        return n

    def add_manual(self, scope: str, owner_id: str, category: str,
                   content: str, keywords: list[str] | None = None) -> dict:
        """手动添加一条记忆（用户在界面上补录）。"""
        item = self._merge_or_create(
            scope=scope, owner_id=owner_id, category=category,
            content=content.strip(), keywords=keywords or [],
            source_chat="manual",
        )
        item.confidence = 1.0        # 手动添加的视为确定
        self._save()
        return item.to_dict()

    def stats(self) -> dict:
        by_cat: dict[str, int] = {}
        by_scope: dict[str, int] = {}
        owners: set[str] = set()
        for it in self._items.values():
            by_cat[it.category] = by_cat.get(it.category, 0) + 1
            by_scope[it.scope] = by_scope.get(it.scope, 0) + 1
            owners.add(f"{it.scope}:{it.owner_id}")
        return {
            "total": len(self._items),
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
            return i.confidence * 2 + min(i.hits, 10) * 0.3 - age_days / 365

        ordered = sorted(self._items.values(), key=weight)
        for it in ordered[: len(self._items) - MAX_ITEMS]:
            del self._items[it.id]
