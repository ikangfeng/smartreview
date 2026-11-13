# =============================================================================
# 规则自学习 - rule_learner.py
# =============================================================================
# 解决的问题：法务反复把"违约金上限"从 25% 改到 20%、把"预付款"从 40% 降到 30%，
# 这些偏好每次都靠模型现场推断，既慢又不稳定。
#
# 本模块把历史"已采纳/已修改"的改写稿做频次统计：
#   同一主题下，若 ≥ min_support 条改写都收敛到同一个数值约束（百分比/天数），
#   就提示"要不要把它固化成企业规则"——固化成规则后由确定性通道执行，
#   零 token、零幻觉、且对新合同立即生效。
#
# 为什么不做自动写入规则：规则是风控红线，误写代价高。这里只"提建议"，
# 由人点确认后才落库——AI 提假设、人来否决，这是可审计的边界。
# =============================================================================

import re
from typing import List

from db import review_repository as repo

# 主题 → 用于归类条款的关键词（覆盖合同里最常被反复调整的几类数值条款）
TOPIC_KEYWORDS = {
    "违约金上限": ["违约金", "逾期交付", "逾期付款"],
    "预付款比例": ["预付款", "预付"],
    "质保期": ["质保", "质量保证", "保修"],
    "保密期限": ["保密期限", "保密期"],
    "赔偿上限": ["赔偿", "损失赔偿"],
}

_PERCENT_RE = re.compile(r"(?:不超过|不得超过|不得高于|不高于|上限为|最高)\s*(?:合同总额|合同金额|总价|总额)?\s*的?\s*(\d{1,3}(?:\.\d+)?)\s*%")
_DAYS_RE = re.compile(r"(\d{1,3})\s*(?:个)?(?:工作日|日|天)")
# "倍"是赔偿/违约金封顶最常用的口径（如"不得超过实际损失的 1.5 倍"），
# 早期版本只认百分比会漏掉这类反复出现的强口径，故单列一类。
_MULTIPLE_RE = re.compile(r"(?:不超过|不得超过|不得高于|不高于|上限为|最高为?)\s*(?:实际损失|合同总额|合同金额)?\s*的?\s*(\d+(?:\.\d+)?)\s*倍")


def _topic_of(text: str) -> str:
    """按关键词把条款归到一个主题；无法归类返回空串（不参与建议）。"""
    for topic, words in TOPIC_KEYWORDS.items():
        if any(w in text for w in words):
            return topic
    return ""


def _extract_constraints(text: str) -> dict:
    """从改写文本中抽数值约束：百分比上限 / 倍数上限 / 期限天数。"""
    out = {}
    m = _MULTIPLE_RE.search(text or "")
    if m:
        out["multiple"] = float(m.group(1))
    m = _PERCENT_RE.search(text or "")
    if m:
        out["percent"] = float(m.group(1))
    m2 = _DAYS_RE.search(text or "")
    if m2:
        out["days"] = int(m2.group(1))
    return out


def suggest_rules(min_support: int = 3) -> dict:
    """分析历史采纳改写，产出规则固化建议 + 样本池规模。

    返回 {sample_pool, total_samples, suggestions[]}。
    suggestions 里每条都给出 support（支持该结论的历史条数），
    让人能判断这条建议是否值得固化。
    """
    pool = repo.count_dispositions()
    rows = repo.get_adopted_revisions(limit=500)

    # topic → {"percent": {值: 次数}, "days": {值: 次数}}
    buckets: dict = {}
    for r in rows:
        topic = _topic_of(r.get("content") or "")
        if not topic:
            continue
        cons = _extract_constraints(r.get("final_text") or "")
        if not cons:
            continue
        b = buckets.setdefault(topic, {"multiple": {}, "percent": {}, "days": {}})
        for k, v in cons.items():
            b[k][v] = b[k].get(v, 0) + 1

    suggestions: List[dict] = []
    for topic, b in buckets.items():
        for kind, counter in b.items():
            for value, support in counter.items():
                if support < min_support:
                    continue
                if kind == "multiple":
                    msg = (f"{support} 条历史改写都把「{topic}」封顶在 {value:g} 倍，"
                           f"可考虑固化为企业规则，后续由确定性通道直接卡住")
                    rule = {
                        "name": f"{topic}封顶 {value:g} 倍（学习自历史处置）",
                        "matcher_kind": "keyword_regex",
                        "params": {"keywords": [topic]},
                        "risk_level": "高",
                        "description": f"{topic}未约定 {value:g} 倍封顶时提示",
                        "suggestion": f"建议{topic}总额不超过实际损失的 {value:g} 倍",
                    }
                elif kind == "percent":
                    msg = (f"{support} 条历史改写都把「{topic}」收敛到不超过 {value:g}%，"
                           f"可考虑固化为企业规则，后续由确定性通道直接卡住")
                    rule = {
                        "name": f"{topic}上限 {value:g}%（学习自历史处置）",
                        "matcher_kind": "percent_ceiling",
                        "params": {"threshold": value, "scope": topic},
                        "risk_level": "高",
                        "description": f"{topic}超过合同总额 {value:g}% 时触发",
                        "suggestion": f"将{topic}调整为不超过合同总额的 {value:g}%",
                    }
                else:
                    msg = (f"{support} 条历史改写都把「{topic}」设为 {value} 日，"
                           f"可考虑固化为企业规则")
                    rule = {
                        "name": f"{topic} {value} 日（学习自历史处置）",
                        "matcher_kind": "keyword_any",
                        "params": {"keywords": [topic]},
                        "risk_level": "中",
                        "description": f"{topic}期限与历史口径 {value} 日不一致时提示",
                        "suggestion": f"建议{topic}约定为 {value} 日",
                    }
                suggestions.append({
                    "topic": topic,
                    "kind": kind,
                    "value": value,
                    "support": support,
                    "message": msg,
                    "suggested_rule": rule,
                })

    suggestions.sort(key=lambda x: x["support"], reverse=True)
    return {
        "sample_pool": pool,
        "total_samples": len(rows),
        "suggestions": suggestions,
    }
