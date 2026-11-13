# =============================================================================
# 合同版本对比复审 - version_compare.py
# =============================================================================
# 合同谈判后回传 v2，需要回答三个问题：哪些风险已修复、哪些仍存在、
# 修改是否引入了新风险。本模块对两次任务的逐条结果做条款对齐（优先同条号，
# 其次内容相似度），再按风险等级变化给出 fixed/remaining/new/added/removed。
# 纯计算，不调用模型；只依赖 review_results 的条款正文与风险等级。
# =============================================================================

from difflib import SequenceMatcher
from typing import Dict, List

_RISKY = {"高", "中"}


def _norm(text: str) -> str:
    return "".join((text or "").split())


def _ratio(a: str, b: str) -> float:
    a, b = _norm(a)[:800], _norm(b)[:800]
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _status(base_risk: str, target_risk: str) -> str:
    base_risky = base_risk in _RISKY
    target_risky = target_risk in _RISKY
    if base_risky and not target_risky:
        return "fixed"
    if base_risky and target_risky:
        return "remaining"
    if not base_risky and target_risky:
        return "new"
    return "unchanged"


def _change(item: dict, status: str, base: dict = None, target: dict = None,
            order: int = 9999) -> dict:
    source = target or base or item
    return {
        "clause_id": source.get("clause_id") or item.get("clause_id"),
        "title": source.get("title") or (base or {}).get("title") or "",
        "status": status,
        "base_risk": (base or {}).get("risk_level"),
        "target_risk": (target or {}).get("risk_level"),
        "base_issue": ((base or {}).get("risk_description") or "")[:120],
        "target_issue": ((target or {}).get("risk_description") or "")[:120],
        "base_suggestion": ((base or {}).get("suggestion") or "")[:120],
        "target_suggestion": ((target or {}).get("suggestion") or "")[:120],
        "_order": order,
    }


def compare_results(base_results: List[dict], target_results: List[dict]) -> dict:
    """对齐两次审查结果并输出风险变化。base=旧版本，target=新版本。"""
    base_results = base_results or []
    target_results = target_results or []
    used_target = set()
    pairs = []  # (base_index, target_index)

    # 第一轮：同条号优先（且内容不能差异过大，避免“第三条”整体换内容被错配）
    by_id: Dict[str, List[int]] = {}
    for j, target in enumerate(target_results):
        by_id.setdefault(target.get("clause_id") or "", []).append(j)
    for i, base in enumerate(base_results):
        for j in by_id.get(base.get("clause_id") or "", []):
            if j in used_target:
                continue
            target = target_results[j]
            if (_ratio(base.get("content"), target.get("content")) >= 0.45
                    or not (base.get("content") or "").strip()
                    or not (target.get("content") or "").strip()):
                pairs.append((i, j))
                used_target.add(j)
                break

    # 第二轮：剩余条款按内容相似度贪心配对（阈值 0.62，低于视为删除/新增）
    paired_base = {i for i, _ in pairs}
    for i, base in enumerate(base_results):
        if i in paired_base:
            continue
        best_j, best_score = None, 0.62
        for j, target in enumerate(target_results):
            if j in used_target:
                continue
            score = _ratio(base.get("content"), target.get("content"))
            if score >= best_score:
                best_j, best_score = j, score
        if best_j is not None:
            pairs.append((i, best_j))
            used_target.add(best_j)

    changes: List[dict] = []
    counts = {"fixed": 0, "remaining": 0, "new": 0, "added": 0, "removed": 0,
              "unchanged": 0}
    for i, j in pairs:
        base, target = base_results[i], target_results[j]
        status = _status(base.get("risk_level"), target.get("risk_level"))
        counts[status] += 1
        if status != "unchanged":
            changes.append(_change(target, status, base, target, order=j))

    paired_base = {i for i, _ in pairs}
    for i, base in enumerate(base_results):
        if i in paired_base:
            continue
        counts["removed"] += 1
        changes.append(_change(base, "removed", base=base, order=i))
    for j, target in enumerate(target_results):
        if j in used_target:
            continue
        status = "new" if target.get("risk_level") in _RISKY else "added"
        counts[status] += 1
        changes.append(_change(target, status, target=target, order=j))

    changes.sort(key=lambda c: c["_order"])
    for change in changes:
        change.pop("_order", None)
    return {"summary": counts, "changes": changes[:200], "total_changes": len(changes)}
