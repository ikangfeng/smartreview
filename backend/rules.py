# =============================================================================
# 企业风控规则引擎 - rules.py（确定性判断通道）
# =============================================================================
# 与 LLM 通道构成"双通道判断"（见架构文档 DR-4）：规则命中即直接裁决，
# 保证 PRD"高风险误判率<1%"的红线不依赖模型输出。
#
# 本模块保持**纯函数**（不依赖 DB/网络），便于单测。
# 规则来源：db.risk_rules 表（P2 可配置），matcher_kind 支持：
#   - percent_ceiling  数值上限类：正则捕获百分比，超过阈值即命中
#   - ip_rights        知识产权归属类：非甲方独占时命中
#   - keyword_any      关键词类：出现任一关键词即命中
#   - keyword_regex    正则模式类：triggers 字段（可为 | 分隔的多条正则）任一命中
# =============================================================================

import re
from typing import List, Optional

# 风险等级严重度（用于多规则命中时取最高者）
_SEVERITY = {"高": 3, "中": 2, "低": 1, "无": 0}

_RULE_CONFIDENCE = 0.95  # 确定性规则置信度


def match_percent_ceiling(content: str, params: dict) -> bool:
    """命中：条款中出现高于阈值的百分比数值（默认 20% 企业底线）。

    新增 trigger_keywords 前置约束：params 若带了 trigger_keywords 列表，
    则条款里必须先出现这些关键词之一，才去检查百分比是否超标。
    这避免"预付款 50%"被"违约金上限 20%"规则误命中——预付款条款没有"违约金"
    关键词，直接跳过百分比扫描。未配置 trigger_keywords 时保持旧行为（全条款扫）。
    """
    trigger_keywords = params.get("trigger_keywords") or params.get("triggers") or []
    # 前置关键词检查：必须先有其一，才去扫百分比
    if trigger_keywords and not any(kw in content for kw in trigger_keywords):
        return False

    regex = params.get("regex") or r"(\d+(?:\.\d+)?)\s*%"
    threshold = float(params.get("threshold", 20.0))
    for m in re.finditer(regex, content):
        try:
            if float(m.group(1)) > threshold:
                return True
        except (ValueError, IndexError):
            continue
    return False


def match_ip_rights(content: str) -> bool:
    """命中：涉知识产权且约定非甲方独占（共有/归乙方等表述）。"""
    ip_hit = re.search(r"著作权|知识产权|专利权|商标权|技术成果|软件著作权", content)
    if not ip_hit:
        return False
    if re.search(r"甲方\s*(独占|独有|唯一|全部所有)|归甲方\s*(所有|独有|享有)", content):
        return False
    return bool(re.search(r"共有|共同所有|双方共同|归双方|归乙方|乙方所有|供方所有", content))


def match_keyword_any(content: str, params: dict) -> bool:
    """命中：出现任一预设关键词。"""
    keywords = params.get("keywords") or []
    return any(kw in content for kw in keywords)


def match_keyword_regex(content: str, params: dict) -> bool:
    """命中：triggers 里任一正则模式匹配成功。

    triggers 字段兼容两种格式：
    1. "xxx|yyy" —— 单字符串，用 "|" 拆成多条
    2. ["xxx", "yyy"] —— 已是列表，直接遍历

    任何一条正则匹配成功即返回 True；编译失败静默跳过该模式。
    """
    triggers = params.get("triggers") or params.get("trigger") or []
    if isinstance(triggers, str):
        # 按 "|" 拆，但先用正则判断整体是否已是合法正则（含 .{, \d 等元字符时
        # 视为单条正则，不拆，避免把 "知识产权.{0,20}(共有|乙方)" 这类截断）
        if re.search(r"\.\{|\\d|\\w|\(\?|\[.+\]", triggers):
            patterns = [triggers]
        else:
            patterns = [t.strip() for t in triggers.split("|") if t.strip()]
    else:
        patterns = [str(p) for p in triggers if p]

    for pat in patterns:
        try:
            if re.search(pat, content):
                return True
        except re.error:
            continue
    return False


def match_rule(content: str, rule: dict) -> bool:
    """按 matcher_kind 分发单条规则匹配。"""
    kind = rule.get("matcher_kind")
    params = rule.get("params") or {}
    if kind == "percent_ceiling":
        return match_percent_ceiling(content, params)
    if kind == "ip_rights":
        return match_ip_rights(content)
    if kind == "keyword_any":
        return match_keyword_any(content, params)
    if kind == "keyword_regex":
        return match_keyword_regex(content, params)
    return False


def build_rule_verdict(rule: dict) -> dict:
    """把命中的规则转成与 LLM 裁决同构的 verdict。"""
    name = rule.get("name", "")
    return {
        "risk_level": rule.get("risk_level", "中"),
        "risk_description": f"【规则:{name}】{rule.get('description', '')}",
        "law_basis": "",
        "suggestion": rule.get("suggestion", ""),
        "confidence": _RULE_CONFIDENCE,
        "rule_name": name,
    }


def evaluate_clause_rules(content: str, rules: List[dict]) -> Optional[dict]:
    """
    对一条条款执行全部启用规则；返回最高严重度命中的 verdict，无命中返回 None。
    """
    best: Optional[dict] = None
    best_severity = -1
    for rule in rules:
        try:
            if not match_rule(content, rule):
                continue
        except Exception:
            continue  # 单条规则异常不影响审查
        severity = _SEVERITY.get(rule.get("risk_level", "中"), 0)
        if severity > best_severity:
            best = build_rule_verdict(rule)
            best_severity = severity
    return best
