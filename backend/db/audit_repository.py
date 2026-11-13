# =============================================================================
# 审计留痕仓储 - audit_repository.py
# =============================================================================
# 法务场景的基本要求：任何改变合同结论或处置口径的动作，都要留下
# "谁（actor）、何时（created_at）、对什么（target）、改了什么（detail）"。
# 本表只追加不更新，保证历史不可被静默篡改（演示环境不做防删库加固）。
# =============================================================================

import json
from datetime import datetime
from typing import Optional

from db.database import get_conn

DEFAULT_ACTOR = "演示用户"


def log(action: str, target_type: str, target_id: str = "",
        task_id: str = "", actor: str = "", detail: Optional[dict] = None) -> None:
    """写入一条审计事件；失败不抛出，审计不能反向阻断业务主流程。"""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_logs(created_at, actor, action, target_type, "
                "target_id, task_id, detail_json) VALUES (?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"),
                 actor or DEFAULT_ACTOR, action, target_type,
                 str(target_id or ""), str(task_id or ""),
                 json.dumps(detail or {}, ensure_ascii=False)))
    except Exception:
        # 审计是旁路能力：落库失败时宁可丢一条日志，也不能让审查/处置失败
        pass


def list_logs(limit: int = 100, offset: int = 0, task_id: str = "",
              action: str = "", target_type: str = "") -> dict:
    """分页查询审计事件，默认按时间倒序；支持按任务/动作/对象类型过滤。"""
    limit = max(1, min(500, int(limit or 100)))
    offset = max(0, int(offset or 0))
    where, params = [], []
    if task_id:
        where.append("task_id = ?")
        params.append(task_id)
    if action:
        where.append("action = ?")
        params.append(action)
    if target_type:
        where.append("target_type = ?")
        params.append(target_type)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM audit_logs {clause}", params).fetchone()["n"]
        rows = conn.execute(
            f"SELECT id, created_at, actor, action, target_type, target_id, task_id, detail_json "
            f"FROM audit_logs {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset)).fetchall()

    items = []
    for r in rows:
        item = dict(r)
        try:
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
        except Exception:
            item["detail"] = {}
        items.append(item)
    return {"total": total, "items": items}


def count_by_action() -> dict:
    """动作分布（审计页顶部统计卡用）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT action, COUNT(*) n FROM audit_logs GROUP BY action").fetchall()
    return {r["action"]: r["n"] for r in rows}
