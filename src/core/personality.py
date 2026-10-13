"""Versioned persona prompt conditioning and scene-specific few-shot selection."""
from __future__ import annotations

from ..personas.phrolova import (
    CORE_PROMPT, EXAMPLES, OVERVIEW, PROFILE_ID, PROFILE_VERSION,
    RELATIONSHIPS, SCENE_KEYWORDS,
)

PROFILE_IDS = {"custom", PROFILE_ID}
RELATIONSHIP_IDS = set(RELATIONSHIPS)


def profile_prompt(profile_id: str, relationship: str = "neutral") -> str:
    if profile_id != PROFILE_ID:
        return ""
    return CORE_PROMPT + "\n\n" + RELATIONSHIPS.get(relationship, RELATIONSHIPS["neutral"])


def detect_scene(query: str) -> str:
    query = (query or "").casefold()
    for scene, terms in SCENE_KEYWORDS.items():
        if any(term in query for term in terms):
            return scene
    return "daily"


def few_shot_messages(profile_id: str, query: str,
                      relationship: str = "neutral", limit: int = 2) -> list[dict]:
    if profile_id != PROFILE_ID or limit <= 0:
        return []
    limit = min(limit, 2)
    scene = detect_scene(query)
    mode = relationship if relationship in RELATIONSHIP_IDS else "neutral"
    candidates = [e for e in EXAMPLES if e["scene"] == scene
                  and e["relationship"] in ("neutral", mode)]
    if not candidates:
        candidates = [e for e in EXAMPLES if e["scene"] == "daily"
                      and e["relationship"] == "neutral"]
    q_chars = set((query or "").casefold())
    candidates = sorted(candidates, key=lambda e: (
        -len(q_chars & set(e["user"].casefold())),
        -(e["relationship"] == mode), e["id"],
    ))
    messages = [{"role": "system", "content": (
        "[角色表达校准：合成改写示例]以下示例只用于语气与行为校准，"
        "不是官方新增台词，也不是当前用户真实经历或已发生的亲密关系。"
        "不要把示例当作本次对话历史。当前情境：" + scene
    )}]
    for example in candidates[:limit]:
        messages += [{"role":"user", "content":example["user"]},
                     {"role":"assistant", "content":example["assistant"]}]
    return messages


def public_profiles() -> list[dict]:
    return [{"id":"custom", "name":"自定义人格"}, {
        "id": PROFILE_ID, "name":"弗洛洛（鸣潮）", "version":PROFILE_VERSION,
        "default_name":"弗洛洛", "default_persona":OVERVIEW,
        "relationships":{"neutral":"普通聊天", "trusted":"信任日常", "rover":"漂泊者剧情扮演"},
        "method":"人格提示词 + 场景少样本；模型权重未修改",
    }]
