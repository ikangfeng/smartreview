# =============================================================================
# 并发审查调度 - scheduler.py
# =============================================================================
# 逐条并行调用 LLM（DR-5）：ThreadPoolExecutor(workers) + 完成即落库 + 进度更新。
# 流程（架构文档 6.4）：
#   1. 规则通道先判（命中直接裁决，仍附检索法条）；
#   2. 未命中 → RAG 检索法条 → context_builder 组装 → LLM(JSON) → 容错解析；
#   3. 单条失败使用 fallback_verdict，不拖垮整单；
#   4. 全部完成 → 汇总 → 生成 Word 报告 → 置 completed。
# =============================================================================

import concurrent.futures as cf
import os

from config.settings import settings
from config.logger import get_logger, new_trace_id, Timer, set_trace_id
from db import review_repository as repo
from llm.client import llm_client
from rag.store import kb
from review.context_builder import (build_review_messages, build_quality_check_messages,
                                    retry_note)
from review.reasonableness_checker import (check_clause_reasonableness,
                                          merge_reasonableness, summarize_issues)
from review.law_basis_checker import annotate_law_basis
from review.injection_guard import scan as scan_injection
from review.fewshot import retrieve_examples, render_examples_block
from review.masking import TextMasker
from review.revision_checker import check_revision_consistency
from review.verdict_parser import (ParseError, fallback_verdict, parse_verdict,
                                   _clean_revised_text, _parse_quality_issues,
                                   _parse_reasonableness_issues)
from review.report_generator import generate_report
from rules import evaluate_clause_rules

_MAX_LLM_RETRIES = 2  # 解析失败后的额外重试次数

# 审查链路的业务日志器：所有埋点都带 trace_id，可整单回溯
log = get_logger("scheduler")


def _strip_cross_clause_issues(issues: list) -> list:
    """剔除 LLM 越权输出的跨条款结论（编号重复/断层、与其他条款冲突）。

    模型逐条审查时看不到全文，却常凭主观臆断输出"条款编号重复"这类结论
    （实测误报率高）。这类问题已由 format_checker 用确定性规则精确检查，
    故此处一律丢弃模型的相关输出，避免污染结果。
    """
    banned = ("编号重复", "编号断层", "编号不连续", "层级错乱",
              "与其他条款冲突", "条款编号")
    return [i for i in (issues or [])
            if not any(b in str(i.get("issue", "")) for b in banned)]


def _trim_revised_header(text: str, clause: dict) -> str:
    """去掉模型改写文本开头的"第X条 标题"重复行（正文区已有标题）。"""
    if not text:
        return ""
    parts = text.split("\n", 1)
    if len(parts) < 2:
        return text
    head = parts[0].strip()
    clause_id = str(clause.get("clause_id") or "")
    title = str(clause.get("title") or "")
    if (clause_id and clause_id in head) or (title and head == title) or \
            (title and head.endswith(title) and len(head) <= len(clause_id) + len(title) + 4):
        return parts[1].strip()
    return text


def _quality_check(clause: dict, risk_hint: str = "", plan_text: str = "",
                   stance: str = "甲方", trace_id: str = "",
                   variant: str = "baseline") -> dict:
    """规则已命中风险时，额外跑一次"文本质量 + 商业合理性 + 修订文本"体检。

    返回 {quality, reasonableness, revised_text, usage}。

    为什么规则命中后还要调 LLM：规则匹配器只会盯百分比/关键词，
    对错别字、语义歧义、术语不一致这类书写问题完全无感；若不补这一步，
    被规则命中的条款（本系统约 6 成）将永远得不到文本质量、合理性与修订文本。
    失败一律静默返回空结果——质量检查是增值项，绝不能拖垮规则结论。
    """
    empty = {"quality": [], "reasonableness": [], "revised_text": "", "usage": {}}
    try:
        messages = build_quality_check_messages(clause, risk_hint, plan_text, stance)
        result = llm_client.chat_json(messages, variant=variant)
        import json as _json
        raw = None
        try:
            raw = _json.loads(result.text[result.text.find("{"):result.text.rfind("}") + 1])
        except Exception:
            return {**empty, "usage": result.usage}
        return {
            "quality": _strip_cross_clause_issues(
                _parse_quality_issues(raw.get("quality_issues"))),
            "reasonableness": _parse_reasonableness_issues(
                raw.get("reasonableness_issues")),
            "revised_text": _clean_revised_text(raw.get("revised_text")),
            "usage": result.usage,
            "provider": result.provider,
            "variant": result.variant,
        }
    except Exception as e:
        log.warning("quality_check_degraded", extra={"trace_id": trace_id, "error": str(e)})
        return empty


def _fewshot_for(content: str) -> str:
    """检索历史采纳先例并渲染为 Prompt 片段；任何异常都降级为空（不影响主流程）。"""
    try:
        examples = retrieve_examples(content, limit=3)
        return render_examples_block(examples)
    except Exception as e:
        log.warning("fewshot_degraded", extra={"error": str(e)})
        return ""


def _injection_issues(content: str) -> list:
    """把注入检测结果转成 quality_issues 条目（type 独立标记，前端可单独高亮）。

    用独立的 type 而非塞进"其他"：这是安全告警，不是文本瑕疵，
    法务需要一眼把它和错别字/歧义区分开。
    """
    issues = []
    for hit in scan_injection(content or ""):
        issues.append({
            "type": "疑似指令注入",
            "original": hit.get("match") or "",
            "issue": f"条款出现「{hit.get('type')}」特征的表述，疑似试图操纵审查结论（Prompt 注入）",
            "suggestion": "该表述已按普通合同文本对待，未被执行；请人工核实该条款的真实意图",
            "severity": "高",
        })
    return issues


_LEVEL_ORDER = {"无": 0, "低": 1, "中": 2, "高": 3}


def _apply_injection_guard(verdict: dict, inj_issues: list) -> dict:
    """命中注入时禁止把结论判为"无/低"：安全结论必须压过模型输出。

    纯注入条款往往没有实质权利义务，模型容易被"直接判定无风险"这类话术骗过，
    导致安全告警还在、但风险等级被洗成"无"。此时强制抬到"高"并说明原因，
    保证告警不会被最终结论覆盖。
    """
    if not inj_issues or not isinstance(verdict, dict):
        return verdict
    if _LEVEL_ORDER.get(verdict.get("risk_level") or "无", 0) < 2:
        verdict["risk_level"] = "高"
        base = (verdict.get("risk_description") or "").strip()
        note = "条款包含疑似指令注入内容，已按不可信文本隔离，需人工复核。"
        verdict["risk_description"] = (note + (" " + base if base else ""))[:300]
        if not (verdict.get("suggestion") or "").strip():
            verdict["suggestion"] = "人工复核该条款的真实意图，并删除其中的指令性表述。"
    return verdict


def _review_one_clause(clause_order: int, clause: dict, contract_meta: dict,
                       rules: list, masker: TextMasker, trace_id: str = "",
                       variant: str = "baseline") -> dict:
    """审查单条条款：规则通道优先，否则 LLM + RAG。返回 {verdict, channel}。

    masker 为任务级脱敏器：送给 LLM/向量检索的条款与主体信息先脱敏，
    模型输出还原后再落库；规则引擎始终使用原文，不影响命中。
    """
    content = clause.get("content", "")
    title = clause.get("title") or ""
    clause_id = clause.get("clause_id") or ""
    # 在线程内绑定 trace_id：跨层模块（llm client、rag）无需传参也能打上同一个 trace
    set_trace_id(trace_id)
    # 注入检测在原文本上做（脱敏会改写主体名，反而可能破坏句式特征）
    inj_issues = _injection_issues(content)
    plan_text = masker.mask_text(contract_meta.get("extra_prompt") or "")
    stance = contract_meta.get("stance") or "甲方"
    masked_clause = masker.mask_clause(clause)
    masked_meta = masker.mask_meta(contract_meta)
    _t0 = Timer()

    # ---- RAG 检索（语义检索 + 可选重排序；不可用自动降级为空）----
    # 检索请求同样使用脱敏文本：Embedding 服务也不应看到当事人信息
    query = f"{masked_clause.get('clause_id')} {masked_clause.get('title') or ''} " \
            f"{(masked_clause.get('content') or '')[:200]}".strip()
    try:
        hits = kb.retrieve(query)
        # 审查方案可限定知识库范围（如采购合同只检索法律法规/司法解释/案例）；
        # 过滤后为空则保留原结果，避免方案配置过窄导致法条通道整体失效
        scope = contract_meta.get("kb_categories") or []
        if scope and hits:
            scoped = [h for h in hits
                      if (h.metadata or {}).get("category") in scope]
            if scoped:
                hits = scoped
        law_context = kb.format_context(hits)
    except Exception as e:
        log.warning("retrieval_degraded", extra={"trace_id": trace_id,
                                                 "clause_order": clause_order, "error": str(e)})
        hits, law_context = [], ""

    # ---- 通道一：确定性规则 ----
    rule_verdict = evaluate_clause_rules(content, rules) if content else None
    if rule_verdict:
        # 附上检索到的最相关法条原文，保证结论可追溯
        if hits:
            rule_verdict["law_basis"] = hits[0].page_content[:200]
        # 规则只判法律风险；文本质量、商业合理性与修订文本另跑一次轻量检查
        extra = _quality_check(masked_clause, masker.mask_text(
            rule_verdict.get("risk_description") or ""), plan_text, stance, trace_id,
            variant=variant)
        extra = masker.restore(extra)  # 模型输出中的占位符还原为真实主体
        rule_verdict["quality_issues"] = extra["quality"] + list(
            clause.get("_format_issues") or []) + inj_issues
        # 确定性合理性规则兜底 + 模型语义判断（同类型同问题只留一条）
        rule_verdict["reasonableness_issues"] = merge_reasonableness(
            check_clause_reasonableness(content, stance), extra["reasonableness"], stance)
        rule_verdict["revised_text"] = _trim_revised_header(extra["revised_text"], clause)
        # 改写一致性：模型可能在改写里擅改金额/比例/日期，必须提示人工确认
        rule_verdict["quality_issues"].extend(
            check_revision_consistency(content, rule_verdict["revised_text"]))
        _apply_injection_guard(rule_verdict, inj_issues)
        log.info("clause_done", extra={
            "trace_id": trace_id, "clause_order": clause_order, "clause_id": clause_id,
            "channel": "rule", "provider": extra.get("provider") or "",
            "risk_level": rule_verdict.get("risk_level"),
            "latency_ms": _t0.ms,
            "total_tokens": (extra.get("usage") or {}).get("total_tokens", 0),
            "injections": len(inj_issues),
        })
        return {"verdict": rule_verdict, "channel": "rule",
                "usage": extra.get("usage") or {},
                "provider": extra.get("provider") or "",
                "variant": extra.get("variant") or "baseline"}

    # ---- 通道二：LLM 审查 ----
    model_clause = dict(masked_clause)
    if len(content) > settings.max_clause_chars:
        model_clause["content"] = (masked_clause.get("content") or "")[: settings.max_clause_chars] + "\n（条款过长，以上为截断内容）"
    messages = build_review_messages(masked_meta, model_clause, law_context, rules,
                                     fewshot_block=_fewshot_for(content))

    last_error = ""
    for _ in range(_MAX_LLM_RETRIES + 1):
        try:
            result = llm_client.chat_json(messages, variant=variant)
            verdict = masker.restore(parse_verdict(result.text))
            model_law = verdict.get("law_basis") or ""
            if not model_law and hits:
                # 模型未给依据时直接回填 RAG 原文：这是检索材料本身，天然可信，
                # 不走白名单校验（否则法条摘录会被误标“待核验”）
                verdict["law_basis"] = hits[0].page_content[:200]
            else:
                # 法条引用校验：模型可能编造不存在的法条——不在白名单且未被 RAG
                # 检索材料佐证的引用，统一标注“待核验”，避免报告把幻觉当依据
                verdict["law_basis"] = annotate_law_basis(model_law, law_context)
            # 合并确定性格式问题（编号/标点/字号），与模型给出的质量问题并列；
            # 模型越权输出的跨条款结论先被剔除，避免与确定性检查重复/矛盾
            fmt = list(clause.get("_format_issues") or []) + inj_issues
            verdict["quality_issues"] = (_strip_cross_clause_issues(
                verdict.get("quality_issues")) + fmt)
            # 合理性：模型语义判断 + 确定性兜底（防模型漏掉明显失衡条款）
            verdict["reasonableness_issues"] = merge_reasonableness(
                check_clause_reasonableness(content, stance),
                verdict.get("reasonableness_issues"), stance)
            verdict["revised_text"] = _trim_revised_header(
                verdict.get("revised_text") or "", clause)
            # 改写一致性：标记模型在改写中擅自变更的金额/比例/日期/期限
            verdict["quality_issues"].extend(
                check_revision_consistency(content, verdict["revised_text"]))
            _apply_injection_guard(verdict, inj_issues)
            log.info("clause_done", extra={
                "trace_id": trace_id, "clause_order": clause_order, "clause_id": clause_id,
                "channel": "llm", "provider": result.provider,
                "risk_level": verdict.get("risk_level"),
                "latency_ms": _t0.ms,
                "total_tokens": (result.usage or {}).get("total_tokens", 0),
                "injections": len(inj_issues),
            })
            return {"verdict": verdict, "channel": "llm",
                    "usage": result.usage, "provider": result.provider,
                    "variant": result.variant}
        except ParseError as e:
            last_error = f"JSON 解析失败: {e}"
            log.warning("parse_failed", extra={
                "trace_id": trace_id, "clause_order": clause_order,
                "clause_id": clause_id, "error": str(e)})
            messages = messages + [{"role": "user", "content": retry_note()}]
        except Exception as e:  # 网络/Key/超时等
            last_error = str(e)
            break  # 提供商级故障：直接走兜底，避免多次无谓重试
    # 模型不可用时仍保留确定性合理性结论，不让这部分能力随 LLM 一起降级
    fallback = fallback_verdict(last_error)
    fallback["reasonableness_issues"] = check_clause_reasonableness(content, stance)
    # 模型不可用也不能丢掉注入告警：安全结论优先于可用性降级
    fallback["quality_issues"] = inj_issues
    _apply_injection_guard(fallback, inj_issues)
    log.error("clause_fallback", extra={
        "trace_id": trace_id, "clause_order": clause_order, "clause_id": clause_id,
        "channel": "fallback", "latency_ms": _t0.ms, "error": last_error[:300]})
    return {"verdict": fallback, "channel": "fallback"}


def _summarize_reasonableness(task_id: str) -> dict:
    """汇总全部条款的合理性问题，产出合同级评分与结论（按方案审查立场）。"""
    import json as _json
    stance = ((repo.get_task(task_id) or {}).get("playbook") or {}).get("stance") or "甲方"
    issues = []
    for row in repo.get_results(task_id):
        raw = row.get("reasonableness_issues")
        if not raw:
            continue
        try:
            value = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if isinstance(value, list):
            issues.extend(value)
    return summarize_issues(issues, stance)


def _cleanup_upload(task_id: str) -> None:
    """审查成功后清理临时上传文件（PRD 7.2）。"""
    task = repo.get_task(task_id)
    if not task:
        return
    file_id = task.get("file_id")
    if not file_id:
        return
    for name in os.listdir(settings.upload_dir_path):
        if name.startswith(file_id):
            try:
                os.remove(settings.upload_dir_path / name)
            except OSError:
                pass


def run_review_task_blocking(task_id: str) -> None:
    """后台执行整单审查（在独立线程中运行，供 asyncio.to_thread 包装）。"""
    task = repo.get_task(task_id)
    if task is None:
        return
    clauses = task.get("clauses") or []
    # 审查方案快照：规则子集 + 立场 + 附加提示词 + 知识库范围（任务创建时已冻结）
    playbook = task.get("playbook") or {}
    contract_meta = {
        "contract_name": task.get("contract_name"),
        "party_a": task.get("party_a"),
        "party_b": task.get("party_b"),
        "total_amount": task.get("total_amount"),
        "plan_name": task.get("playbook_name") or playbook.get("name"),
        "stance": playbook.get("stance") or "甲方",
        "extra_prompt": playbook.get("extra_prompt") or "",
        "kb_categories": playbook.get("kb_categories") or [],
    }
    rules = repo.get_enabled_rules()
    # 方案指定了规则子集时只跑这些规则；空列表（通用方案）= 全部启用规则
    rule_ids = playbook.get("rule_ids") or []
    if rule_ids:
        allowed = {int(x) for x in rule_ids}
        rules = [r for r in rules if r["id"] in allowed]
    repo.set_task_running(task_id)

    # trace_id 直接复用 task_id：一次审查 = 一条 trace，前端/日志/DB 三处可互相跳转
    trace_id = task_id
    # 灰度分流：按任务哈希稳定决定整单走 canary 还是 baseline
    variant = llm_client.pick_variant(task_id)
    task_timer = Timer()
    log.info("task_started", extra={
        "trace_id": trace_id, "total_clauses": len(clauses),
        "stance": contract_meta.get("stance"),
        "playbook": contract_meta.get("plan_name"), "rules": len(rules),
        "workers": settings.workers, "variant": variant})

    try:
        completed = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        channel_counts: dict[str, int] = {}
        used_variants: set = set()  # 实际生效的灰度变体（用于区分 canary / mixed / baseline）
        # 任务级脱敏器：同一主体在整份合同内保持同一占位符，便于输出还原
        masker = TextMasker(contract_meta)
        with cf.ThreadPoolExecutor(max_workers=max(1, settings.workers)) as executor:
            futures = {}
            for order, clause in enumerate(clauses, start=1):
                future = executor.submit(_review_one_clause, order, clause,
                                         contract_meta, rules, masker, trace_id,
                                         variant=variant)
                futures[future] = (order, clause)
            for future in cf.as_completed(futures):
                order, clause = futures[future]
                try:
                    outcome = future.result()
                except Exception as e:
                    outcome = {"verdict": fallback_verdict(str(e)[:100]), "channel": "fallback"}
                    log.error("clause_exception", extra={
                        "trace_id": trace_id, "clause_order": order, "error": str(e)[:300]})
                usage = outcome.get("usage") or {}
                # 落库用"实际变体"而非"计划变体"：canary 失败会回落基线，
                # 若按计划变体落库，A/B 指标会把基线结果错记到 canary 头上
                used_variant = outcome.get("variant") or "baseline"
                repo.save_result(task_id, order, clause,
                                 outcome["verdict"], outcome.get("channel", "llm"),
                                 usage=usage, provider=outcome.get("provider"),
                                 variant=used_variant)
                used_variants.add(used_variant)
                ch = outcome.get("channel", "llm")
                channel_counts[ch] = channel_counts.get(ch, 0) + 1
                total_prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
                total_completion_tokens += int(usage.get("completion_tokens", 0) or 0)
                completed += 1
                repo.update_progress(task_id, completed)

        summary = repo.get_summary(task_id)
        repo.save_reasonableness(task_id, _summarize_reasonableness(task_id))
        # 记录本次送审脱敏的敏感信息处数（前端展示“已脱敏 N 处”）
        repo.set_masked_count(task_id, masker.count)
        # 记录本次审查的 token 消耗（成本核算：报告页展示“本次消耗 X tokens ≈ ¥Y”）
        repo.set_token_usage(task_id, total_prompt_tokens, total_completion_tokens)
        # 灰度变体落库：canary 全量命中记 canary，混合（部分回落）记 mixed
        if used_variants == {"canary"}:
            task_variant = "canary"
        elif "canary" in used_variants:
            task_variant = "mixed"
        else:
            task_variant = "baseline"
        repo.set_task_variant(task_id, task_variant)
        report_path = generate_report(task_id)
        repo.finish_task(task_id, "completed", summary, report_path=report_path)
        _cleanup_upload(task_id)
        log.info("task_completed", extra={
            "trace_id": trace_id, "latency_ms": task_timer.ms,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "channels": channel_counts, "clauses": completed,
            "masked": masker.count,
            "high": (summary or {}).get("高", 0)})
    except Exception as e:
        log.error("task_failed", extra={
            "trace_id": trace_id, "latency_ms": task_timer.ms, "error": str(e)[:500]})
        try:
            repo.finish_task(task_id, "failed", repo.get_summary(task_id), error=str(e))
        except Exception:
            pass
