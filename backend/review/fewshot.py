# =============================================================================
# 少样本经验召回 - fewshot.py
# =============================================================================
# "越用越聪明"的最小诚实实现：不做微调（demo 阶段是伪需求），
# 而是把法务每一次"已采纳/已修改"沉淀为企业经验库，下次审同类条款时
# 检索最相似的 3 条历史改写作为 few-shot，注入 Prompt。
#
# 为什么用字面相似度而不是向量检索：
#   1. 样本量小（几十~几百条），向量索引的维护成本高于收益；
#   2. 条款文本同构性极强（同一类合同措辞高度相似），
#      字符二元组 Jaccard 已足够把"违约金类"和"保密类"分开；
#   3. 无外部依赖、结果可解释（面试可当场展示"命中了哪 3 条先例"）。
# 若样本量增长到千级以上，再换成 kb 那条向量链路即可，接口不变。
# =============================================================================

import re
from typing import List

from db import review_repository as repo


def _bigrams(text: str) -> set:
    """取汉字/字母数字的二元组集合，作为轻量相似度特征。"""
    s = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", text or "")
    if len(s) < 2:
        return set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set, b: set) -> float:
    """Jaccard 相似度：交集/并集，0~1。"""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def retrieve_examples(clause_content: str, limit: int = 3,
                      min_similarity: float = 0.08) -> List[dict]:
    """从历史已采纳改写中取与当前条款最相似的若干条，作为 few-shot 先例。

    返回 [{clause_id, original, revised, similarity}]。
    min_similarity 门槛刻意设低：宁可在候选池里多带一条弱相关先例，
    也不要因为阈值过高导致这个能力"看起来没生效"。
    """
    rows = repo.get_adopted_revisions(limit=300)
    if not rows:
        return []
    target = _bigrams(clause_content)
    if not target:
        return []

    scored = []
    for r in rows:
        sim = _jaccard(target, _bigrams(r.get("content") or ""))
        if sim >= min_similarity:
            scored.append((sim, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    examples = []
    for sim, r in scored[:limit]:
        examples.append({
            "clause_id": r.get("clause_id") or "",
            "original": (r.get("content") or "")[:600],
            "revised": (r.get("final_text") or "")[:600],
            "similarity": round(sim, 3),
        })
    return examples


def render_examples_block(examples: List[dict]) -> str:
    """把先例渲染成 Prompt 片段；无先例返回空串（不占 token）。"""
    if not examples:
        return ""
    parts = ["【企业历史采纳的改写先例】（同类条款，法务已确认，请对齐其处理口径与措辞习惯）"]
    for i, ex in enumerate(examples, start=1):
        parts.append(
            f"\n先例 {i}（原条款 {ex['clause_id'] or '-'}）：\n"
            f"- 改写前：{ex['original']}\n"
            f"- 改写后：{ex['revised']}"
        )
    parts.append("\n注意：先例仅用于对齐改写风格与风险处置口径，"
                 "不得照抄与本条款无关的金额、比例、主体名称。\n")
    return "\n".join(parts) + "\n"
