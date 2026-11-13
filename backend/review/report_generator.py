# =============================================================================
# Word 审查报告生成 - report_generator.py
# =============================================================================
# 输出 .docx：概览（元数据+风险统计）→ 逐条详情（原文定位/风险描述/法条/建议）。
# 样式：高风险文字标红，中风险黄底高亮（PRD 3.2.4）。
# =============================================================================

from typing import List
import json
import difflib

from docx import Document as DocxDocument
from docx.enum.text import WD_COLOR_INDEX
from docx.shared import Pt, RGBColor

from config.settings import settings
from db import review_repository as repo

_RISK_COLOR = {"高": "FF0000", "中": "FF7F00", "低": "2E7D32", "无": "000000"}
_RISK_HIGHLIGHT = {"中": WD_COLOR_INDEX.YELLOW}


def _add_colored_run(paragraph, text: str, color: str = None,
                     highlight=None, bold: bool = False, size: int = None):
    """向段落追加一个 run 并设置颜色/高亮/字号——python-docx 的字体样式须逐 run 设置。"""
    run = paragraph.add_run(text)
    run.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    if highlight is not None:
        run.font.highlight_color = highlight
    if size:
        run.font.size = Pt(size)
    return run


def generate_report(task_id: str) -> str:
    """基于 DB 中的任务与逐条结果生成报告，返回保存路径。"""
    task = repo.get_task(task_id)
    if task is None:
        raise FileNotFoundError(f"任务不存在: {task_id}")
    results: List[dict] = repo.get_results(task_id)
    summary = repo.get_summary(task_id)

    doc = DocxDocument()
    doc.add_heading("合同智能审查报告", level=0)

    # ---- 概览 ----
    doc.add_heading("一、基本信息", level=1)
    playbook = task.get("playbook") or {}
    meta_rows = [
        ("合同名称", task.get("contract_name") or "-"),
        ("甲方", task.get("party_a") or "-"),
        ("乙方", task.get("party_b") or "-"),
        ("合同金额", task.get("total_amount") or "-"),
        ("审查条款数", str(task.get("total_clauses") or 0)),
        ("审查方案", task.get("playbook_name") or "未指定（按识别类型自动匹配）"),
        ("审查立场", playbook.get("stance") or "甲方"),
        ("数据安全", (f"送审前已脱敏 {task.get('masked_count')} 处"
                     if task.get("masked_count") else "未检测到需脱敏信息")),
        ("生成时间", task.get("finished_at") or task.get("created_at") or "-"),
    ]
    table = doc.add_table(rows=len(meta_rows), cols=2)
    table.style = "Table Grid"
    for i, (k, v) in enumerate(meta_rows):
        table.rows[i].cells[0].text = k
        table.rows[i].cells[1].text = str(v)

    doc.add_heading("二、风险概览", level=1)
    for level, count in [("高", summary.get("高", 0)), ("中", summary.get("中", 0)),
                         ("低", summary.get("低", 0)), ("无", summary.get("无", 0))]:
        p = doc.add_paragraph()
        _add_colored_run(p, f"■ {level}风险：{count} 条",
                         color=_RISK_COLOR.get(level, "000000"),
                         highlight=_RISK_HIGHLIGHT.get(level))

    # ---- 合同完整性检查（任务级：缺失条款/金额大小写/跨条款一致性）----
    doc.add_heading("三、合同完整性检查", level=1)
    ci = task.get("contract_issues") or {}
    ci_issues = ci.get("issues") or []
    doc.add_paragraph(f"识别合同类型：{ci.get('contract_type') or '通用合同'}")
    if not ci_issues:
        _add_colored_run(doc.add_paragraph(),
                         "未发现缺失条款或跨条款数值/主体不一致问题。", color="2E7D32")
    else:
        for q in ci_issues:
            line = doc.add_paragraph(style="List Bullet")
            _add_colored_run(
                line, f"[{q.get('type') or '其他'}·{q.get('severity') or '低'}] ",
                color="C62828" if q.get("severity") == "高" else "B26A00", size=10)
            line.add_run(q.get("issue") or "")
            if q.get("original"):
                line.add_run(f"（原文：{q['original']}）")
            if q.get("suggestion"):
                line.add_run(f" → {q['suggestion']}")

    # ---- 合同合理性评估（任务级：甲方利益视角的 0-100 分）----
    doc.add_heading("四、合同合理性评估", level=1)
    reason = task.get("reasonableness") or {}
    if reason:
        p_score = doc.add_paragraph()
        p_score.add_run("合理性评分：").bold = True
        score = reason.get("score")
        score_color = "2E7D32" if (score or 0) >= 85 else "E6A23C" if (score or 0) >= 70 else "FF0000"
        _add_colored_run(p_score, f"{score} / 100", color=score_color, bold=True, size=14)
        p_con = doc.add_paragraph()
        p_con.add_run("结论：").bold = True
        p_con.add_run(reason.get("conclusion") or "-")
        counts = reason.get("counts") or {}
        doc.add_paragraph(
            f"问题分布：高 {counts.get('高', 0)} 条 / 中 {counts.get('中', 0)} 条 / "
            f"低 {counts.get('低', 0)} 条（判断立场：{playbook.get('stance') or '甲方'}）")
    else:
        doc.add_paragraph("本次任务未生成合理性评估（历史任务或模型不可用）。")

    # ---- 逐条详情 ----
    doc.add_heading("五、逐条审查明细", level=1)
    for r in results:
        clause_id = r.get("clause_id") or "-"
        title = r.get("title") or ""
        page = r.get("page")
        heading_text = f"{clause_id} {title}".strip()
        p = doc.add_heading(level=2)
        _add_colored_run(p, heading_text, color="000000", bold=True, size=14)
        if page:
            doc.add_paragraph(f"原文位置：第 {page} 页（字符偏移 {r.get('char_index')}）")

        level = r.get("risk_level") or "无"
        p_risk = doc.add_paragraph()
        p_risk.add_run("风险等级：")
        _add_colored_run(p_risk, level, color=_RISK_COLOR.get(level),
                         highlight=_RISK_HIGHLIGHT.get(level), bold=True)

        p1 = doc.add_paragraph()
        p1.add_run("风险描述：").bold = True
        p1.add_run(r.get("risk_description") or "-")

        p2 = doc.add_paragraph()
        p2.add_run("法条依据：").bold = True
        p2.add_run(r.get("law_basis") or "（无引用）")

        p3 = doc.add_paragraph()
        p3.add_run("修改建议：").bold = True
        p3.add_run(r.get("suggestion") or "-")

        # 修订后的完整条款（模型改写），报告里可直接作为改稿参考
        if r.get("revised_text"):
            p_rev = doc.add_paragraph()
            p_rev.add_run("修订后条款：").bold = True
            _add_colored_run(p_rev, r["revised_text"], color="1F5FAF")

        # 文本质量问题（错别字/歧义/格式/字号）：与法律风险分开呈现，
        # 库中为 JSON 文本，坏数据静默跳过不影响报告生成
        raw_qi = r.get("quality_issues")
        try:
            qi_list = json.loads(raw_qi) if isinstance(raw_qi, str) else (raw_qi or [])
        except Exception:
            qi_list = []
        if isinstance(qi_list, list) and qi_list:
            p4 = doc.add_paragraph()
            p4.add_run("文本质量问题：").bold = True
            for q in qi_list:
                line = doc.add_paragraph(style="List Bullet")
                _add_colored_run(
                    line,
                    f"[{q.get('type') or '其他'}·{q.get('severity') or '低'}] ",
                    color="C62828", size=10)
                line.add_run(q.get("issue") or "")
                if q.get("original"):
                    line.add_run(f"（原文：{q['original']}）")
                if q.get("suggestion"):
                    line.add_run(f" → {q['suggestion']}")

        # 商业合理性提示（甲方视角）：与法律风险、文本质量分开展示
        raw_reason = r.get("reasonableness_issues")
        try:
            reason_list = json.loads(raw_reason) if isinstance(raw_reason, str) else (raw_reason or [])
        except Exception:
            reason_list = []
        if isinstance(reason_list, list) and reason_list:
            p5 = doc.add_paragraph()
            p5.add_run("合理性提示：").bold = True
            for q in reason_list:
                line = doc.add_paragraph(style="List Bullet")
                _add_colored_run(
                    line,
                    f"[{q.get('type') or '其他'}·{q.get('severity') or '低'}·"
                    f"{q.get('direction') or '对甲方不利'}] ",
                    color="B26A00", size=10)
                line.add_run(q.get("issue") or "")
                if q.get("original"):
                    line.add_run(f"（原文：{q['original']}）")
                if q.get("suggestion"):
                    line.add_run(f" → {q['suggestion']}")

        note = (f"判定通道：{'确定性规则' if r.get('channel') == 'rule' else '大模型审查'} · "
                f"置信度：{r.get('confidence') or 0:.2f}")
        _add_colored_run(doc.add_paragraph(), note, color="808080", size=9)

    path = settings.report_dir_path / f"{task_id}.docx"
    doc.save(str(path))
    return str(path)


def _add_diff_runs(paragraph, old: str, new: str) -> None:
    """把 old→new 的差异渲染成"红色删除线 + 蓝色下划线"的修订样式。

    python-docx 不直接支持 Word 修订痕迹（w:ins/w:del），这里用字符级 diff
    配合删除线/下划线模拟红line，导出后可直接看出增删改位置。
    """
    matcher = difflib.SequenceMatcher(None, old or "", new or "")
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            paragraph.add_run(old[i1:i2])
        elif tag == "delete":
            run = paragraph.add_run(old[i1:i2])
            run.font.strike = True
            run.font.color.rgb = RGBColor(0xC6, 0x28, 0x28)
        elif tag == "insert":
            run = paragraph.add_run(new[j1:j2])
            run.font.underline = True
            run.font.color.rgb = RGBColor(0x1F, 0x5F, 0xAF)
        else:  # replace：先删后增
            run = paragraph.add_run(old[i1:i2])
            run.font.strike = True
            run.font.color.rgb = RGBColor(0xC6, 0x28, 0x28)
            run2 = paragraph.add_run(new[j1:j2])
            run2.font.underline = True
            run2.font.color.rgb = RGBColor(0x1F, 0x5F, 0xAF)


def generate_revised_report(task_id: str) -> str:
    """生成"修订对照"Word：逐条展示原文、修订后文本（红删蓝增）与修改理由。"""
    task = repo.get_task(task_id)
    if task is None:
        raise FileNotFoundError(f"任务不存在: {task_id}")
    results = repo.get_results(task_id)

    doc = DocxDocument()
    doc.add_heading("合同修订对照报告", level=0)
    doc.add_paragraph(f"合同名称：{task.get('contract_name') or '-'}")
    doc.add_paragraph(f"甲方：{task.get('party_a') or '-'}    乙方：{task.get('party_b') or '-'}")
    doc.add_paragraph("图例：红色删除线为原文删除内容，蓝色下划线为新增或改写内容。")

    revised = [r for r in results if (r.get("revised_text") or "").strip()]
    if not revised:
        doc.add_paragraph("本次审查未生成修订文本（未发现需要改写的条款）。")
    for r in revised:
        heading = f"{r.get('clause_id') or ''} {r.get('title') or ''}".strip()
        p = doc.add_heading(level=2)
        _add_colored_run(p, heading or "条款", color="000000", bold=True, size=14)

        p1 = doc.add_paragraph()
        p1.add_run("原文：").bold = True
        _add_colored_run(p1, r.get("content") or "-", color="606060")

        p2 = doc.add_paragraph()
        p2.add_run("修订后：").bold = True
        _add_diff_runs(p2, r.get("content") or "", r.get("revised_text") or "")

        p3 = doc.add_paragraph()
        p3.add_run("修改理由：").bold = True
        reason = "；".join(x for x in [r.get("risk_description"), r.get("suggestion")] if x)
        p3.add_run(reason or "-")

    path = settings.report_dir_path / f"{task_id}_revised.docx"
    doc.save(str(path))
    return str(path)


def generate_action_list(task_id: str) -> str:
    """生成"整改处置清单"Word：逐条列风险、处置状态、备注与处置时间。"""
    task = repo.get_task(task_id)
    if task is None:
        raise FileNotFoundError(f"任务不存在: {task_id}")
    results = repo.get_results(task_id)

    doc = DocxDocument()
    doc.add_heading("整改处置清单", level=0)
    doc.add_paragraph(f"合同名称：{task.get('contract_name') or '-'}")

    actionable = [r for r in results
                  if (r.get("risk_level") or "无") != "无"
                  or (r.get("disposition") or "未处理") != "未处理"]
    counts: dict = {}
    for r in actionable:
        key = r.get("disposition") or "未处理"
        counts[key] = counts.get(key, 0) + 1
    doc.add_paragraph(
        f"共 {len(actionable)} 条处置事项：" +
        " / ".join(f"{k} {counts.get(k, 0)}" for k in
                   ["未处理", "已采纳", "已修改", "接受风险", "已忽略"]))

    if not actionable:
        doc.add_paragraph("全部条款均无需处置。")
        path = settings.report_dir_path / f"{task_id}_actions.docx"
        doc.save(str(path))
        return str(path)

    headers = ["序号", "条款", "风险", "问题摘要", "处置状态", "备注", "处置时间"]
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for i, h in enumerate(headers):
        table.rows[0].cells[i].text = h
    for idx, r in enumerate(actionable, 1):
        cells = table.add_row().cells
        cells[0].text = str(idx)
        cells[1].text = f"{r.get('clause_id') or ''} {r.get('title') or ''}".strip()
        cells[2].text = r.get("risk_level") or "无"
        cells[3].text = (r.get("risk_description") or "")[:100]
        cells[4].text = r.get("disposition") or "未处理"
        cells[5].text = r.get("disposition_note") or ""
        cells[6].text = r.get("disposition_at") or ""

    path = settings.report_dir_path / f"{task_id}_actions.docx"
    doc.save(str(path))
    return str(path)
