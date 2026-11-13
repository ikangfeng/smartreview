# =============================================================================
# SmartReview 主应用 - FastAPI 入口（法律合同智能审查后端）
# =============================================================================
# 路由全景（统一不带 /api 前缀，与 PRD 表六 / zhiqida 风格一致）：
#
#   [上传/审查]  POST   /upload                   上传合同（扩展名白名单/大小校验+解析预检）
#                POST   /review                   建审查任务落库 → 后台并发审查 → 返回 task_id
#                GET    /tasks/{task_id}/status   进度轮询（前端 2s，逐条增量出结果）
#                GET    /tasks/{task_id}/results  结果页完整明细（含法条依据/修改建议）
#                POST   /tasks/{task_id}/clauses/draft  缺失条款一键生成草案
#                GET    /tasks/{task_id}/compare/{other} 合同版本对比复审（旧→新）
#                GET    /tasks/{task_id}/report   下载 Word 审查报告
#
#   [历史]       GET    /history                  任务列表（分页，按创建时间倒序）
#                DELETE /history/{task_id}        删除记录 + 级联清理报告与上传残留文件
#
#   [审查方案]   GET    /playbooks                 方案列表（含禁用项）
#                GET    /playbooks/{id}            方案详情
#                POST   /playbooks                 新增方案
#                PUT    /playbooks/{id}            全量更新方案
#                DELETE /playbooks/{id}            删除方案（历史任务持快照不受影响）
#
#   [风控规则]   GET    /rules                    列出规则（含禁用项）
#                POST   /rules                    新增规则（三类匹配器白名单校验）
#                PUT    /rules/{id}               局部更新（PATCH 语义）
#                DELETE /rules/{id}               删除（seed 一次性标记，重启不复活）
#
#   [法律知识库] GET    /knowledge/stats          统计（chunk 总量/来源分布）
#                POST   /knowledge/index          重建索引（md5 稳定 ID 幂等增量）
#                DELETE /knowledge                清空向量索引（不动 data/kb 磁盘语料）
#                GET    /knowledge/documents      文档列表（按 category+source 聚合）
#                POST   /knowledge/documents      上传语料（落盘 uploads/ → 解析 → 入库）
#                GET    /knowledge/document       指定文档的分块详情
#                DELETE /knowledge/document       删除文档（chunk + 源文件）
#                POST   /knowledge/search         语义检索测试
#
#   [LLM 配置]   GET    /llm/config               读取运行时自定义 LLM（api_key 明文回显）
#                PUT    /llm/config               保存并立即注入生效（DB 优先，覆盖 .env 默认链）
#                POST   /llm/test                 连通性探测（不落库）
#
#   [其他]       GET    /health                   健康检查（LLM/Reranker/KB 就绪度）
#                GET    /                         服务信息（/docs 入口）
#
# 核心执行模型：审查任务"请求即返回 + asyncio 后台线程执行"——
#   POST /review → 任务落库 → scheduler.run_review_task_blocking 经 asyncio.to_thread
#   在线程池执行（逐条审查、完成即落库）→ 前端轮询 /status。
#   后台任务句柄保存在模块级 _bg_tasks 集合防 GC 回收；进程中断遗留的
#   running/pending 任务在 lifespan 启动时统一复位为 failed（状态机收敛）。
# =============================================================================

import asyncio
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config.settings import settings
from config.logger import get_logger, read_trace
from db import audit_repository as audit
from db import model_config_repository as mrepo
from db import playbook_repository as pbrepo
from db import review_repository as repo
from db.database import init_db
from llm.client import llm_client
from llm.registry import apply_canary_to_client, apply_custom_to_client, probe_llm
from rag.store import KB_CATEGORIES, kb
from review.completeness_checker import (CONTRACT_TYPES, analyze_contract,
                                         detect_contract_type)
from review.context_builder import (build_quality_check_messages, build_missing_clause_messages,
                                    build_review_messages)
from review.masking import TextMasker
from rules import evaluate_clause_rules
from review.contract_splitter import SUPPORTED_EXTS, parse_contract
from review.format_checker import attach_format_issues
from review.metadata_extractor import extract_metadata
from review.report_generator import generate_action_list, generate_revised_report
from review.revision_checker import check_revised_document
from review.scheduler import run_review_task_blocking
from review.version_compare import compare_results

# 后台任务句柄集合：asyncio 不会强引用 create_task 产生的 Task，
# 若不保存引用，任务可能在执行途中被 GC 回收、整单审查静默消失；
# add_done_callback(discard) 在任务结束（含异常）后自动移除句柄，防止集合无限增长。
_bg_tasks: set = set()


def _actor(request: Request) -> str:
    """审计操作人：演示环境用 X-Operator 头（前端本地记住的姓名），缺省"演示用户"。

    为什么不接登录：本 Demo 刻意不展示账号体系；用请求头既能让"谁改了什么"
    可演示，又不会把审计能力绑死在未做的权限模块上。

    中文姓名必须 URI 编码：HTTP 头只允许 latin-1，前端 encodeURIComponent，
    这里 unquote 还原（未编码的英文名也兼容）。
    """
    from urllib.parse import unquote
    raw = request.headers.get("x-operator") or ""
    return unquote(raw).strip()[:40] or audit.DEFAULT_ACTOR


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子：启动时完成目录就绪、建表+幂等 seed、复位中断任务、注入自定义 LLM
    （各步"为什么"见下方行内注释）。"""
    # 确保 uploads/reports/kb/chroma/db 目录就绪，否则后续落盘/建库直接失败
    settings.ensure_dirs()
    # 结构化日志就绪：控制台可读 + logs/app.jsonl 可查（trace_id 贯穿整单审查）
    from config.logger import setup_logging
    setup_logging()
    # 幂等初始化：CREATE TABLE IF NOT EXISTS 建表 + 首次 seed 默认风控规则。
    # seed 以 app_meta['rules_seeded'] 做"一次性标记"：只灌一次，用户手动删除的
    # 内置规则不会因表空而在下次重启被重新插入（"删除后不复活"的关键）。
    init_db()
    # 复位上次进程中断遗留的运行中任务（smartbrief 惯例）：
    # 若服务在审查中途崩溃/被杀，任务 status 会停在 pending/running，
    # 前端将永远轮询不到终态；启动时统一置 failed 让状态机收敛。
    repo.mark_interrupted_as_failed()
    # 把 DB 中 llm_profile（模型配置页保存的自定义 OpenAI 兼容端点）注入全局
    # llm_client：DB 优先，排在 .env 默认链（vllm→siliconflow）之前生效。
    apply_custom_to_client()
    # 灰度配置同样从 DB 注入：重启后保持上次的分流比例与 canary 端点
    try:
        apply_canary_to_client()
    except Exception as e:
        get_logger("startup").warning("canary_apply_failed", extra={"error": str(e)[:200]})
    # 启动即预热 provider 探测：让"第一个坏端点"的失败成本只付一次，
    # 而不是让首批并发审查的每个线程各付一次（实测可省 7 次 502 等待）
    try:
        llm_client.active_info()
    except Exception as e:
        get_logger("startup").warning("llm_probe_failed", extra={"error": str(e)[:200]})
    yield


app = FastAPI(
    title="SmartReview 法律合同智能审查系统",
    description="企业法律合同智能审查：上传 → 拆解 → RAG 法条检索 → 双通道风险判断 → Word 报告",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 内网部署；生产可收敛为白名单
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- 请求/响应模型 ----------------
class ReviewRequest(BaseModel):
    """发起一次审查：携带已上传文件的 file_id，可选指定审查方案。

    contract_type/stance 来自上传后的人工确认（可覆盖自动识别结果），
    用于避免“类型猜错→必备条款清单错”“立场错→规则与建议系统性偏向对方”。
    """

    file_id: str
    playbook_id: Optional[int] = None  # 空 = 按确认/识别出的合同类型自动匹配默认方案
    contract_type: Optional[str] = None  # 用户确认的合同类型（空 = 自动识别）
    stance: Optional[str] = None  # 用户确认的审查立场（空 = 取方案默认，最终回退甲方）


class PlaybookPayload(BaseModel):
    """审查方案创建/更新载荷（编辑弹窗提交完整对象，PUT 做全量替换）。"""

    name: str
    contract_type: Optional[str] = None
    stance: str = "甲方"
    description: Optional[str] = None
    required_clauses: Optional[List[dict]] = None
    rule_ids: Optional[List[int]] = None
    kb_categories: Optional[List[str]] = None
    extra_prompt: Optional[str] = None
    is_default: bool = False
    enabled: bool = True


class DispositionRequest(BaseModel):
    """单条问题的处置状态更新：状态白名单见 DISPOSITION_CHOICES。"""

    disposition: str
    note: Optional[str] = None
    custom_revised_text: Optional[str] = None  # 用户手动修改的改写稿（"已修改"时）


class DraftClauseRequest(BaseModel):
    """缺失条款草案生成：contract_issues 中的下标。"""

    index: int


class PromptPreviewRequest(BaseModel):
    """Prompt 预览：指定 clause_order 重建审查时发给 LLM 的完整 messages。

    返回两条通道的 Prompt（规则命中时的 quality_check Prompt + LLM 审查 Prompt），
    供前端 Debug 面板直接展示 messages JSON。
    """

    clause_order: int


class ClauseAskRequest(BaseModel):
    """条款追问：基于已审条款上下文 + 用户问题，再调一次 LLM 给出即时回答。"""

    question: str


# ---------------- Prompt 预览 + 对话式追问（面试 Demo 亮点）----------------
@app.post("/tasks/{task_id}/prompt-preview")
async def prompt_preview(task_id: str, req: PromptPreviewRequest):
    """重建指定条款在审查时的完整 Prompt（双通道），供前端 Debug 面板展示。

    返回：
      - rule_channel: 若该条款命中规则引擎，给出 quality_check 通道的 messages
      - llm_channel: 发给 LLM 主审查通道的 messages（含法条检索结果）
      - law_context: RAG 检索到的法条原文段
      - rules: 本次审查实际生效的企业风控规则列表（name + risk_level）
      - stance: 审查立场
      - clause: 原始条款内容
    """
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "审查任务不存在")

    clauses = task.get("clauses") or []
    if not (1 <= req.clause_order <= len(clauses)):
        raise HTTPException(404, f"条款序号超出范围 (1-{len(clauses)})")

    clause = clauses[req.clause_order - 1]
    playbook = task.get("playbook") or {}
    stance = playbook.get("stance") or "甲方"
    contract_meta = {
        "contract_name": task.get("contract_name"),
        "party_a": task.get("party_a"),
        "party_b": task.get("party_b"),
        "total_amount": task.get("total_amount"),
        "plan_name": task.get("playbook_name") or playbook.get("name"),
        "stance": stance,
        "extra_prompt": playbook.get("extra_prompt") or "",
        "kb_categories": playbook.get("kb_categories") or [],
    }

    # 生效规则（与 scheduler 同逻辑）
    rules = repo.get_enabled_rules()
    rule_ids = playbook.get("rule_ids") or []
    if rule_ids:
        allowed = {int(x) for x in rule_ids}
        rules = [r for r in rules if r["id"] in allowed]

    # RAG 检索（同样本逻辑，不脱敏——这是面试 demo）
    law_context = ""
    try:
        query = f"{clause.get('clause_id')} {clause.get('title') or ''} {(clause.get('content') or '')[:200]}"
        hits = kb.retrieve(query)
        scope = contract_meta.get("kb_categories") or []
        if scope and hits:
            scoped = [h for h in hits if (h.metadata or {}).get("category") in scope]
            if scoped:
                hits = scoped
        law_context = kb.format_context(hits)
    except Exception:
        hits = []

    # 规则命中判定（决定走哪条通道）
    rule_hit = evaluate_clause_rules(clause.get("content", ""), rules)
    rule_channel = None
    if rule_hit:
        rule_channel = build_quality_check_messages(
            clause,
            rule_hit.get("risk_description") or "",
            contract_meta.get("extra_prompt") or "",
            stance,
        )

    llm_channel = build_review_messages(contract_meta, clause, law_context, rules)

    rules_summary = [
        {"name": r["name"], "risk_level": r["risk_level"], "description": r.get("description", "")}
        for r in rules
    ]

    return {
        "clause_order": req.clause_order,
        "clause_id": clause.get("clause_id"),
        "clause_content": clause.get("content"),
        "channel_hit": "rule" if rule_hit else "llm",
        "stance": stance,
        "law_context": law_context,
        "rules": rules_summary,
        "rule_channel": rule_channel,
        "llm_channel": llm_channel,
    }


@app.post("/tasks/{task_id}/clauses/{clause_order}/ask")
async def clause_ask(task_id: str, clause_order: int, req: ClauseAskRequest):
    """对话式追问：基于指定条款的审查结果 + 全文上下文 + 用户问题，即时回答。

    前端在结果页每条条款旁加一个 💬 按钮 → mini chat → 调这个接口。
    """
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "审查任务不存在")

    clauses = task.get("clauses") or []
    if not (1 <= clause_order <= len(clauses)):
        raise HTTPException(404, f"条款序号超出范围 (1-{len(clauses)})")

    clause = clauses[clause_order - 1]
    playbook = task.get("playbook") or {}
    stance = playbook.get("stance") or "甲方"

    # 取已审结果（已落库的）作为上下文
    existing_result = None
    for r in repo.get_results(task_id):
        if r.get("clause_order") == clause_order:
            existing_result = r
            break

    system_prompt = (
        "你是企业法务顾问，正在协助审查一份合同条款。"
        "基于以下审查上下文，回答用户的追问。"
        "要求：\n"
        "1. 回答要具体、可执行，不要空泛的法律条文堆砌；\n"
        "2. 金额、比例、期限等关键数值给出企业常用参考值；\n"
        "3. 若涉及法律引用，只引用已知法律名称即可，不要编造具体条号；\n"
        "4. 可以给出改写建议，但标注为'建议稿'，法务需确认后采用。"
    )

    ctx_parts = [
        f"【合同】{task.get('contract_name') or ''}",
        f"【立场】{stance}",
        f"【条款编号】{clause.get('clause_id')}",
        f"【条款原文】\n{clause.get('content')}",
    ]
    if existing_result:
        ctx_parts.append(f"【此前审查结论】风险等级={existing_result.get('risk_level')} | {existing_result.get('risk_description') or ''}")
        if existing_result.get("suggestion"):
            ctx_parts.append(f"【原审查建议】{existing_result.get('suggestion')}")
        if existing_result.get("revised_text"):
            ctx_parts.append(f"【AI 改写稿】{existing_result.get('revised_text')}")

    # 少量相关法条作为参考
    law_ctx = ""
    try:
        hits = kb.retrieve(f"{clause.get('clause_id')} {clause.get('title') or ''} {(clause.get('content') or '')[:150]}")
        if hits:
            law_ctx = kb.format_context(hits)[:800]
    except Exception:
        pass
    if law_ctx:
        ctx_parts.append(f"【相关法条参考】\n{law_ctx}")

    ctx_parts.append(f"【用户问题】{req.question.strip()}")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(ctx_parts)},
    ]

    answer = ""
    provider = ""
    try:
        result = llm_client.chat(messages)
        answer = (result.text or "").strip()
        provider = result.provider or ""
    except Exception as e:
        answer = f"LLM 调用失败：{e}"

    return {
        "clause_order": clause_order,
        "question": req.question.strip(),
        "answer": answer,
        "provider": provider,
    }


# 处置状态机：未处理 → 已采纳/已修改/接受风险/已忽略，可回退为未处理
DISPOSITION_CHOICES = {"未处理", "已采纳", "已修改", "接受风险", "已忽略"}


class RulePatch(BaseModel):
    """规则局部更新载荷：全部字段可选，只改传入的字段（PATCH 语义）。"""

    name: Optional[str] = None
    category: Optional[str] = None
    risk_level: Optional[str] = None
    description: Optional[str] = None
    suggestion: Optional[str] = None
    enabled: Optional[bool] = None


class RuleCreate(BaseModel):
    """新增规则载荷：matcher_kind 决定用哪个匹配器解读 params。"""

    name: str
    category: Optional[str] = None
    matcher_kind: str  # percent_ceiling / ip_rights / keyword_any
    params: Optional[dict] = None
    risk_level: str = "中"
    description: Optional[str] = None
    suggestion: Optional[str] = None
    enabled: bool = True


class KnowledgeSearchRequest(BaseModel):
    """知识库检索请求：query 为检索原文，k 为召回条数，category 可收窄范围。"""

    query: str
    k: int = 5
    category: Optional[str] = None


class LlmConfigRequest(BaseModel):
    """运行时自定义 LLM 配置（OpenAI 兼容端点）。api_key 留空 = 保留已存 Key。"""
    enabled: bool = True
    base_url: str
    model: str
    api_key: Optional[str] = None


class LlmProbeRequest(BaseModel):
    """连通性测试载荷（仅测试，不落库）。"""
    base_url: str
    model: str
    api_key: Optional[str] = None


class CanaryConfigRequest(BaseModel):
    """灰度发布配置：按百分比把新任务分流到 canary 端点。"""
    enabled: bool = False
    percent: int = 0
    base_url: str = ""
    model: str = ""
    api_key: Optional[str] = None


# ---------------- 工具函数 ----------------
def _resolve_upload_path(file_id: str) -> Optional[Path]:
    """按 file_id 前缀在 uploads 目录反查已上传文件（找不到返回 None）。

    落盘文件名 = file_id + 原始扩展名（见 upload_file），调用方无从预知扩展名，
    只能列出目录做前缀匹配。前缀匹配还有一层安全收益：file_id 仅参与 startswith
    字符串比较、从不拼进文件路径，天然规避路径穿越注入。
    """
    if not file_id:
        return None
    for name in os.listdir(settings.upload_dir_path):
        # file_id 为 12 位随机 hex，目录内前缀唯一，首个命中即目标
        if name.startswith(file_id):
            return settings.upload_dir_path / name
    return None


async def _schedule_review(task_id: str) -> None:
    """把整单审查编排为后台 asyncio 任务，经线程池执行。

    审查链路（scheduler.run_review_task_blocking）是同步阻塞的：逐条调 LLM
    （网络 IO，默认 60s 超时）且逐条落库。若直接放进事件循环，会长时间卡住
    全部 API（/status 轮询也会饿死），故用 asyncio.to_thread 挪到工作线程。
    """
    bg = asyncio.create_task(asyncio.to_thread(run_review_task_blocking, task_id))
    _bg_tasks.add(bg)  # 持有强引用防 GC（见 _bg_tasks 集合定义处的说明）
    bg.add_done_callback(_bg_tasks.discard)  # 任务结束/异常后自动释放句柄


# ---------------- 上传 ----------------
@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传合同（仅 PDF/DOCX/TXT，≤20MB）。

    流程：扩展名白名单校验 → 读取内容 → 大小校验 → 落盘为 {file_id}{ext}
    → 试解析（预检）→ 返回 file_id/页数/预估条款数。
    错误码：400 类型不支持 / 413 超限 / 422 解析失败（落盘文件已清理）。
    """
    filename = file.filename or "unnamed"
    ext = os.path.splitext(filename)[1].lower()
    # 先按扩展名白名单拦截：不支持的格式即便落盘也无法 parse_contract，
    # 提前报 400 比把无法解析的文件写进 uploads 目录更干净
    if ext not in SUPPORTED_EXTS:
        raise HTTPException(400, f"仅支持 PDF/DOCX/TXT 文件（收到: {ext or '无扩展名'}）")

    content = await file.read()
    max_bytes = settings.upload_max_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(413, f"文件超过 {settings.upload_max_mb}MB 限制")

    # 12 位随机 hex 作 file_id：足够短以保持 URL/文件名友好，随机性保证并发
    # 上传不冲突；真实扩展名拼在其后，删除/回查时靠前缀匹配（见 _resolve_upload_path）
    file_id = uuid.uuid4().hex[:12]
    dest = settings.upload_dir_path / f"{file_id}{ext}"
    dest.write_bytes(content)

    # 上传即做一次解析预检：把"文件损坏/不可解析"的失败提前暴露给用户，
    # 而不是拖到 POST /review 才报错；失败时清理落盘文件，不留孤儿垃圾
    try:
        # 扫描件会走 OCR（约 1-2s/页）：放到线程池执行，避免阻塞事件循环
        # 导致上传期间整个服务无响应
        parsed = await asyncio.to_thread(parse_contract, str(dest))
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"文件解析失败：{e}")

    return {
        "file_id": file_id,
        "filename": filename,
        "page_count": parsed["page_count"],
        "estimate_clauses": len(parsed["clauses"]),
        # 类型与 OCR 信息供上传页做“审查前确认”，避免类型猜错导致必备条款清单错
        "detected_contract_type": detect_contract_type(parsed["full_text"]),
        "contract_types": CONTRACT_TYPES,
        "ocr_pages": parsed.get("ocr_pages", 0),
    }


# ---------------- 审查 ----------------
@app.post("/review")
async def start_review(req: ReviewRequest, request: Request):
    """创建审查任务并立即返回：解析合同 → 提取元信息 → 条款全量落库 → 后台并发审查。

    请求体：file_id（来自 /upload）。响应：task_id + 条款总数，前端随即轮询
    /tasks/{task_id}/status 直至终态。错误码：404 上传文件缺失 / 422 解析失败
    或文档中无可审查条款。
    """
    path = _resolve_upload_path(req.file_id)
    if path is None:
        raise HTTPException(404, "上传文件不存在或已被清理，请重新上传")

    # /upload 虽已预检，但两次请求之间文件可能被清理/替换，此处必须重新解析。
    # 拿到的条款全量随任务落库（clauses_json 列），后台线程与报告生成都读
    # 同一份数据，保证单次任务内解析结果自洽
    try:
        parsed = await asyncio.to_thread(parse_contract, str(path))
    except Exception as e:
        raise HTTPException(422, f"合同解析失败：{e}")
    if not parsed["clauses"]:
        raise HTTPException(422, "未能从文档中提取任何条款，请检查文档内容")

    # 确定性格式体检（编号断层/标点混用/DOCX 字号）：结果挂到各条款
    # 的 _format_issues，审查时与模型输出的 quality_issues 合并展示
    attach_format_issues(parsed["clauses"], str(path), parsed["full_text"])

    meta = extract_metadata(parsed["full_text"], path.name)
    # 审查前人工确认优先：合同类型决定必备条款口径，立场决定规则/Prompt 的偏向
    detected_type = detect_contract_type(parsed["full_text"])
    if req.contract_type and req.contract_type not in CONTRACT_TYPES:
        raise HTTPException(422, f"未知合同类型：{req.contract_type}")
    if req.stance and req.stance not in pbrepo.STANCES:
        raise HTTPException(422, f"stance 仅支持 {'/'.join(pbrepo.STANCES)}")
    contract_type = req.contract_type or detected_type
    playbook = pbrepo.resolve_playbook(req.playbook_id, contract_type)
    if playbook is None:
        # 一条方案都没有时仍保留人工确认口径，避免立场/类型选择被静默丢弃
        playbook = {
            "id": None,
            "name": f"{contract_type} · {req.stance or '甲方'}（临时口径）",
            "contract_type": contract_type,
            "stance": req.stance or "甲方",
            "description": "未匹配到审查方案，按类型内置清单与全部启用规则执行",
            "required_clauses": [],
            "rule_ids": [],
            "kb_categories": [],
            "extra_prompt": "",
        }
    else:
        playbook = dict(playbook)  # 复制后再覆盖，避免改动仓储返回对象
        orig_type, orig_stance = playbook.get("contract_type"), playbook.get("stance")
        playbook["contract_type"] = contract_type
        if req.stance:
            playbook["stance"] = req.stance
        # 人工确认与方案默认口径不一致时，重写展示名，避免结果页出现
        # “甲方审查方案 + 乙方立场”这种自相矛盾的标签
        if contract_type != orig_type or (req.stance and req.stance != orig_stance):
            playbook["name"] = f"{contract_type} · {playbook['stance']}审查口径"
    # 整份合同完整性体检（纯代码、零 LLM token）：必备条款缺失、金额大小写、
    # 跨条款金额/期限/主体一致性。必备条款清单优先取方案配置（企业口径），
    # 方案未配置时回退内置类型清单。结果按任务级落库，与逐条审查互为补充
    completeness = analyze_contract(
        parsed["full_text"], parsed["clauses"], meta,
        required_clauses=playbook.get("required_clauses"))
    # 展示口径以人工确认为准（analyze_contract 内部还会再识别一次）
    completeness["contract_type"] = contract_type
    task_id = uuid.uuid4().hex
    # 先落库再调度：DB 行是任务的唯一事实来源——即便调度后进程立刻崩溃，
    # 该 pending 任务也会在下次启动被 mark_interrupted_as_failed 复位为 failed
    repo.create_task(
        task_id=task_id,
        file_id=req.file_id,
        filename=Path(path).name,
        contract_name=meta["contract_name"],
        party_a=meta["party_a"],
        party_b=meta["party_b"],
        total_amount=meta["total_amount"],
        clauses=parsed["clauses"],
        contract_issues=completeness,
        full_text=parsed["full_text"],
        playbook=playbook,
    )
    # 此处必须 await：_schedule_review 是 async 函数，不 await 只会拿到一个
    # 尚未启动的协程对象并被丢弃，其函数体（含 create_task）永远不会执行
    await _schedule_review(task_id)
    audit.log("task_created", "task", task_id, task_id, actor=_actor(request),
              detail={"contract": meta["contract_name"] or Path(path).name,
                      "file_id": req.file_id, "contract_type": contract_type,
                      "stance": req.stance or (playbook or {}).get("stance") or "甲方",
                      "clauses": len(parsed["clauses"]),
                      "playbook": (playbook or {}).get("name") or ""})
    return {"task_id": task_id, "total_clauses": len(parsed["clauses"]),
            "playbook_id": (playbook or {}).get("id"),
            "playbook_name": (playbook or {}).get("name")}


@app.get("/tasks/{task_id}/status")
async def get_status(task_id: str):
    """查询任务进度与逐条已出结果（前端 2s 轮询数据源）。

    业务流：后台线程逐条审查、完成一条落库一条（见 scheduler），故轮询可拿到
    增量 findings 做实时展示；summary 在任务完成前可能为空，前端据此展示进度条。
    错误码：404 任务不存在。
    """
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "审查任务不存在")
    results = repo.get_results(task_id)
    findings = [
        {
            "clause_id": r.get("clause_id"),
            "title": r.get("title"),
            "risk_level": r.get("risk_level"),
            "description": r.get("risk_description"),
            "channel": r.get("channel"),
            "confidence": r.get("confidence"),
        }
        for r in results
    ]
    return {
        "task_id": task_id,
        "status": task["status"],
        "current": task["current"],
        "total": task["total_clauses"],
        "error": task.get("error"),
        "summary": task.get("summary") or repo.get_summary(task_id),
        "findings": findings,
    }


def _token_usage_payload(task: dict) -> dict:
    """组装 token 用量与估算成本，供结果页展示"本次审查花了多少钱"。

    成本 = 输入 token × 输入单价 + 输出 token × 输出单价（单价见 settings）。
    规则通道不调 LLM，其 token 恒为 0——这正是"双通道省钱"的量化体现。
    """
    p = int(task.get("total_prompt_tokens") or 0)
    c = int(task.get("total_completion_tokens") or 0)
    cost = (p / 1000.0) * settings.llm_price_input_per_1k \
        + (c / 1000.0) * settings.llm_price_output_per_1k
    return {
        "prompt_tokens": p,
        "completion_tokens": c,
        "total_tokens": p + c,
        "estimated_cost_cny": round(cost, 4),
    }


@app.get("/tasks/{task_id}/trace")
async def get_task_trace(task_id: str, limit: int = Query(500, ge=1, le=2000)):
    """回溯一次审查的结构化日志（trace_id 即 task_id）。

    用途：解释"这次为什么慢 / 哪条降级了 / 走了哪个通道与模型"，
    数据直接来自 logs/app.jsonl，与审查链路的实时埋点同源，无二次加工。
    """
    if repo.get_task(task_id) is None:
        raise HTTPException(404, "审查任务不存在")
    events = read_trace(task_id, limit=limit)
    # 按事件类型聚合成一眼可读的摘要，前端可直接展示
    summary = {"total_events": len(events), "fallbacks": 0, "parse_failed": 0,
               "provider_failures": 0, "slowest_clause": None}
    slowest = 0.0
    for e in events:
        ev = e.get("event")
        if ev == "clause_fallback":
            summary["fallbacks"] += 1
        elif ev == "parse_failed":
            summary["parse_failed"] += 1
        elif ev == "provider_failed":
            summary["provider_failures"] += 1
        if ev == "clause_done":
            ms = float(e.get("latency_ms") or 0)
            if ms > slowest:
                slowest = ms
                summary["slowest_clause"] = {
                    "clause_id": e.get("clause_id"),
                    "clause_order": e.get("clause_order"),
                    "latency_ms": ms,
                    "channel": e.get("channel"),
                    "provider": e.get("provider"),
                }
    return {"trace_id": task_id, "summary": summary, "events": events}


@app.get("/tasks/{task_id}/results")
async def get_task_results(task_id: str):
    """结果页明细：任务元信息 + 风险汇总 + 逐条完整审查结果（含法条/建议）。"""
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "审查任务不存在")
    rows = repo.get_results(task_id)
    import json as _json

    def _qi(raw):
        """quality_issues 在库中是 JSON 文本，此处还原为数组；脏数据降级为空。"""
        if not raw:
            return []
        try:
            val = _json.loads(raw) if isinstance(raw, str) else raw
            return val if isinstance(val, list) else []
        except Exception:
            return []

    items = [
        {
            "clause_order": r.get("clause_order"),
            "clause_id": r.get("clause_id"),
            "title": r.get("title"),
            "content": r.get("content"),
            "char_index": r.get("char_index"),
            "risk_level": r.get("risk_level"),
            "description": r.get("risk_description"),
            "law_basis": r.get("law_basis"),
            "suggestion": r.get("suggestion"),
            "confidence": r.get("confidence"),
            "channel": r.get("channel"),
            "page": r.get("page"),
            "quality_issues": _qi(r.get("quality_issues")),
            "reasonableness_issues": _qi(r.get("reasonableness_issues")),
            "revised_text": r.get("revised_text") or "",
            "disposition": r.get("disposition") or "未处理",
            "disposition_note": r.get("disposition_note") or "",
            # 条款级成本：前端可逐条展示"这条花了多少 token / 哪个模型审的"
            "prompt_tokens": r.get("prompt_tokens") or 0,
            "completion_tokens": r.get("completion_tokens") or 0,
            "llm_provider": r.get("llm_provider") or "",
        }
        for r in rows
    ]
    disposition_summary: dict = {}
    for r in rows:
        key = r.get("disposition") or "未处理"
        disposition_summary[key] = disposition_summary.get(key, 0) + 1
    return {
        "task_id": task_id,
        "status": task["status"],
        "filename": task.get("filename"),
        "contract_name": task.get("contract_name"),
        "party_a": task.get("party_a"),
        "party_b": task.get("party_b"),
        "total_amount": task.get("total_amount"),
        "total_clauses": task.get("total_clauses"),
        "error": task.get("error"),
        "summary": task.get("summary") or repo.get_summary(task_id),
        "contract_type": (task.get("contract_issues") or {}).get("contract_type"),
        "contract_issues": (task.get("contract_issues") or {}).get("issues") or [],
        "reasonableness": task.get("reasonableness") or {},
        "playbook_name": task.get("playbook_name"),
        "playbook": task.get("playbook") or {},
        "masked_count": task.get("masked_count") or 0,
        "token_usage": _token_usage_payload(task),
        "llm_variant": task.get("llm_variant") or "baseline",
        "full_text": task.get("full_text") or "",
        "disposition_summary": disposition_summary,
        "items": items,
    }


@app.put("/tasks/{task_id}/results/{clause_order}/disposition")
async def set_result_disposition(task_id: str, clause_order: int,
                                 req: DispositionRequest, request: Request):
    """更新单条结果的处置状态与备注（处置闭环：采纳/修改/接受风险/忽略）。"""
    if repo.get_task(task_id) is None:
        raise HTTPException(404, "审查任务不存在")
    if req.disposition not in DISPOSITION_CHOICES:
        raise HTTPException(400, f"无效的处置状态：{req.disposition}")
    old = repo.get_result_disposition(task_id, clause_order)
    if not repo.update_disposition(task_id, clause_order, req.disposition, req.note, req.custom_revised_text):
        raise HTTPException(404, "审查结果不存在")
    audit.log("disposition_updated", "task", task_id, task_id, actor=_actor(request),
              detail={"clause_order": clause_order, "from": old, "to": req.disposition,
                      "note": req.note or "",
                      "custom_revised": bool((req.custom_revised_text or "").strip())})
    return {"status": "ok", "clause_order": clause_order, "note": req.note or ""}


# ---------------- 修订合同合并（建议→采纳→产出闭环）----------------
class ApplyRevisionsRequest(BaseModel):
    """把已采纳的模型改写合并进合同原文 → 输出修订后合同 + 可选择直接发起复审。

    接受两种模式：
      1. 按处置状态自动合并 disposition='已采纳' 的条款（auto=True，默认）
      2. 手动指定 clause_orders 列表，覆盖处置状态

    还支持 append_drafts：把完整性草案直接 append 到合同末尾。
    """
    auto: bool = True  # True=自动合并所有"已采纳"的条款
    clause_orders: Optional[List[int]] = None  # 手动指定
    revise_party_a: Optional[str] = None  # 可一并修改甲方名称（合同抬头）
    revise_party_b: Optional[str] = None  # 可一并修改乙方名称
    revise_total_amount: Optional[str] = None  # 可一并修改合同金额
    start_new_review: bool = False  # 是否直接用修订后合同发起复审
    append_drafts: Optional[List[str]] = None  # 完整性草案文本数组，append 到合同末尾


def _merge_adopted_revisions(task: dict, rows: List[dict], adopt_orders: List[int]) -> dict:
    """把指定 clauses 的 revised_text 替换进 full_text，产出修订后合同。

    替换策略：按 char_index 定位条款在 full_text 中的原文，用 revised_text 覆盖。
    若某条款没有 char_index（历史任务）或 revised_text 为空，则用原文。
    同时支持替换合同抬头的甲方/乙方/金额（元信息层面）。
    """
    import json as _json

    full_text = task.get("full_text") or ""
    results = task.get("results") or rows

    # 建立 clause_order → row 的映射
    row_map: dict = {}
    for r in results:
        o = r.get("clause_order")
        if o is not None:
            row_map[o] = r

    # 按 char_index 排序，从后向前替换避免索引偏移
    sorted_adopt = []
    for order in adopt_orders:
        r = row_map.get(order)
        if not r:
            continue
        # 优先用用户手动修改的改写稿（"已修改"时），否则用模型原始改写
        revised = (r.get("custom_revised_text") or "").strip() or (r.get("revised_text") or "").strip()
        if not revised:
            continue  # 空改写不替换
        char_idx = r.get("char_index")
        orig_content = r.get("content") or ""
        sorted_adopt.append((order, char_idx, orig_content, revised))

    if not sorted_adopt:
        return {"revised_text": full_text, "replacements": 0, "applied": []}

    # 策略 A：有 char_index → 按位置替换（最精准）
    by_index = [(o, ci, orig, rev) for (o, ci, orig, rev) in sorted_adopt if ci is not None and ci >= 0]
    # 策略 B：无 char_index → 用 content 字符串在 full_text 中搜索替换
    by_content = [(o, ci, orig, rev) for (o, ci, orig, rev) in sorted_adopt if ci is None or ci < 0]

    applied = []
    text = full_text

    # 先按位置替换（从后往前）
    by_index.sort(key=lambda x: x[1], reverse=True)
    for order, ci, orig, rev in by_index:
        # 从 ci 位置找原文内容边界：用 orig_content 做 sanity check
        # 如果 orig_content 和 text[ci:ci+len(orig)] 对不上，退化为搜索替换
        snippet_len = max(len(orig), 20)
        actual = text[ci:ci + snippet_len]
        if orig and orig[:20] in actual[:40]:
            # 精准替换：找到 orig 在 text[ci:] 的实际结束位置
            end = text.find(orig, ci)
            if end >= 0:
                text = text[:end] + rev + text[end + len(orig):]
                applied.append(order)
                continue
        # 退化为全文搜索
        if orig and orig in text:
            text = text.replace(orig, rev, 1)
            applied.append(order)

    # 再处理 by_content（全文搜索）
    for order, ci, orig, rev in by_content:
        if orig and orig in text:
            text = text.replace(orig, rev, 1)
            applied.append(order)

    return {"revised_text": text, "replacements": len(applied), "applied": applied}


@app.post("/tasks/{task_id}/apply-revisions")
async def apply_revisions(task_id: str, req: ApplyRevisionsRequest, request: Request):
    """把已采纳的改写合并成完整修订后合同。

    返回：
      - 修订后合同全文 (revised_text)
      - 实际替换了多少条款 (replacements)
      - 哪些条款被采纳合并 (applied)
      - 合同信息（甲乙方/金额）：若用户传入了 revise_party_a 等则一并替换展示
      - 可选：若 start_new_review=true，直接上传修订文本、发起复审，返回 new_task_id
    """
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "审查任务不存在")
    rows = repo.get_results(task_id)

    # 确定要合并的条款序号
    if req.auto:
        adopt_orders = [
            r["clause_order"] for r in rows
            if r.get("clause_order") is not None
            and (r.get("disposition") or "") in ("已采纳", "已修改")
            and (r.get("revised_text") or "").strip()
        ]
    else:
        adopt_orders = [o for o in (req.clause_orders or []) if o is not None]

    if not adopt_orders:
        raise HTTPException(422, "没有可合并的改写条款。请先在处置状态中标记'已采纳'或'已修改'，或手动指定条款序号。")

    merge_result = _merge_adopted_revisions(task, rows, adopt_orders)
    revised_text = merge_result["revised_text"]

    # 完整性草案回写：append 到合同末尾（面试 Demo 亮点）
    appended_drafts = 0
    if req.append_drafts:
        for draft_text in req.append_drafts:
            if draft_text and draft_text.strip():
                revised_text = revised_text.rstrip() + "\n\n" + draft_text.strip()
                appended_drafts += 1

    # 元信息层面的甲乙方/金额替换（抬头处）
    updated_meta = {
        "party_a": req.revise_party_a or task.get("party_a"),
        "party_b": req.revise_party_b or task.get("party_b"),
        "total_amount": req.revise_total_amount or task.get("total_amount"),
    }
    for key, old_val in [
        ("party_a", task.get("party_a") or ""),
        ("party_b", task.get("party_b") or ""),
        ("total_amount", task.get("total_amount") or ""),
    ]:
        new_val = updated_meta[key]
        if old_val and new_val and new_val != old_val:
            revised_text = revised_text.replace(old_val, new_val)

    response: dict = {
        "task_id": task_id,
        "revised_text": revised_text,
        "replacements": merge_result["replacements"],
        "applied_clause_orders": merge_result["applied"],
        "appended_drafts": appended_drafts,
        "meta": updated_meta,
        # 修订后全文一致性：编号重复/断层 + 交叉引用悬空（确定性校验，不阻断）
        "consistency_issues": check_revised_document(revised_text),
    }

    # 可选：直接用修订后合同发起复审
    if req.start_new_review:
        # 写入临时文件 → 模拟上传 → 发起审查
        import uuid as _uuid
        file_id = _uuid.uuid4().hex[:12]
        dest = settings.upload_dir_path / f"{file_id}_revised.txt"
        dest.write_text(revised_text, encoding="utf-8")
        # 直接调 /review 的逻辑（避免 HTTP 自调用）
        try:
            parsed = await asyncio.to_thread(parse_contract, str(dest))
        except Exception as e:
            dest.unlink(missing_ok=True)
            raise HTTPException(422, f"修订后合同解析失败：{e}")
        playbook = task.get("playbook") or {}
        contract_type = (task.get("contract_issues") or {}).get("contract_type") or playbook.get("contract_type")
        stance = playbook.get("stance") or "甲方"
        completeness = analyze_contract(
            parsed["full_text"], parsed["clauses"],
            {"contract_name": task.get("contract_name"),
             "party_a": updated_meta["party_a"], "party_b": updated_meta["party_b"],
             "total_amount": updated_meta["total_amount"]},
            required_clauses=playbook.get("required_clauses"))
        new_task_id = _uuid.uuid4().hex
        # 版本链：本次修订产物是上一版的子版本，root 指向合同族首版
        root_task_id = task.get("root_task_id") or task_id
        new_version = int(task.get("version") or 1) + 1
        repo.create_task(
            task_id=new_task_id,
            file_id=file_id,
            filename=f"{task.get('filename') or '合同'}_修订版.txt",
            contract_name=task.get("contract_name"),
            party_a=updated_meta["party_a"],
            party_b=updated_meta["party_b"],
            total_amount=updated_meta["total_amount"],
            clauses=parsed["clauses"],
            contract_issues=completeness,
            full_text=parsed["full_text"],
            playbook=playbook,
            root_task_id=root_task_id,
            parent_task_id=task_id,
            version=new_version,
        )
        asyncio.create_task(_schedule_review(new_task_id))
        response["new_task_id"] = new_task_id
        response["new_total_clauses"] = len(parsed["clauses"])
        response["note"] = "复审任务已创建，可在历史记录中查看或立即跳转版本对比"
    else:
        # 不发起复审时清理临时文件
        try:
            for f in settings.upload_dir_path.glob(f"{file_id}_*"):
                f.unlink()
        except Exception:
            pass

    audit.log("revision_applied", "task", task_id, task_id, actor=_actor(request),
              detail={"clause_orders": merge_result["applied"],
                      "appended_drafts": appended_drafts,
                      "consistency_issues": len(response["consistency_issues"]),
                      "new_task_id": response.get("new_task_id") or "",
                      "version": int(task.get("version") or 1) + (1 if req.start_new_review else 0)})
    return response


@app.post("/tasks/{task_id}/clauses/draft")
async def draft_missing_clause(task_id: str, req: DraftClauseRequest):
    """为一条“缺失条款”生成可并入合同的草案（LLM 起草，失败降级为模板）。"""
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "审查任务不存在")
    issues = (task.get("contract_issues") or {}).get("issues") or []
    if req.index < 0 or req.index >= len(issues):
        raise HTTPException(404, "完整性问题不存在")
    item = issues[req.index]
    if item.get("type") != "缺失条款":
        raise HTTPException(422, "仅缺失条款支持生成草案")

    issue_text = item.get("issue") or ""
    name = ""
    matched = re.search(r"「(.+?)」", issue_text)
    if matched:
        name = matched.group(1)
    name = name or "补充条款"

    playbook = task.get("playbook") or {}
    stance = playbook.get("stance") or "甲方"
    meta = {
        "contract_name": task.get("contract_name"),
        "contract_type": (task.get("contract_issues") or {}).get("contract_type"),
        "party_a": task.get("party_a"),
        "party_b": task.get("party_b"),
        "total_amount": task.get("total_amount"),
    }
    law_context = ""
    try:
        hits = kb.retrieve(f"{meta['contract_type'] or ''} {name} {item.get('suggestion') or ''}")
        law_context = kb.format_context(hits)
    except Exception:
        pass

    source = "template"
    draft = ""
    try:
        result = llm_client.chat(build_missing_clause_messages(
            meta, name, item.get("suggestion") or "", law_context, stance))
        draft = (result.text or "").strip()
        if draft:
            source = "llm"
    except Exception as e:
        # 只记日志、不中断：下面会用规则拼一段兜底草案，保证前端始终有内容
        get_logger("draft").warning("draft_degraded", extra={"error": str(e)})

    if not draft:
        suggestion = item.get("suggestion") or f"双方应就{name}事项作出明确约定。"
        draft = (f"【{name}】\n{suggestion}\n"
                 f"（以上为系统生成草案，具体表述与条款编号需法务确认后并入合同。）")
    return {"name": name, "draft": draft, "source": source,
            "suggestion": item.get("suggestion") or ""}


@app.get("/tasks/{task_id}/compare/{other_task_id}")
async def compare_task_versions(task_id: str, other_task_id: str):
    """合同版本对比复审：task_id=旧版本（基线），other_task_id=新版本。"""
    base = repo.get_task(task_id)
    target = repo.get_task(other_task_id)
    if base is None or target is None:
        raise HTTPException(404, "对比任务不存在")
    result = compare_results(repo.get_results(task_id), repo.get_results(other_task_id))

    def _brief(task: dict) -> dict:
        return {
            "task_id": task.get("task_id"),
            "contract_name": task.get("contract_name") or task.get("filename"),
            "created_at": task.get("created_at"),
            "playbook_name": task.get("playbook_name"),
            "summary": task.get("summary") or {},
            "status": task.get("status"),
        }

    return {"base": _brief(base), "target": _brief(target), **result}


@app.get("/tasks/{task_id}/report")
async def download_report(task_id: str, request: Request):
    """下载 Word 审查报告（仅 completed 且 report_path 存在时可用）。

    下载文件名取 task_id 前 8 位而非合同名：中文/超长文件名容易触发下载器
    编码或截断问题。错误码：404 任务或报告文件不存在 / 409 报告尚未生成。
    """
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "审查任务不存在")
    if task["status"] != "completed" or not task.get("report_path"):
        raise HTTPException(409, "报告尚未生成（任务未完成）")
    report_path = Path(task["report_path"])
    if not report_path.exists():
        raise HTTPException(404, "报告文件缺失")
    audit.log("report_downloaded", "task", task_id, task_id, actor=_actor(request),
              detail={"report": "review", "contract": task.get("contract_name")})
    return FileResponse(
        path=str(report_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"审查报告_{task_id[:8]}.docx",
    )


@app.get("/tasks/{task_id}/revised-report")
async def download_revised_report(task_id: str, request: Request):
    """下载修订对照报告（原文 vs 模型改写，按需生成，始终反映最新结果）。"""
    if repo.get_task(task_id) is None:
        raise HTTPException(404, "审查任务不存在")
    path = generate_revised_report(task_id)
    audit.log("report_downloaded", "task", task_id, task_id, actor=_actor(request),
              detail={"report": "revised"})
    return FileResponse(
        path=path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"修订对照_{task_id[:8]}.docx",
    )


@app.get("/tasks/{task_id}/action-list")
async def download_action_list(task_id: str, request: Request):
    """下载整改处置清单（按需生成，包含最新处置状态与备注）。"""
    if repo.get_task(task_id) is None:
        raise HTTPException(404, "审查任务不存在")
    path = generate_action_list(task_id)
    audit.log("report_downloaded", "task", task_id, task_id, actor=_actor(request),
              detail={"report": "action_list"})
    return FileResponse(
        path=path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"整改清单_{task_id[:8]}.docx",
    )


def _remove_file_quiet(path_str: Optional[str]) -> bool:
    """尝试删除磁盘文件（不存在或删除失败均静默），返回是否已删除。"""
    if not path_str:
        return False
    try:
        p = Path(path_str)
        if p.exists():
            p.unlink()
            return True
    except OSError:
        pass
    return False


# ---------------- 历史 ----------------
@app.get("/history")
async def list_history(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    """历史审查任务列表（按创建时间倒序）。"""
    return {"total": repo.count_tasks(), "items": repo.list_tasks(limit=limit, offset=offset)}


@app.delete("/history/{task_id}")
async def delete_history(task_id: str):
    """删除一条历史记录：级联删除逐条结果，并清理报告 docx 与残留上传文件。"""
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(404, "审查任务不存在")
    # 拦截执行中任务：后台线程此刻可能正逐条写 review_results，此刻删除会造成
    # 外键破坏/统计错乱，故要求任务先收敛到终态再删
    if task["status"] in ("pending", "running"):
        raise HTTPException(409, "任务执行中，请等待完成后再删除")
    # 仓储层在事务内"先取行信息、再级联删 results + task"，返回的行信息供下方清理磁盘
    removed = repo.delete_task(task_id)
    removed_files = 0
    # 磁盘清理是"尽力而为"：报告 docx/上传原件被占用或删除失败时静默跳过
    #（_remove_file_quiet 不抛错），文件系统问题不应拖垮整个删除请求
    if removed and removed.get("report_path"):
        removed_files += 1 if _remove_file_quiet(removed["report_path"]) else 0
    if removed and removed.get("file_id"):
        # 审查成功后 scheduler 已自动清理上传件；此处兜底清理仍残留的（如任务失败时）
        upload_path = _resolve_upload_path(removed["file_id"])
        if upload_path is not None:
            removed_files += 1 if _remove_file_quiet(str(upload_path)) else 0
    return {"status": "ok", "deleted": task_id, "removed_files": removed_files}


# ---------------- 审查方案（Playbook） ----------------
def _validate_playbook(payload: PlaybookPayload) -> None:
    """方案载荷校验：名称非空、立场/知识库分类在枚举内。

    这些值会被拼进 Prompt（立场）与检索过滤（kb_categories），存成枚举外值
    会导致方案静默失效且难以排查，故在入口拦截。
    """
    if not payload.name or not payload.name.strip():
        raise HTTPException(422, "方案名称不能为空")
    if payload.stance not in pbrepo.STANCES:
        raise HTTPException(422, f"stance 仅支持 {'/'.join(pbrepo.STANCES)}")
    invalid = set(payload.kb_categories or []) - set(KB_CATEGORIES)
    if invalid:
        raise HTTPException(422, f"未知知识库分类：{', '.join(sorted(invalid))}")


@app.get("/playbooks")
async def list_playbooks(enabled_only: bool = False):
    """方案列表（含禁用项）；enabled_only=true 供上传页只展示可用方案。"""
    return {"items": pbrepo.list_playbooks(enabled_only=enabled_only),
            "stances": list(pbrepo.STANCES),
            "kb_categories": list(KB_CATEGORIES)}


@app.get("/playbooks/{playbook_id}")
async def get_playbook(playbook_id: int):
    """方案详情；不存在返回 404。"""
    item = pbrepo.get_playbook(playbook_id)
    if item is None:
        raise HTTPException(404, "审查方案不存在")
    return item


@app.post("/playbooks")
async def create_playbook(payload: PlaybookPayload, request: Request):
    """新增审查方案；同合同类型下勾选默认时会自动取消其它方案的默认标记。"""
    _validate_playbook(payload)
    created = pbrepo.create_playbook(payload.model_dump())
    audit.log("playbook_created", "playbook", str(created.get("id", "")), "",
              actor=_actor(request), detail={"name": payload.name})
    return created


@app.put("/playbooks/{playbook_id}")
async def update_playbook(playbook_id: int, payload: PlaybookPayload, request: Request):
    """全量更新审查方案（编辑弹窗提交完整对象）；不存在返回 404。"""
    _validate_playbook(payload)
    updated = pbrepo.update_playbook(playbook_id, payload.model_dump())
    if updated is None:
        raise HTTPException(404, "审查方案不存在")
    audit.log("playbook_updated", "playbook", str(playbook_id), "",
              actor=_actor(request), detail={"name": payload.name})
    return updated


@app.delete("/playbooks/{playbook_id}")
async def delete_playbook(playbook_id: int, request: Request):
    """删除审查方案；历史任务持有快照，不受影响。"""
    if not pbrepo.delete_playbook(playbook_id):
        raise HTTPException(404, "审查方案不存在")
    audit.log("playbook_deleted", "playbook", str(playbook_id), "",
              actor=_actor(request))
    return {"status": "ok", "deleted": playbook_id}


# ---------------- 风控规则（P2 可配置） ----------------
@app.get("/rules")
async def list_rules():
    """列出全部风控规则（含禁用项与匹配参数），供规则管理页整体渲染。"""
    return {"items": repo.list_rules()}


@app.post("/rules")
async def create_rule(rule: RuleCreate, request: Request):
    """手动新增一条规则。匹配器四类：percent_ceiling(阈值) / ip_rights / keyword_any / keyword_regex(正则模式)。

    先做白名单/非空校验再落库：matcher_kind 或 risk_level 若存成枚举外值，
    运行期匹配器将无法解析、该规则静默失效，事后极难排查。
    """
    _ALLOWED_MATCHERS = {"percent_ceiling", "ip_rights", "keyword_any", "keyword_regex"}
    if rule.risk_level not in {"高", "中", "低"}:
        raise HTTPException(422, "risk_level 只能是 高/中/低")
    if rule.matcher_kind not in _ALLOWED_MATCHERS:
        raise HTTPException(422, f"matcher_kind 仅支持 {', '.join(sorted(_ALLOWED_MATCHERS))}")
    if not rule.name or not rule.name.strip():
        raise HTTPException(422, "规则名称不能为空")
    created = repo.create_rule(
        name=rule.name.strip(), category=rule.category or "自定义",
        matcher_kind=rule.matcher_kind, params=rule.params,
        risk_level=rule.risk_level, description=rule.description,
        suggestion=rule.suggestion, enabled=rule.enabled)
    audit.log("rule_created", "rule", str(created.get("id", "")), "",
              actor=_actor(request), detail={"name": rule.name, "risk_level": rule.risk_level})
    return created


@app.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int, request: Request):
    """删除一条规则，不存在返回 404。

    "删除后重启不复活"的实现要点在 database.init_db：默认规则仅在
    app_meta['rules_seeded'] 首次缺失时 seed 一次，之后无论 risk_rules 是否
    为空都不再重灌——所以用户删掉的内置规则不会在下次启动被悄悄加回来。
    """
    if not repo.delete_rule(rule_id):
        raise HTTPException(404, "规则不存在")
    audit.log("rule_deleted", "rule", str(rule_id), "", actor=_actor(request))
    return {"status": "ok", "deleted": rule_id}


@app.put("/rules/{rule_id}")
async def update_rule(rule_id: int, patch: RulePatch, request: Request):
    """PATCH 式局部更新规则（名称/风险等级/文案/启用状态，字段均可省略）。

    exclude_unset=True：只提交请求里实际携带的字段，未传字段保持原值，
    避免前端"只改名称"时把 category/description 等误覆盖为空。
    """
    fields = patch.model_dump(exclude_unset=True)
    if patch.risk_level is not None and patch.risk_level not in {"高", "中", "低"}:
        raise HTTPException(422, "risk_level 只能是 高/中/低")
    updated = repo.update_rule(rule_id, **fields)
    if updated is None:
        raise HTTPException(404, "规则不存在")
    audit.log("rule_updated", "rule", str(rule_id), "", actor=_actor(request),
              detail={"fields": list(fields.keys()),
                      "risk_level": fields.get("risk_level")})
    return updated


# ---------------- 法律知识库 ----------------
@app.get("/knowledge/stats")
async def kb_stats():
    """知识库统计（chunk 总量与按 source 的来源分布）；/health 也复用其判断就绪度。"""
    return kb.stats()


@app.post("/knowledge/index")
async def kb_index():
    """扫描 data/kb 重建法律知识库索引（含手动上传的 uploads/ 语料）。

    入库以 md5 稳定 ID 去重（见 vectorstores.stable_id），重复执行只补新增、
    不产生副本——因此可放心幂等调用。错误码：500 索引失败（向量库/嵌入 API 异常）。
    """
    try:
        # 加载→清洗→分块→批量向量化（调嵌入 API）全程同步阻塞，挪到工作线程
        # 执行，避免长时间占住事件循环饿死其它请求
        result = await asyncio.to_thread(kb.build_index)
    except Exception as e:
        raise HTTPException(500, f"索引失败：{e}")
    return result


@app.delete("/knowledge")
async def kb_clear():
    """清空向量索引（运行时生效），不影响 data/kb 磁盘语料。

    与 DELETE /knowledge/document 不同：它只删除向量集合并重置组件缓存，
    之后检索结果为空，需手动 POST /knowledge/index 重建才有数据。
    """
    # 删除整个 collection 并把 vectorstore/retriever 缓存句柄置空，
    # 防止旧句柄继续指向已删除的集合
    kb.clear()
    return {"status": "ok", "message": "法律知识库已清空"}


# ---------------- 学习闭环（越用越聪明）----------------
@app.get("/learning/insights")
async def learning_insights(min_support: int = Query(3, ge=2, le=20)):
    """学习闭环看板：企业样本池规模 + 由历史处置归纳出的规则固化建议。

    数据来源全是法务真实的"已采纳/已修改"动作，不是模拟数据；
    建议只做提示，固化与否由人工确认（AI 提假设、人否决）。
    """
    from review.rule_learner import suggest_rules
    return suggest_rules(min_support=min_support)


@app.post("/learning/apply-rule")
async def learning_apply_rule(rule: RuleCreate):
    """把一条学习建议固化为企业规则（复用规则表 CRUD 的落库逻辑）。"""
    created = repo.create_rule(
        name=rule.name, category=rule.category, matcher_kind=rule.matcher_kind,
        params=rule.params, risk_level=rule.risk_level,
        description=rule.description, suggestion=rule.suggestion)
    return {"status": "ok", "rule": created,
            "message": "已固化为企业规则，后续审查立即生效"}


# ---------------- 知识库管理（前端"知识库"页，参照 smartbrief 接口形态）----------------
@app.get("/knowledge/documents")
async def kb_list_documents():
    """知识库文档列表：按 (category, source) 聚合为一个"文件"条目。"""
    try:
        documents = await asyncio.to_thread(kb.list_documents)
    except Exception as e:
        raise HTTPException(500, f"读取知识库失败：{e}")
    return {"documents": documents}


@app.get("/knowledge/document")
async def kb_document_detail(
    source: str = Query(..., min_length=1, description="文档来源（source 绝对路径）"),
    category: str = Query(..., pattern="^(law|judicial|case|upload)$", description="文档分类"),
):
    """指定 (source, category) 文档的全部分块（前端"查看"弹窗数据源）。"""
    chunks = await asyncio.to_thread(kb.get_document_chunks, source, category)
    if not chunks:
        raise HTTPException(404, "文档不存在或未入库")
    return {"source": source, "category": category, "chunks": chunks}


@app.delete("/knowledge/document")
async def kb_delete_document(
    source: str = Query(..., min_length=1, description="文档来源（source 绝对路径）"),
    category: str = Query(..., pattern="^(law|judicial|case|upload)$", description="文档分类"),
):
    """删除指定 (category, source) 文档：全部分块 + 知识库目录下的源文件。"""
    deleted = await asyncio.to_thread(kb.delete_document, source, category)
    return {"deleted": deleted}


@app.post("/knowledge/documents")
async def kb_upload_document(
    file: UploadFile = File(...),
    category: str = Form("upload", description="文档分类：law/judicial/case/upload"),
):
    """上传法律语料到知识库：落盘 data/kb/uploads/ → 解析 → 分块 → 向量化入库。

    入库幂等（md5 稳定 ID）：同名文件覆盖落盘，内容变更才会新增对应块。
    错误码：422 category 非法 / 400 空文件、非法文件名或扩展名、未配置嵌入
    API Key 等运行期错误（详见 rag/store.upload_file 的异常分类）。
    """
    if category not in KB_CATEGORIES:
        raise HTTPException(422, f"category 只能是 {'/'.join(KB_CATEGORIES)}")
    filename = file.filename or "unnamed.txt"
    content = await file.read()
    if not content:
        raise HTTPException(400, "上传文件为空")
    try:
        # to_thread：upload_file 内含磁盘写入 + 文件解析 + 嵌入 API 批量调用（同步阻塞）
        return await asyncio.to_thread(kb.upload_file, content, filename, category)
    except ValueError as e:
        # 业务校验失败（非法文件名/不支持的扩展名）→ 按用户输入错误返回
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        # 未配置嵌入 API Key 等运行期错误
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"文档入库失败：{e}")


@app.post("/knowledge/search")
async def kb_search(req: KnowledgeSearchRequest):
    """知识库语义检索测试（知识库页"检索测试"数据源），返回命中块与相似度。

    无 404 语义：知识库为空或检索异常时 store 层优雅返回空列表，前端据
    hits 长度提示"无命中/未建索引"。错误码：422 检索文本为空。
    """
    query = req.query.strip()
    if not query:
        raise HTTPException(422, "检索文本不能为空")
    # k 收敛到 [1,20]：每个命中都需向量相似度计算，过大的 k 会放大耗时与成本
    hits = await asyncio.to_thread(kb.search, query, max(1, min(req.k, 20)), req.category)
    return {"query": query, "hits": hits}


# ---------------- 运行时自定义 LLM（模型配置页，OpenAI 兼容 / vLLM 微调模型）----------------
def _llm_config_response() -> dict:
    """汇总"DB 已存配置 + 进程内注入状态"，供读取接口与保存后回显共用。

    applied 与 enabled 未必相等：enabled 是 DB 开关，applied 表示 llm_client
    当前是否真的注入了自定义端点（保存即 apply，正常同步；前端据此显示"生效中"）。
    """
    profile = mrepo.get_profile()
    applied = llm_client.custom_active()
    return {
        "enabled": profile["enabled"],
        "base_url": profile["base_url"],
        # 明文返回：前端回显到输入框，实现"所见即所得"编辑（本地/内网工具）
        "api_key": profile["api_key"] or "",
        "api_key_set": bool(profile["api_key"]),
        "model": profile["model"],
        "updated_at": profile["updated_at"],
        "applied": applied,  # 进程内是否已注入（启用且 base/model 合法）
    }


@app.get("/llm/config")
async def get_llm_config():
    """读取运行时自定义 LLM 配置（api_key 明文返回供前端回显编辑）。"""
    return _llm_config_response()


@app.put("/llm/config")
async def save_llm_config(req: LlmConfigRequest, request: Request):
    """保存运行时自定义 LLM 配置并立即生效；所见即所得：api_key 以请求值为准，
    为空即视为清除已保存的 Key。错误码：422 base_url/model 非法。
    """
    # 去首尾空白与尾部斜杠：统一配置形态，避免后续 URL 拼接出现双斜杠等脏值
    base_url = req.base_url.strip().rstrip("/")
    model = req.model.strip()
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(422, "base_url 必须以 http:// 或 https:// 开头")
    if not model:
        raise HTTPException(422, "模型名称不能为空")
    # OpenAI SDK 的 chat.completions 调用会自动追加 /chat/completions 路径：
    # 用户若照抄网页上的完整调用地址，最终会拼成 .../chat/completions/chat/completions
    # 而 404，故此处校验并要求填 OpenAI 兼容根地址（如 .../v1）
    if base_url.rstrip("/").endswith(("/chat/completions", "/completions")):
        raise HTTPException(422, "base_url 应为 OpenAI 兼容根地址（如 http://127.0.0.1:8001/v1），勿带 /chat/completions")
    api_key = (req.api_key or "").strip()  # 空 → 清空已存 Key（前端已回显原值，所见即所得）
    mrepo.save_profile(req.enabled, base_url, api_key, model)
    # 写库后立即注入全局 llm_client（置顶自定义 provider 并重置探测缓存），
    # 无需重启进程，后续审查/测试请求即走新配置
    apply_custom_to_client()
    audit.log("llm_config_saved", "llm", "custom", "", actor=_actor(request),
              detail={"enabled": req.enabled, "base_url": base_url, "model": model})
    return _llm_config_response()


# ---------------- 灰度发布（canary）----------------
@app.get("/llm/canary")
async def get_canary_config():
    """读取灰度配置与运行时状态（是否熔断）。"""
    return {**mrepo.get_canary(), "status": llm_client.canary_status()}


@app.put("/llm/canary")
async def save_canary_config(req: CanaryConfigRequest, request: Request):
    """保存灰度配置并立即生效；percent 收敛到 0-100。

    启用时强校验端点：canary 是"把一部分真实流量切给新模型"，
    宁可在这里 422，也不要让坏配置在审查时才暴露成整批降级。
    """
    percent = min(100, max(0, int(req.percent or 0)))
    base_url = req.base_url.strip().rstrip("/")
    model = req.model.strip()
    if req.enabled and percent > 0:
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(422, "canary base_url 必须以 http:// 或 https:// 开头")
        if not model:
            raise HTTPException(422, "canary 模型名称不能为空")
    api_key = (req.api_key or "").strip()
    mrepo.save_canary(req.enabled, percent, base_url, api_key, model)
    apply_canary_to_client()
    audit.log("canary_config_saved", "llm", "canary", "", actor=_actor(request),
              detail={"enabled": req.enabled, "percent": percent,
                      "base_url": base_url, "model": model})
    return {**mrepo.get_canary(), "status": llm_client.canary_status()}


@app.get("/metrics/canary")
async def get_canary_metrics():
    """按变体聚合的 A/B 指标（任务数/失败数/平均耗时/token/风险分布）。"""
    data = repo.canary_metrics()
    data["status"] = llm_client.canary_status()
    return data


# ---------------- 审计留痕 + 版本管理 ----------------
@app.get("/audit/logs")
async def list_audit_logs(limit: int = Query(100, ge=1, le=500),
                          offset: int = Query(0, ge=0),
                          task_id: str = "", action: str = "",
                          target_type: str = ""):
    """审计事件分页查询：默认全局倒序，可按任务/动作/对象类型过滤。"""
    data = audit.list_logs(limit=limit, offset=offset, task_id=task_id,
                           action=action, target_type=target_type)
    data["actions"] = audit.count_by_action()
    return data


@app.get("/tasks/{task_id}/versions")
async def get_task_versions(task_id: str):
    """返回该任务所属合同族的版本链（v1 → vN），含每版状态与风险汇总。"""
    if repo.get_task(task_id) is None:
        raise HTTPException(404, "审查任务不存在")
    return repo.list_versions(task_id)


@app.post("/llm/test")
async def test_llm_config(req: LlmProbeRequest):
    """最小对话测试连通性（不落库）；返回耗时与模型回复/错误。

    所见即所得：以请求体中的 Key 为准（与页面输入框同步）。Key 留空也照常
    发起请求——本地 vLLM 通常不校验鉴权，留空即可连通；云端无 Key 会由
    下游返回 401 等真实错误，原样上报前端。
    """
    base_url = req.base_url.strip().rstrip("/")
    model = req.model.strip()
    if not base_url.startswith(("http://", "https://")) or not model:
        raise HTTPException(422, "请填写完整的 base_url 与模型名称")
    api_key = (req.api_key or "").strip()
    # probe_llm 会发起真实最小对话（超时上限 = llm_timeout_seconds，默认 60s），
    # 放线程池执行，避免测试期间长时间卡住事件循环
    ok, latency_ms, message = await asyncio.to_thread(
        probe_llm, base_url, model, api_key)
    return {"ok": ok, "latency_ms": latency_ms, "reply": message if ok else None,
            "error": None if ok else message}


# ---------------- 健康检查 ----------------
def _reranker_mode() -> str:
    """当前生效的重排序模式：disabled / siliconflow / local（本地词重叠降级）。

    siliconflow 需同时满足"配置启用 + 具备任一 SF Key"，否则回退 local
    （判定逻辑见 rag/retrievers.create_reranker 工厂）。
    """
    if not settings.rerank_enabled:
        return "disabled"
    # 函数内延迟导入：仅在健康检查需要区分 reranker 类型时才加载检索器链路，
    # 避免模块顶层 import 把 rag.retrievers 全家桶无谓拉进启动路径
    from rag.retrievers import SiliconFlowReranker, create_reranker
    return "siliconflow" if isinstance(create_reranker(), SiliconFlowReranker) else "local"


@app.get("/health")
async def health():
    """健康检查：汇总 LLM 生效提供方/模型、自定义开关、重排序模式、知识库就绪度。

    供部署探活与前端诊断页使用。任一组件故障都不应让健康检查自身报错——
    LLM/知识库的异常在内部兜底为 none/空，页面据此展示降级状态而非整个报红。
    """
    try:
        # active_info 会对各 provider 逐个做真实网络探活（GET /models，每个约 5s
        # 超时）；包裹 try/except 保证 LLM 全挂/超时也只降级为 none
        info = llm_client.active_info()
    except Exception:
        info = None
    return {
        "status": "ok",
        "llm_provider": info[0] if info else "none",
        "llm_model": info[1] if info else None,
        "custom_active": llm_client.custom_active(),
        "reranker": _reranker_mode(),
        "rerank_model": settings.rerank_model,
        "rerank_top_k": settings.rerank_top_k,
        "kb_ready": (kb.stats().get("total") or 0) > 0,
    }


@app.get("/")
async def root():
    """服务根入口：返回服务名与 /docs 交互文档链接，浏览器直访即可确认存活。"""
    return {"service": "SmartReview 法律合同智能审查系统", "docs": "/docs", "version": "0.1.0"}
