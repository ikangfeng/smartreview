# =============================================================================
# 改写一致性校验 - revision_checker.py
# =============================================================================
# 模型改写条款时最危险的行为是“顺手改数”：金额、比例、日期、期限被悄悄
# 调整后直接进入修订对照报告，法务很难逐字发现。本模块对比原条款与改写文本
# 中的关键数值，出现增删即生成“数值冲突”质量问题（不阻断，提示人工确认）。
# 只比对数字型事实，不评判措辞——措辞差异正是模型改写的价值所在。
# =============================================================================

import re
from typing import Dict, List, Set, Tuple

_AMOUNT_RE = re.compile(r"[¥￥]?\s*(\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|万|元)")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_DATE_RE = re.compile(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?")
_PERIOD_RE = re.compile(r"(\d+)\s*(个?工作日|个月|年|日|天)")

_UNIT_FACTOR = {"亿元": 1e8, "万元": 1e4, "万": 1e4, "元": 1.0}


def _canonical_amount(number: str, unit: str) -> str:
    try:
        value = float(number.replace(",", "")) * _UNIT_FACTOR.get(unit, 1.0)
    except ValueError:
        return number + unit
    return f"{value:.2f}"


def _collect(text: str) -> Dict[str, Dict[str, str]]:
    """抽取四类关键事实：canonical 值 → 展示原文（用于提示）。"""
    text = text or ""
    amounts: Dict[str, str] = {}
    for m in _AMOUNT_RE.finditer(text):
        amounts.setdefault(_canonical_amount(m.group(1), m.group(2)),
                           f"{m.group(1)}{m.group(2)}")
    percents: Dict[str, str] = {}
    for m in _PERCENT_RE.finditer(text):
        try:
            key = f"{float(m.group(1)):.2f}"
        except ValueError:
            key = m.group(1)
        percents.setdefault(key, f"{m.group(1)}%")
    dates: Dict[str, str] = {}
    for m in _DATE_RE.finditer(text):
        try:
            key = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except ValueError:
            continue
        dates.setdefault(key, f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日")
    periods: Dict[str, str] = {}
    for m in _PERIOD_RE.finditer(text):
        unit = m.group(2).replace("个", "").replace("天", "日").replace("个月", "月")
        periods.setdefault(f"{int(m.group(1))}{unit}", m.group(0).strip())
    return {"金额": amounts, "百分比": percents, "日期": dates, "期限": periods}


def _diff(original: Dict[str, str], revised: Dict[str, str]) -> Tuple[List[str], List[str]]:
    removed = [original[key] for key in original if key not in revised]
    added = [revised[key] for key in revised if key not in original]
    return removed[:3], added[:3]


def _is_split(original: Dict[str, str], revised: Dict[str, str],
              label: str, removed_keys: List[str], added_keys: List[str]) -> bool:
    """识别“拆分写法”而非真实改数：如 500 万拆成 454.55 万 + 45.45 万。

    仅在没有删除项时判断：新增金额之和等于仍保留的原金额，或新增百分比
    之和为 100%，视为模型把总额拆成明细，不提示（避免制造噪声）。
    """
    if removed_keys or not added_keys:
        return False
    try:
        added_values = [float(k) for k in added_keys]
    except ValueError:
        return False
    if label == "百分比":
        return abs(sum(added_values) - 100.0) < 0.01
    if label == "金额":
        unchanged = [float(k) for k in original if k in revised]
        return any(abs(sum(added_values) - value) < 0.01 for value in unchanged)
    return False


def check_revision_consistency(original: str, revised: str) -> List[dict]:
    """返回改写前后关键数值不一致的质量问题列表（最多 3 条）。"""
    if not revised or not revised.strip():
        return []
    old_facts, new_facts = _collect(original), _collect(revised)

    issues: List[dict] = []
    for label in ("金额", "百分比", "日期", "期限"):
        removed_keys = [key for key in old_facts[label] if key not in new_facts[label]]
        added_keys = [key for key in new_facts[label] if key not in old_facts[label]]
        if _is_split(old_facts[label], new_facts[label], label, removed_keys, added_keys):
            continue
        removed, added = _diff(old_facts[label], new_facts[label])
        if not removed and not added:
            continue
        parts = []
        if removed:
            parts.append("删除 " + "、".join(f"「{v}」" for v in removed))
        if added:
            parts.append("新增 " + "、".join(f"「{v}」" for v in added))
        issues.append({
            "type": "数值冲突",
            "original": "、".join(removed) if removed else "",
            "issue": f"改写文本的{label}与原文不一致：" + "；".join(parts),
            "suggestion": "请确认该变更为预期修复；若非预期，保留原文数值后再采用改写。",
            "severity": "中" if label in ("金额", "百分比", "日期") else "低",
        })
        if len(issues) >= 3:
            break
    return issues


# ---------------- 修订后全文一致性（交叉引用 / 编号） ----------------
# 条款级替换只改"那一段文字"，但合同是一个整体：改动可能在别处留下断链，
# 例如删掉第 6.3 条后，其他条款仍写着"按第 6.3 条处理"。这类问题模型看不出来，
# 确定性规则可以精确发现，因此作为修订产出前的最后一道校验。

_CLAUSE_HEAD_RE = re.compile(r"^\s*第\s*([一二三四五六七八九十百零〇\d]+)\s*条", re.M)
_CLAUSE_REF_RE = re.compile(r"第\s*([一二三四五六七八九十百零〇\d]+)\s*条")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100}


def _cn_to_int(raw: str) -> int | None:
    """把"十二/二十/一百零五"或阿拉伯数字转成 int；无法解析返回 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    total = 0
    number = 0
    for ch in raw:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            total += (number or 1) * _CN_UNITS[ch]
            number = 0
        else:
            return None
    return total + number


def check_revised_document(text: str) -> List[dict]:
    """修订后全文的编号与交叉引用校验（不阻断，提示人工确认）。

    检查三类问题：
      1. 条款编号重复（同一"第 X 条"出现多次）
      2. 条款编号断层（1、2、4 缺 3，仅提示，可能是刻意保留）
      3. 交叉引用悬空（正文引用"第 X 条"但全文不存在该条）
    """
    if not text or not text.strip():
        return []

    heads: dict[int, List[str]] = {}
    for m in _CLAUSE_HEAD_RE.finditer(text):
        num = _cn_to_int(m.group(1))
        if num:
            heads.setdefault(num, []).append(m.group(0).strip())

    issues: List[dict] = []

    for num, raws in sorted(heads.items()):
        if len(raws) > 1:
            issues.append({
                "type": "编号冲突",
                "original": raws[0],
                "issue": f"修订稿中「第{num}条」出现了 {len(raws)} 次，条款编号重复",
                "suggestion": "检查是否因替换/追加导致条款重复，保留一处并重新编号",
                "severity": "高",
            })

    nums = sorted(heads)
    if nums:
        missing = [n for n in range(nums[0], nums[-1] + 1) if n not in heads]
        if missing:
            shown = "、".join(f"第{n}条" for n in missing[:5])
            issues.append({
                "type": "编号断层",
                "original": shown,
                "issue": f"条款编号不连续，缺少 {shown}" + ("…" if len(missing) > 5 else ""),
                "suggestion": "确认是否为删除条款后的遗留编号，必要时重排编号并同步更新交叉引用",
                "severity": "低",
            })

    defined = set(heads)
    dangling: List[int] = []
    # 仅在合同确实使用"第 X 条"编号体系时才做引用校验，避免纯数字编号合同误报
    if defined:
        for m in _CLAUSE_REF_RE.finditer(text):
            num = _cn_to_int(m.group(1))
            if num and num not in defined and num not in dangling:
                dangling.append(num)
    if dangling:
        refs = "、".join(f"第{n}条" for n in dangling[:5])
        issues.append({
            "type": "引用悬空",
            "original": refs,
            "issue": f"正文引用了不存在的条款：{refs}" + ("…" if len(dangling) > 5 else ""),
            "suggestion": "修订可能删改了被引用条款，请补齐目标条款或同步修正引用",
            "severity": "高",
        })

    return issues[:5]
