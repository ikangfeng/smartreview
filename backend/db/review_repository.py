# =============================================================================
# 审查数据仓储 - review_repository.py
# =============================================================================
# review_tasks / review_results / risk_rules 三张表的读写封装。
# 结构化字段（clauses_json / summary_json / params_json）在仓储层完成序列化，
# 业务层无感知（与 smartbrief 的仓储约定一致）。
# 任务状态机不跨重启续跑：崩溃遗留的 pending/running 由启动复位为 failed；
# 本文件内 DEFAULT_RULES 是出厂内置规则，seed 与"防复活"逻辑见 database.init_db。
# =============================================================================

import json
from datetime import datetime
from typing import List, Optional

from db.database import get_conn

# 出厂内置规则：三种 matcher_kind 各一例（percent_ceiling/ip_rights/keyword_any），
# 即规则引擎三条确定性能力的最小出厂集。仅由 init_db 首次 seed 写入一次，
# 此后归用户全权管理（可编辑/停用/删除，删除后重启不复活，见 database.init_db）。
DEFAULT_RULES = [
    {
        "name": "违约金/赔偿上限超标",
        "category": "违约责任",
        "matcher_kind": "percent_ceiling",
        "params_json": json.dumps({
            "regex": r"(?:上限|最高|不超过|累计|总计|合计)[^。\n]{0,40}?(\d+(?:\.\d+)?)\s*%",
            "threshold": 20.0,
        }, ensure_ascii=False),
        "risk_level": "高",
        "description": "违约金/赔偿上限可能超过企业红线（合同总额的20%）。",
        "suggestion": "建议将违约金/赔偿上限调整为企业红线以内，即不超过合同总额的20%。",
    },
    {
        "name": "知识产权归属不当",
        "category": "知识产权",
        "matcher_kind": "ip_rights",
        "params_json": "{}",
        "risk_level": "中",
        "description": "知识产权权属约定不利于甲方（非甲方独占）。",
        "suggestion": "建议将合同履行产生的知识产权约定为甲方独占所有。",
    },
    {
        "name": "争议管辖地不利",
        "category": "争议解决",
        "matcher_kind": "keyword_any",
        "params_json": json.dumps({
            "keywords": ["乙方所在地人民法院", "乙方所在地法院", "供应商所在地法院", "供方所在地"],
        }, ensure_ascii=False),
        "risk_level": "中",
        "description": "争议管辖/仲裁地约定可能增加甲方应诉成本。",
        "suggestion": "建议将争议管辖法院或仲裁地约定为甲方所在地。",
    },
]


def _now() -> str:
    """统一时间戳格式 'YYYY-MM-DD HH:MM:SS'（与 database.py 一致）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dumps(obj) -> Optional[str]:
    """结构化字段落库前序列化为 JSON 文本；None 原样存 NULL。"""
    # ensure_ascii=False：中文条款/规则直接可读，便于人工查库与调试
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


def _loads(text: Optional[str]):
    """读出时反序列化；空串或坏 JSON 一律返回 None，不让脏数据炸业务层。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None  # 历史/手工写入的异常值静默降级为"无"，而不是让上层调用崩溃


# ---------------- 任务 ----------------
def create_task(task_id: str, file_id: str, filename: str, contract_name: str,
                party_a: Optional[str], party_b: Optional[str],
                total_amount: Optional[str], clauses: List[dict],
                contract_issues: Optional[dict] = None,
                full_text: Optional[str] = None,
                playbook: Optional[dict] = None,
                root_task_id: Optional[str] = None,
                parent_task_id: Optional[str] = None,
                version: int = 1) -> None:
    """新建审查任务（pending 态），把拆解出的条款全量快照一并落库。

    contract_issues 为整份合同体检结果（合同类型 + 缺失条款/数值冲突/主体不一致），
    在审查开始前由确定性规则算好，随任务一次性落库；full_text 供结果页原文对照。
    playbook 为本次采用的审查方案：id/名称/配置全量快照一并落库，保证历史任务
    可回溯当时口径（方案后续被编辑/删除也不影响本任务）。
    """
    playbook = playbook or {}
    with get_conn() as conn:
        # total_clauses 与 clauses_json 同批写入：进度分母直接取列值，
        # 无需每次反序列化 JSON 去数条款
        conn.execute(
            "INSERT INTO review_tasks(task_id, file_id, filename, contract_name, "
            "party_a, party_b, total_amount, total_clauses, status, clauses_json, "
            "full_text, contract_issues, playbook_id, playbook_name, playbook_json, "
            "current, created_at, root_task_id, parent_task_id, version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, file_id, filename, contract_name, party_a, party_b,
             total_amount, len(clauses), "pending", _dumps(clauses),
             full_text or "", _dumps(contract_issues),
             playbook.get("id"), playbook.get("name"), _dumps(playbook),
             0, _now(), root_task_id or task_id, parent_task_id or "", version))


def list_versions(task_id: str) -> dict:
    """返回该任务所属合同族的版本链（按 version 升序）。

    root_task_id 标识同一合同族：首次上传为 v1，之后每次"修订→复审"生成
    parent=上一版、version=上一版+1 的新任务；历史任务没有该列时以自身为 root。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT task_id, root_task_id FROM review_tasks WHERE task_id=?",
            (task_id,)).fetchone()
        if row is None:
            return {"root_task_id": task_id, "versions": []}
        root = row["root_task_id"] or row["task_id"]
        rows = conn.execute(
            "SELECT task_id, contract_name, version, parent_task_id, status, "
            "total_clauses, summary_json, created_at, finished_at "
            "FROM review_tasks WHERE COALESCE(root_task_id, task_id)=? "
            "ORDER BY COALESCE(version, 1), created_at", (root,)).fetchall()

    versions = []
    for r in rows:
        item = dict(r)
        try:
            item["summary"] = _loads(item.pop("summary_json") or "{}")
        except Exception:
            item["summary"] = {}
        item["is_current"] = item["task_id"] == task_id
        item["version"] = item.get("version") or 1
        versions.append(item)
    return {"root_task_id": root, "versions": versions}


def set_task_running(task_id: str) -> None:
    """置任务为 running 并清零进度（审查工作正式开始前调用）。"""
    with get_conn() as conn:
        conn.execute("UPDATE review_tasks SET status='running', current=0 WHERE task_id=?",
                     (task_id,))


def get_task(task_id: str) -> Optional[dict]:
    """取单任务详情；clauses_json/summary_json 自动还原为内存对象。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM review_tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    # pop 掉原始 JSON 列并反序列化：对外暴露 clauses/summary 键，
    # 调用方无需感知存储列名 clauses_json/summary_json
    d["clauses"] = _loads(d.pop("clauses_json")) or []
    d["summary"] = _loads(d.pop("summary_json"))
    d["contract_issues"] = _loads(d.pop("contract_issues")) or {}
    d["reasonableness"] = _loads(d.pop("reasonableness_json")) or {}
    d["playbook"] = _loads(d.pop("playbook_json")) or {}
    return d


def list_tasks(limit: int = 50, offset: int = 0) -> List[dict]:
    """分页列出任务摘要（列裁剪，不携带条款全量大字段），供任务列表页。"""
    with get_conn() as conn:
        # 刻意按列裁剪而非 SELECT *：clauses_json 是整份条款快照（可能数十 KB），
        # 列表场景用不到，避免每行全量读出拖慢分页响应
        rows = conn.execute(
            "SELECT task_id, filename, contract_name, party_a, party_b, total_amount, "
            "total_clauses, status, current, summary_json, playbook_name, created_at, "
            "finished_at FROM review_tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["summary"] = _loads(d.pop("summary_json"))
        result.append(d)
    return result


def count_tasks() -> int:
    """统计任务总数（前端分页总数/角标数据源）。"""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM review_tasks").fetchone()
    return row["n"]


def update_progress(task_id: str, current: int) -> None:
    """刷新已审条款数 current（前端进度条逐条递增的数据源）。"""
    with get_conn() as conn:
        conn.execute("UPDATE review_tasks SET current=? WHERE task_id=?",
                     (current, task_id))


def finish_task(task_id: str, status: str, summary: dict,
                report_path: Optional[str] = None, error: Optional[str] = None) -> None:
    """写任务终态：status(completed/failed) + 风险汇总 + 报告路径/失败原因。"""
    with get_conn() as conn:
        # summary 已按 risk_level 聚合好，冗余落库：列表页直接读列展示，
        # 免去每次对 review_results 做 GROUP BY 聚合的开销
        conn.execute(
            "UPDATE review_tasks SET status=?, summary_json=?, report_path=?, "
            "error=?, finished_at=? WHERE task_id=?",
            (status, _dumps(summary), report_path, error, _now(), task_id))


def mark_interrupted_as_failed() -> int:
    """后端启动时复位遗留的 running/pending 任务，避免状态卡死。返回受影响行数。"""
    with get_conn() as conn:
        # 同时覆盖 pending 与 running：进程重启后没有调度器再去消费/续跑它们，
        # 不置 failed 会让前端永远停在"排队中/审查中"；finished_at 一并写，
        # 保证列表按时间排序与失败时间可查
        cur = conn.execute(
            "UPDATE review_tasks SET status='failed', error='服务中断，任务未完成', "
            "finished_at=? WHERE status IN ('pending','running')",
            (_now(),))
        return cur.rowcount


def delete_task(task_id: str) -> Optional[dict]:
    """删除一条审查任务（先子表后父表手动级联），返回任务信息供上层清理磁盘文件。

    返回 None 表示任务不存在。调用方应负责删除 report_path 与残留上传文件。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM review_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        info = dict(row)
        # 建表未声明 FOREIGN KEY，级联必须在此手动完成；
        # 两条 DELETE 同处一个 get_conn 事务，任一失败即整体回滚，
        # 不会出现"任务行没了、结果行仍悬空"的中间态
        conn.execute("DELETE FROM review_results WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM review_tasks WHERE task_id=?", (task_id,))
    return info


# ---------------- 结果 ----------------
def save_result(task_id: str, clause_order: int, clause: dict,
                verdict: dict, channel: str,
                usage: Optional[dict] = None, provider: Optional[str] = None,
                variant: str = "baseline") -> None:
    """保存单条款审查结果；独立短事务逐条落库，单条结果自带事务边界。

    usage/provider 记录该条款的 LLM token 消耗与命中的提供商，用于成本核算；
    规则通道或兜底结果没有调用 LLM，传 None 即落 0/空。
    """
    u = usage or {}
    with get_conn() as conn:
        # scheduler 完成一条即调一次本函数、每条独立提交：LLM 审查是秒级耗时，
        # 逐条落库可保证进程中途崩溃时已审条款仍留在 results（任务随后被标 failed）
        conn.execute(
            "INSERT INTO review_results(task_id, clause_order, clause_id, title, content, "
            "char_index, page, risk_level, risk_description, law_basis, suggestion, "
            "confidence, channel, quality_issues, reasonableness_issues, revised_text, "
            "disposition, created_at, prompt_tokens, completion_tokens, llm_provider, "
            "llm_variant) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, clause_order, clause.get("clause_id"), clause.get("title"),
             clause.get("content"), clause.get("char_index"), clause.get("page"),
             verdict.get("risk_level"), verdict.get("risk_description"),
             verdict.get("law_basis"), verdict.get("suggestion"),
             verdict.get("confidence"), channel,
             _dumps(verdict.get("quality_issues") or []),
             _dumps(verdict.get("reasonableness_issues") or []),
             verdict.get("revised_text") or "", "未处理", _now(),
             int(u.get("prompt_tokens", 0) or 0),
             int(u.get("completion_tokens", 0) or 0),
             provider or "", variant or "baseline"))


def set_task_variant(task_id: str, variant: str) -> None:
    """记录本任务的灰度变体（baseline / canary），供 A/B 指标聚合。"""
    with get_conn() as conn:
        conn.execute("UPDATE review_tasks SET llm_variant=? WHERE task_id=?",
                     (variant or "baseline", task_id))


def set_token_usage(task_id: str, prompt_tokens: int, completion_tokens: int) -> None:
    """写入任务级 token 汇总（审查结束时调用，供报告页展示成本）。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE review_tasks SET total_prompt_tokens=?, total_completion_tokens=? "
            "WHERE task_id=?",
            (max(0, int(prompt_tokens)), max(0, int(completion_tokens)), task_id))


def canary_metrics() -> dict:
    """按灰度变体聚合任务级与条款级指标，用于 A/B 对比。

    指标口径刻意保持"可核对"：任务数/完成数/失败数/平均耗时/总 token/
    各风险等级条款数；不在 SQL 里做比率换算，比率交给前端避免除零歧义。
    """
    with get_conn() as conn:
        task_rows = conn.execute(
            "SELECT COALESCE(llm_variant,'baseline') variant, COUNT(*) tasks, "
            "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed, "
            "ROUND(AVG(CASE WHEN finished_at IS NOT NULL THEN "
            " (julianday(finished_at)-julianday(created_at))*86400 END),1) avg_seconds, "
            "SUM(COALESCE(total_prompt_tokens,0)+COALESCE(total_completion_tokens,0)) tokens "
            "FROM review_tasks GROUP BY COALESCE(llm_variant,'baseline')").fetchall()
        risk_rows = conn.execute(
            "SELECT COALESCE(llm_variant,'baseline') variant, risk_level, COUNT(*) n "
            "FROM review_results GROUP BY 1,2").fetchall()

    risk_map: dict[str, dict] = {}
    for r in risk_rows:
        risk_map.setdefault(r["variant"], {})[r["risk_level"] or "无"] = r["n"]

    variants = []
    for r in task_rows:
        variants.append({
            "variant": r["variant"],
            "tasks": r["tasks"],
            "completed": r["completed"] or 0,
            "failed": r["failed"] or 0,
            "avg_seconds": r["avg_seconds"],
            "tokens": r["tokens"] or 0,
            "risk": risk_map.get(r["variant"], {}),
        })
    variants.sort(key=lambda v: v["variant"])
    return {"variants": variants}


def get_result_disposition(task_id: str, clause_order: int) -> Optional[str]:
    """读取单条结果当前处置状态（审计留痕需要记录变更前值）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT disposition FROM review_results WHERE task_id=? AND clause_order=?",
            (task_id, clause_order)).fetchone()
    return (row["disposition"] if row else None) or "未处理"


def update_disposition(task_id: str, clause_order: int, disposition: str,
                       note: Optional[str],
                       custom_revised_text: Optional[str] = None) -> bool:
    """更新单条结果的处置状态与备注；可选存用户自定义改写稿；返回是否命中该结果行。"""
    with get_conn() as conn:
        if custom_revised_text is not None:
            cur = conn.execute(
                "UPDATE review_results SET disposition=?, disposition_note=?, disposition_at=?, custom_revised_text=? "
                "WHERE task_id=? AND clause_order=?",
                (disposition, note or "", _now(), custom_revised_text, task_id, clause_order))
        else:
            cur = conn.execute(
                "UPDATE review_results SET disposition=?, disposition_note=?, disposition_at=? "
                "WHERE task_id=? AND clause_order=?",
                (disposition, note or "", _now(), task_id, clause_order))
        return cur.rowcount > 0


def set_masked_count(task_id: str, count: int) -> None:
    """记录本次送审脱敏的敏感信息处数（前端展示“已脱敏 N 处”）。"""
    with get_conn() as conn:
        conn.execute("UPDATE review_tasks SET masked_count=? WHERE task_id=?",
                     (max(0, int(count)), task_id))


def save_reasonableness(task_id: str, summary: dict) -> None:
    """写入合同级合理性评分与结论（任务完成汇总时调用）。"""
    with get_conn() as conn:
        conn.execute("UPDATE review_tasks SET reasonableness_json=? WHERE task_id=?",
                     (_dumps(summary), task_id))


def get_adopted_revisions(limit: int = 200) -> List[dict]:
    """取历史上被法务采纳/修改的改写稿，作为少样本示例来源（越用越聪明的数据底座）。

    只取有实际改写文本的行：没有改写等于没提供"该怎么改"的先例。
    优先用户手动修改过的版本（custom_revised_text），它最能代表这家企业的口径。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT task_id, clause_order, clause_id, title, content, "
            "COALESCE(NULLIF(custom_revised_text,''), revised_text) AS final_text, "
            "disposition, disposition_at "
            "FROM review_results "
            "WHERE disposition IN ('已采纳','已修改') "
            "AND COALESCE(NULLIF(custom_revised_text,''), revised_text) IS NOT NULL "
            "AND LENGTH(TRIM(COALESCE(NULLIF(custom_revised_text,''), revised_text))) > 10 "
            "ORDER BY COALESCE(disposition_at, created_at) DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
        return [dict(r) for r in rows]


def count_dispositions() -> dict:
    """按处置状态统计全库条数（学习闭环的效果看板：采纳越多，样本池越大）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT disposition, COUNT(*) AS n FROM review_results GROUP BY disposition"
        ).fetchall()
        return {r["disposition"] or "未处理": r["n"] for r in rows}


def get_results(task_id: str) -> List[dict]:
    """取某任务的全部逐条结果，按条款先后顺序返回（详情/报告数据源）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM review_results WHERE task_id=? ORDER BY clause_order ASC",
            (task_id,)).fetchall()
    return [dict(r) for r in rows]


def get_summary(task_id: str) -> dict:
    """统计 高/中/低/无 计数（忽略未知等级），用于汇总与写回 summary_json。"""
    levels = {"高": 0, "中": 0, "低": 0, "无": 0}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT risk_level, COUNT(*) AS n FROM review_results "
            "WHERE task_id=? GROUP BY risk_level", (task_id,)).fetchall()
    for r in rows:
        # 只认四档白名单：库里若混入未知等级（历史/手工数据）则跳过，
        # 既不入统计也不抛错污染前端
        if r["risk_level"] in levels:
            levels[r["risk_level"]] = r["n"]
    return levels


# ---------------- 风控规则 ----------------
def get_enabled_rules() -> List[dict]:
    """读取启用中的规则（enabled=1）供审查通道匹配，停用规则不参与。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM risk_rules WHERE enabled=1 ORDER BY id").fetchall()
    rules = []
    for r in rows:
        d = dict(r)
        # 参数列转 dict 后以 params 键暴露，规则引擎可直接取用
        d["params"] = _loads(d.pop("params_json")) or {}
        rules.append(d)
    return rules


def list_rules() -> List[dict]:
    """列出全部规则（含停用行），规则管理页表格的数据源。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM risk_rules ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def update_rule(rule_id: int, **fields) -> Optional[dict]:
    """按字段白名单更新规则；返回更新后的规则，无可更新字段或不存在时返回 None。"""
    # 白名单限定可更新列：入参来自 HTTP，可防注入且杜绝把 matcher_kind/params_json
    # 等关键列带进 SET（误改匹配器类型会让存量 params 失配）
    allowed = {"name", "category", "risk_level", "description", "suggestion", "enabled"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return None  # 调用方没传任何合法字段：不执行空 UPDATE，直接返回 None
    assignments = ", ".join(f"{k}=?" for k in data)  # 键全部来自上方白名单，可安全拼接列名
    values = tuple(data.values()) + (_now(), rule_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE risk_rules SET {assignments}, updated_at=? WHERE id=?",
                     values)
    # 写入事务已提交关闭，另开连接读回最新行返回，保证调用方拿到的是库中真实状态
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM risk_rules WHERE id=?", (rule_id,)).fetchone()
    return dict(row) if row else None


def delete_rule(rule_id: int) -> bool:
    """删除一条规则；返回是否存在该规则。"""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM risk_rules WHERE id=?", (rule_id,))
        # 只删规则行，刻意不碰 app_meta 的 rules_seeded 标记：标记一旦写入即永久
        # 有效，用户删光默认规则后重启也不会被 init_db 重新灌入（防复活边界在此）
    return cur.rowcount > 0


def create_rule(name: str, category: Optional[str] = None,
                matcher_kind: str = "keyword_any", params: Optional[dict] = None,
                risk_level: str = "中", description: Optional[str] = None,
                suggestion: Optional[str] = None, enabled: bool = True) -> dict:
    """手动新增一条风控规则；匹配参数按 JSON 存入 params_json。"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO risk_rules(name, category, matcher_kind, params_json, "
            "risk_level, description, suggestion, enabled, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, category, matcher_kind, _dumps(params), risk_level,
             description, suggestion, 1 if enabled else 0, _now()))
        rule_id = cur.lastrowid  # 取刚插入的自增主键，随后回查完整行返回
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM risk_rules WHERE id=?", (rule_id,)).fetchone()
    return dict(row)
