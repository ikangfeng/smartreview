# =============================================================================
# 合同格式检查 - format_checker.py
# =============================================================================
# 为什么格式检查不走大模型：字号是否统一、编号是否断层、全半角是否混用，
# 都是确定性的字符/属性比对——用代码判断又快又准且零幻觉；交给 LLM 既浪费
# token 又不可靠。本模块产出与 LLM 的 quality_issues 完全同构的条目，
# 便于上层统一合并展示。
# =============================================================================

import re
from collections import Counter
from typing import List, Optional

# 中文 + 半角标点的错误搭配（中文正文应使用全角标点）
_CJK_HALF_PUNCT = re.compile(r"[\u4e00-\u9fff][,;:!?](?!\d)")
# 重复标点（如"。。"、"，，"）
_DUP_PUNCT = re.compile(r"([。，、；：！？])\1+")
# 中文字符之间的多余空格（排版噪声）
_CJK_SPACE = re.compile(r"([\u4e00-\u9fff])[ \t]+([\u4e00-\u9fff])")
# 中文引号与英文引号混用
_MIXED_QUOTE = re.compile(r"[\"'][^\"']{1,30}[\"']")


def _issue(qtype: str, original: str, issue: str, suggestion: str, severity: str = "低") -> dict:
    """构造与 LLM quality_issues 同构的条目。"""
    return {"type": qtype, "original": original[:60], "issue": issue,
            "suggestion": suggestion, "severity": severity}


def check_text_format(content: str) -> List[dict]:
    """检查单条条款的文本层格式问题（不依赖文件类型，txt/docx/pdf 均适用）。"""
    issues: List[dict] = []
    if not content:
        return issues

    m = _CJK_HALF_PUNCT.search(content)
    if m:
        issues.append(_issue("格式异常", m.group(0),
                             "中文语句中混用半角标点",
                             "中文正文应统一使用全角标点（，。；：！？）"))

    m = _DUP_PUNCT.search(content)
    if m:
        issues.append(_issue("格式异常", m.group(0),
                             "出现重复标点", "删除多余标点，保留一个"))

    m = _CJK_SPACE.search(content)
    if m:
        issues.append(_issue("格式异常", m.group(0),
                             "中文字符之间有多余空格",
                             "删除中文字符间的空格"))

    # 中英文引号混用：同一段里同时出现 " 与 “”
    if ('"' in content or "'" in content) and ("“" in content or "”" in content):
        issues.append(_issue("格式异常", "中英文引号混用",
                             "同一段落中同时使用了英文直引号与中文弯引号",
                             "统一改为中文引号“”"))

    return issues


def check_numbering(clauses: List[dict]) -> List[dict]:
    """检查条款编号连续性：章节号与子条款号断层都会被点名。

    例如存在 1.1 / 1.2 / 1.4 而缺 1.3，或章节出现 一、二、四、缺三。
    """
    issues: List[dict] = []

    # 子条款编号（1.1 / 2.3）连续性：按章分组后逐组检查
    groups: dict = {}
    for c in clauses:
        cid = str(c.get("clause_id") or "")
        m = re.match(r"^(\d+)\.(\d+)$", cid)
        if m:
            groups.setdefault(m.group(1), []).append(int(m.group(2)))

    for chapter, nums in groups.items():
        nums.sort()
        for prev, cur in zip(nums, nums[1:]):
            if cur - prev > 1:
                missing = "、".join(f"{chapter}.{n}" for n in range(prev + 1, cur))
                issues.append(_issue(
                    "格式异常", f"{chapter}.{prev} 之后直接到 {chapter}.{cur}",
                    f"条款编号断层，缺少 {missing}",
                    "检查是否漏排条款，或重新连续编号", "中"))

    # 章节编号（一、二、三）连续性
    cn_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    chapters = []
    for c in clauses:
        ch = c.get("chapter") or ""
        m = re.match(r"^([一二三四五六七八九十]+)、", ch)
        if m and m.group(1) in cn_map:
            chapters.append(cn_map[m.group(1)])
    if chapters:
        uniq = sorted(set(chapters))
        for prev, cur in zip(uniq, uniq[1:]):
            if cur - prev > 1:
                issues.append(_issue(
                    "格式异常", f"章节编号 {prev} 之后直接跳到 {cur}",
                    "章节编号不连续，疑似缺少章节",
                    "检查是否漏排章节", "中"))

    return issues


def check_docx_format(file_path: str) -> List[dict]:
    """检查 DOCX 的字号/字体一致性（仅 .docx 有效，其他格式返回空）。

    判定口径：以出现次数最多的字号为"正文字号"，若存在占比不足 8% 的
    零散字号且与正文相差 ≥1pt，视为排版不统一；正文字号小于 10pt 时
    提示打印可读性风险。
    """
    if not str(file_path).lower().endswith(".docx"):
        return []
    try:
        from docx import Document as DocxDocument
    except ImportError:
        return []

    issues: List[dict] = []
    try:
        doc = DocxDocument(file_path)
    except Exception:
        return []

    size_counter: Counter = Counter()
    total_runs = 0
    font_names: Counter = Counter()
    for para in doc.paragraphs:
        if not para.text.strip():
            continue
        for run in para.runs:
            if not run.text.strip():
                continue
            total_runs += 1
            size = run.font.size
            if size is not None:
                size_counter[round(size.pt, 1)] += 1
            if run.font.name:
                font_names[run.font.name] += 1

    if total_runs == 0 or not size_counter:
        return issues

    body_size, body_count = size_counter.most_common(1)[0]
    # 正文字号过小
    if body_size < 10:
        issues.append(_issue("格式异常", f"正文字号 {body_size}pt",
                             "正文字号偏小，打印或阅读时可能不易辨认",
                             "建议正文不小于小四（12pt）", "中"))

    # 零散字号（占比 <8% 且与正文差 ≥1pt）
    for size, count in size_counter.items():
        ratio = count / total_runs
        if ratio < 0.08 and abs(size - body_size) >= 1:
            issues.append(_issue("格式异常", f"字号 {size}pt（{count} 处）",
                                 f"与正文字号 {body_size}pt 不一致，疑似排版遗漏",
                                 "统一调整为正文字号，仅标题可放大"))

    # 字体混用（中文字体应统一）
    if len(font_names) >= 3:
        top = "、".join(name for name, _ in font_names.most_common(3))
        issues.append(_issue("格式异常", f"字体种类 {len(font_names)} 种",
                             f"字体使用不统一（{top}）",
                             "正文统一使用一种中文字体", "低"))

    return issues


def attach_format_issues(clauses: List[dict], file_path: Optional[str] = None,
                         full_text: Optional[str] = None) -> None:
    """把格式检查结果挂到各条款上（原地修改 clauses，加 _format_issues 键）。

    - 文本级问题：逐条作用于对应 clause；
    - 编号连续性问题：挂到第 1 条（属于全文性问题，避免重复刷屏）；
    - DOCX 字号问题：挂到第 1 条。
    """
    for c in clauses:
        try:
            c["_format_issues"] = check_text_format(c.get("content") or "")
        except Exception:
            c["_format_issues"] = []

    global_issues: List[dict] = []
    try:
        global_issues.extend(check_numbering(clauses))
    except Exception:
        pass
    if file_path:
        try:
            global_issues.extend(check_docx_format(file_path))
        except Exception:
            pass
    if global_issues and clauses:
        clauses[0]["_format_issues"] = clauses[0].get("_format_issues", []) + global_issues
