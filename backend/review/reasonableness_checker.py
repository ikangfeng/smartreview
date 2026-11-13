# =============================================================================
# 合同合理性检查 - reasonableness_checker.py
# =============================================================================
# 与"法律风险"正交的商业合理性维度：条款不违法，但权利义务失衡、不可执行
# 或对甲方明显不利。判断立场固定为**甲方利益视角**（本产品以甲方为委托方）。
#
# 分工（延续"确定性的事交给代码，语义交给 LLM"）：
#   - 本模块：可枚举/可量化的失衡点（单方解除、责任不对等、视为验收、
#     自动续约、单方调价、无限期保密），零 token；
#   - LLM：复用逐条审查的同一次调用，在输出 JSON 里补 reasonableness_issues，
#     不额外增加调用次数；
#   - 任务级：summarize_issues() 汇总为 0-100 合理性评分与一句话结论。
# =============================================================================

import re
from typing import List

REASON_TYPES = {"权利义务失衡", "责任不对等", "付款交付不匹配",
                "条款不可执行", "期限不合理", "其他"}
DIRECTIONS = {"对甲方不利", "对乙方不利", "双方失衡"}
_SEVERITY_ORDER = {"高": 0, "中": 1, "低": 2}
_MAX_ISSUES = 6  # 单条条款最多保留 6 条，防止刷屏


def _issue(rtype: str, original: str, issue: str, suggestion: str,
           severity: str = "中", direction: str = "对甲方不利") -> dict:
    """构造与 quality_issues 同构、多一个 direction 字段的合理性条目。"""
    return {"type": rtype, "direction": direction, "original": original[:60],
            "issue": issue, "suggestion": suggestion, "severity": severity}


# 乙方单方解除/终止（需带"单方/随时/无条件/无需甲方同意"等限定词才算失衡）
_RIGHT_UNILATERAL = re.compile(
    r"乙方[^。\n]{0,20}(?:有权|可以|可)[^。\n]{0,12}(?:单方|随时|无条件|自行)[^。\n]{0,10}"
    r"(?:解除|终止)")
_RIGHT_NO_CONSENT = re.compile(
    r"乙方[^。\n]{0,20}(?:无需|不需|不必)[^。\n]{0,10}(?:甲方)?(?:同意|确认)[^。\n]{0,10}"
    r"(?:解除|终止|变更)")
# 乙方保留最终解释权 / 以其认定为准
_RIGHT_INTERPRET = re.compile(
    r"(?:最终解释权|解释权)[^。\n]{0,10}(?:归|属于|由)[^。\n]{0,6}乙方"
    r"|以乙方[^。\n]{0,8}(?:解释|认定|确认)(?:为准)?")

# 甲方承担全部/无限责任
_LIABILITY_A_ALL = re.compile(
    r"甲方[^。\n]{0,12}(?:承担|赔偿)[^。\n]{0,8}(?:全部|一切|无限|任何)[^。\n]{0,6}"
    r"(?:责任|损失)")
# 乙方免责
_LIABILITY_B_EXEMPT = re.compile(
    r"乙方[^。\n]{0,10}(?:不承担|无需承担|概不承担|免除)[^。\n]{0,8}(?:任何)?"
    r"(?:责任|损失)")
# 仅对乙方设责任上限（甲方无限）
_LIABILITY_B_CAP = re.compile(
    r"乙方[^。\n]{0,10}(?:责任|赔偿)[^。\n]{0,10}(?:以|不超过|限于)[^。\n]{0,10}"
    r"(?:为限|元|万元)")

# 未实际验收即视为通过
_ACCEPT_DEEMED = re.compile(
    r"(?:视为|即视为)[^。\n]{0,8}(?:验收)?(?:合格|通过)"
    r"|(?:逾期|期满)[^。\n]{0,15}未[^。\n]{0,8}(?:异议|意见)[^。\n]{0,8}视为")

# 自动续约（若同时给了甲方退出权则不报）
_AUTO_RENEW = re.compile(r"自动(?:续约|续期|续签|延长)")
_EXIT_RIGHT = re.compile(
    r"(?:甲方|任一方)[^。\n]{0,15}(?:可|有权)[^。\n]{0,10}(?:不续约|终止|解除|退出)")
# 不可解除/不可撤销
_NO_EXIT = re.compile(r"不得(?:提前)?(?:单方)?解除|不可(?:提前)?解除|不可撤销")

# 乙方单方调价
_PRICE_UNILATERAL = re.compile(
    r"乙方[^。\n]{0,15}(?:有权|可以|可)[^。\n]{0,15}(?:调整|变更|提高|上涨)[^。\n]{0,8}"
    r"(?:价格|费用|服务费|价款|租金)")
_PRICE_BY_B = re.compile(
    r"(?:价格|费用|服务费|价款|租金)[^。\n]{0,10}(?:以|按)乙方[^。\n]{0,8}"
    r"(?:通知|公布|调整)(?:为准)?")

# 无限期保密（对接收方甲方构成长期义务）
_CONFIDENTIAL_FOREVER = re.compile(
    r"(?:永久|无限期)[^。\n]{0,6}保密"
    r"|保密(?:期限|义务)[^。\n]{0,8}(?:永久|无限期)")


def _check_clause_reasonableness(content: str) -> List[dict]:
    """确定性合理性检查（甲方视角）：只报可枚举的明显失衡点。"""
    issues: List[dict] = []
    if not content:
        return issues

    m = _RIGHT_UNILATERAL.search(content) or _RIGHT_NO_CONSENT.search(content)
    if m:
        issues.append(_issue(
            "权利义务失衡", m.group(0),
            "乙方享有单方解除/终止权，甲方无对等退出权",
            "建议增加甲方对等的解除权，或限定乙方单方解除的触发条件与通知期",
            "高"))

    m = _RIGHT_INTERPRET.search(content)
    if m:
        issues.append(_issue(
            "权利义务失衡", m.group(0),
            "乙方保留单方解释/认定权，争议时甲方处于不利地位",
            "删除单方解释权条款，改为按合同目的、交易习惯和法律规定解释",
            "中"))

    m = _LIABILITY_A_ALL.search(content)
    if m:
        issues.append(_issue(
            "责任不对等", m.group(0),
            "约定甲方承担全部/无限责任，责任分配明显偏向乙方",
            "为甲方责任设置合理上限，并要求乙方承担与其过错相当的责任",
            "高"))

    m = _LIABILITY_B_EXEMPT.search(content) or _LIABILITY_B_CAP.search(content)
    if m:
        issues.append(_issue(
            "责任不对等", m.group(0),
            "乙方免责或责任上限明显偏低，甲方救济不足",
            "删除乙方全面免责表述，至少保留故意/重大过失责任并设置对等上限",
            "高"))

    m = _ACCEPT_DEEMED.search(content)
    if m:
        issues.append(_issue(
            "条款不可执行", m.group(0),
            "未实际验收即视为合格，甲方将丧失质量异议权",
            "改为以甲方书面验收为准；逾期未验收仅视为同意延期，不视为验收合格",
            "高"))

    m = _AUTO_RENEW.search(content)
    if m and not _EXIT_RIGHT.search(content):
        issues.append(_issue(
            "期限不合理", m.group(0),
            "合同自动续期且未约定甲方退出机制，存在长期锁定风险",
            "增加甲方提前书面通知不续约/终止的权利，或改为双方书面确认后续约",
            "中"))
    else:
        m = _NO_EXIT.search(content)
        if m:
            issues.append(_issue(
                "期限不合理", m.group(0),
                "限制甲方单方解除，甲方缺乏退出机制",
                "保留甲方在乙方违约或服务不达标时的解除权",
                "中"))

    m = _PRICE_UNILATERAL.search(content) or _PRICE_BY_B.search(content)
    if m:
        issues.append(_issue(
            "权利义务失衡", m.group(0),
            "乙方保留单方调价权，甲方成本不可控",
            "价格调整须经双方书面确认，或设置调价上限与提前通知期",
            "高"))

    m = _CONFIDENTIAL_FOREVER.search(content)
    if m:
        issues.append(_issue(
            "期限不合理", m.group(0),
            "保密义务无期限，甲方长期承担保密负担",
            "约定合理保密期限（如合同终止后 3-5 年），公开信息等除外",
            "低"))

    # 去重 + 按严重度排序 + 截断
    unique, seen = [], set()
    for item in issues:
        key = (item["type"], item["issue"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda x: _SEVERITY_ORDER.get(x["severity"], 3))
    return unique[:_MAX_ISSUES]


def merge_reasonableness(deterministic: List[dict], llm_issues: List[dict],
                         stance: str = "甲方") -> List[dict]:
    """合并确定性检查与模型输出，并按审查立场过滤方向。

    三条硬过滤：
      1. 与立场相反方向的条目剔除（甲方立场剔除"对乙方不利"，乙方立场剔除
         "对甲方不利"；中立保留双方条目）——避免立场与结论自相矛盾；
      2. 确定性规则已覆盖的类型，模型同类型输出一律丢弃——同一条款里
         "乙方单方解除"常被模型换个说法重复输出，留着只会刷屏；
      3. 同 (type, issue) 去重。
    """
    deterministic = list(deterministic or [])
    blocked = {"甲方": "对乙方不利", "乙方": "对甲方不利"}.get(stance)
    covered_types = {item.get("type") for item in deterministic}
    merged, seen = [], set()
    for item in deterministic:
        key = (item.get("type"), item.get("issue"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    for item in llm_issues or []:
        if blocked and item.get("direction") == blocked:
            continue
        if item.get("type") in covered_types:
            continue
        key = (item.get("type"), item.get("issue"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    merged.sort(key=lambda x: _SEVERITY_ORDER.get(x.get("severity"), 3))
    return merged[:_MAX_ISSUES]


def _mirror_parties(text: str) -> str:
    """交换文中的甲方/乙方（乙方立场复用同一套甲方视角规则）。"""
    return (text or "").replace("甲方", "\x00").replace("乙方", "甲方").replace("\x00", "乙方")


def _mirror_issue(item: dict) -> dict:
    """把甲方视角的条目镜像为乙方视角：交换主体称谓，方向改为对乙方不利。"""
    mirrored = dict(item)
    for key in ("original", "issue", "suggestion"):
        mirrored[key] = _mirror_parties(item.get(key) or "")
    mirrored["direction"] = "对乙方不利"
    return mirrored


def _dedupe_sorted(issues: List[dict]) -> List[dict]:
    """去重 + 按严重度排序 + 截断（中立立场双向检查后合并用）。"""
    unique, seen = [], set()
    for item in issues:
        key = (item.get("type"), item.get("issue"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda x: _SEVERITY_ORDER.get(x.get("severity"), 3))
    return unique[:_MAX_ISSUES]


def check_clause_reasonableness(content: str, stance: str = "甲方") -> List[dict]:
    """按审查立场做确定性合理性检查。

    - 甲方：沿用甲方视角规则；
    - 乙方：先把条款中的甲/乙方互换，跑同一套规则后再镜像回输出，
      使“对方单方解除/己方无限责任”等失衡点仍能被识别（避免系统性偏向）；
    - 中立：双向各跑一次，合并双方失衡条目。
    """
    if not content:
        return []
    if stance == "乙方":
        return _dedupe_sorted([_mirror_issue(i) for i in
                               _check_clause_reasonableness(_mirror_parties(content))])
    if stance == "中立":
        mine = _check_clause_reasonableness(content)
        theirs = [_mirror_issue(i) for i in
                  _check_clause_reasonableness(_mirror_parties(content))]
        return _dedupe_sorted(mine + theirs)
    return _check_clause_reasonableness(content)


def summarize_issues(issues: List[dict], stance: str = "甲方") -> dict:
    """把全部条款的合理性问题汇总为合同级评分与结论（按审查立场措辞）。"""
    counts = {"高": 0, "中": 0, "低": 0}
    type_counter = {}
    for item in issues or []:
        severity = item.get("severity") if item.get("severity") in counts else "低"
        counts[severity] += 1
        rtype = item.get("type") or "其他"
        type_counter[rtype] = type_counter.get(rtype, 0) + 1

    score = max(0, 100 - counts["高"] * 12 - counts["中"] * 6 - counts["低"] * 2)
    if score >= 85:
        conclusion = "整体条款较为合理，未发现明显失衡"
    elif score >= 70:
        conclusion = ("基本合理，个别条款存在明显失衡" if stance == "中立"
                      else f"基本合理，个别条款对{stance}不利或存在失衡")
    elif score >= 50:
        conclusion = "存在明显失衡，建议重点谈判标红条款"
    else:
        conclusion = ("合理性较差，多项核心义务/责任分配明显失衡" if stance == "中立"
                      else f"合理性较差，多项核心义务/责任对{stance}明显不利")

    if type_counter:
        top = sorted(type_counter.items(), key=lambda kv: kv[1], reverse=True)[:2]
        conclusion += "；主要问题集中在：" + "、".join(f"{k}（{v} 条）" for k, v in top)

    return {
        "score": score,
        "conclusion": conclusion,
        "counts": counts,
        "total": sum(counts.values()),
    }
