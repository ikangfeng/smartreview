# =============================================================================
# 审查方案仓储 - playbook_repository.py
# =============================================================================
# review_playbooks 表读写。审查方案（Playbook）= 合同类型 + 审查立场 + 企业口径：
#   - required_clauses：该类型下必须存在的条款清单（完整性检查数据源）
#   - rule_ids：参与本次审查的确定性规则子集（空数组 = 全部启用规则）
#   - kb_categories：限定 RAG 法条检索的知识库范围（空 = 不限）
#   - extra_prompt：叠加到系统提示词的附加审查要求
# 发起审查时按方案取配置并做全量快照落库（review_tasks.playbook_json），
# 因此后续编辑/删除方案不会改写出历史任务的审查口径。
# 出厂方案与风控规则一致：仅在 app_meta['playbooks_seeded'] 缺失时 seed 一次，
# 用户删改后重启不复活。
# =============================================================================

import json
from datetime import datetime
from typing import List, Optional

from db.database import get_conn

STANCES = ("甲方", "乙方", "中立")
KB_CATEGORIES = ("law", "judicial", "case", "upload")

# 出厂方案：必备条款从 completeness_checker.CONTRACT_PROFILES 迁移，
# 规则按 category 圈定子集（seed 时解析为真实 id）。
# 通用合同 rule_ids 留空 = 全部启用规则，作为任何合同类型的兜底方案。
_DEFAULT_PLAYBOOK_SPECS = [
    {
        "name": "技术服务/开发合同 · 甲方审查方案",
        "contract_type": "技术服务/开发合同",
        "stance": "甲方",
        "is_default": True,
        "description": "适用于技术开发、技术服务、软件开发、系统集成、运维等合同；"
                       "必备条款覆盖交付验收、知识产权、保密、违约责任与争议解决。",
        "rule_categories": ["违约责任", "违约与责任", "知识产权", "保密", "付款与结算",
                            "验收与交付", "管辖与争议解决", "争议解决", "期限与终止", "数据合规"],
        "kb_categories": list(KB_CATEGORIES),
        "extra_prompt": "重点关注：交付物与验收标准是否量化、开发成果知识产权是否归甲方独占、"
                        "乙方人员与数据合规义务、运维 SLA 与故障响应时限、"
                        "验收与付款节点是否挂钩。",
    },
    {
        "name": "采购/买卖合同 · 甲方审查方案",
        "contract_type": "采购/买卖合同",
        "stance": "甲方",
        "is_default": True,
        "description": "适用于采购、供货、买卖、购销合同；必备条款覆盖标的规格、"
                       "交付验收、质量保证、付款结算与违约责任。",
        "rule_categories": ["违约责任", "违约与责任", "付款与结算", "验收与交付",
                            "税费与发票", "期限与终止", "管辖与争议解决", "争议解决", "知识产权"],
        "kb_categories": ["law", "judicial", "case"],
        "extra_prompt": "重点关注：货物质量标准与质保期、验收与付款节点是否挂钩、"
                        "逾期交货与质量不符的违约责任、发票类型与税费承担。",
    },
    {
        "name": "通用合同 · 甲方审查方案",
        "contract_type": "通用合同",
        "stance": "甲方",
        "is_default": True,
        "description": "未识别出具体合同类型时的兜底方案：使用全部启用规则，"
                       "并执行通用必备条款清单检查。",
        "rule_categories": [],  # 空 = 不裁剪，全部启用规则参与审查
        "kb_categories": list(KB_CATEGORIES),
        "extra_prompt": "按企业通用审查口径执行：优先识别对甲方不利的权利义务失衡、"
                        "责任不对等与不可执行条款。",
    },
]


def _now() -> str:
    """统一时间戳格式（与 database.py / review_repository.py 一致）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dumps(obj) -> Optional[str]:
    """结构化字段序列化为 JSON 文本；None 原样存 NULL。"""
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


def _loads(text: Optional[str]):
    """读取 JSON 列；空值/坏 JSON 一律降级为 None，避免脏数据炸业务层。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _normalize_required(items) -> List[dict]:
    """规整必备条款结构：keywords 兼容字符串（逗号/顿号分隔），缺省字段补空值。"""
    result = []
    for item in items or []:
        if not isinstance(item, dict) or not (item.get("name") or "").strip():
            continue
        keywords = item.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.replace("，", ",").replace("、", ",").split(",")]
        severity = item.get("severity") if item.get("severity") in ("高", "中", "低") else "中"
        result.append({
            "name": item["name"].strip(),
            "keywords": [str(k).strip() for k in keywords if str(k).strip()],
            "severity": severity,
            "suggestion": (item.get("suggestion") or "").strip(),
        })
    return result


def _row_to_playbook(row) -> dict:
    """DB 行 → 业务对象：JSON 列还原为数组，数值开关保持 int（前端按 0/1 渲染）。"""
    d = dict(row)
    d["required_clauses"] = _loads(d.pop("required_clauses", None)) or []
    d["rule_ids"] = _loads(d.pop("rule_ids", None)) or []
    d["kb_categories"] = _loads(d.pop("kb_categories", None)) or []
    return d


def list_playbooks(enabled_only: bool = False) -> List[dict]:
    """方案列表（按合同类型/主键排序）；enabled_only 用于上传页只展示可用方案。"""
    sql = "SELECT * FROM review_playbooks"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY contract_type, id"
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_playbook(r) for r in rows]


def get_playbook(playbook_id: int) -> Optional[dict]:
    """按 id 取方案；不存在返回 None。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM review_playbooks WHERE id=?",
                           (playbook_id,)).fetchone()
    return _row_to_playbook(row) if row else None


def resolve_playbook(playbook_id: Optional[int], contract_type: Optional[str]) -> Optional[dict]:
    """确定本次审查使用的方案。

    优先按显式指定的 playbook_id；未指定则按识别出的合同类型取默认方案
    （is_default 优先），再回退“通用合同”，最后回退任一启用方案。
    一条方案都没有时返回 None（审查按原有逻辑执行）。
    """
    with get_conn() as conn:
        if playbook_id:
            row = conn.execute("SELECT * FROM review_playbooks WHERE id=?",
                               (playbook_id,)).fetchone()
            return _row_to_playbook(row) if row else None
        for ct in (contract_type, "通用合同"):
            if not ct:
                continue
            row = conn.execute(
                "SELECT * FROM review_playbooks WHERE enabled=1 AND contract_type=? "
                "ORDER BY is_default DESC, id ASC LIMIT 1", (ct,)).fetchone()
            if row:
                return _row_to_playbook(row)
        row = conn.execute(
            "SELECT * FROM review_playbooks WHERE enabled=1 "
            "ORDER BY is_default DESC, id ASC LIMIT 1").fetchone()
        return _row_to_playbook(row) if row else None


def _save_default_flag(conn, contract_type: Optional[str], is_default: bool,
                       exclude_id: Optional[int] = None) -> None:
    """同合同类型只保留一个默认方案：新默认落库前先清掉其它行的默认标记。"""
    if not is_default:
        return
    conn.execute(
        "UPDATE review_playbooks SET is_default=0 WHERE contract_type IS ? "
        "AND id IS NOT ?", (contract_type, exclude_id))


def create_playbook(payload: dict) -> dict:
    """新增方案；payload 字段由 API 层校验后传入。"""
    contract_type = payload.get("contract_type")
    is_default = bool(payload.get("is_default"))
    required = _normalize_required(payload.get("required_clauses"))
    now = _now()
    with get_conn() as conn:
        _save_default_flag(conn, contract_type, is_default)
        cur = conn.execute(
            "INSERT INTO review_playbooks(name, contract_type, stance, description, "
            "required_clauses, rule_ids, kb_categories, extra_prompt, is_default, enabled, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (payload.get("name", "").strip(), contract_type,
             payload.get("stance") or "甲方", payload.get("description") or "",
             _dumps(required), _dumps(payload.get("rule_ids") or []),
             _dumps(payload.get("kb_categories") or []), payload.get("extra_prompt") or "",
             1 if is_default else 0, 1 if payload.get("enabled", True) else 0, now, now))
        playbook_id = cur.lastrowid
    return get_playbook(playbook_id)


def update_playbook(playbook_id: int, payload: dict) -> Optional[dict]:
    """全量更新方案（前端编辑弹窗提交完整对象）；不存在返回 None。"""
    if get_playbook(playbook_id) is None:
        return None
    contract_type = payload.get("contract_type")
    is_default = bool(payload.get("is_default"))
    required = _normalize_required(payload.get("required_clauses"))
    with get_conn() as conn:
        _save_default_flag(conn, contract_type, is_default, exclude_id=playbook_id)
        conn.execute(
            "UPDATE review_playbooks SET name=?, contract_type=?, stance=?, description=?, "
            "required_clauses=?, rule_ids=?, kb_categories=?, extra_prompt=?, is_default=?, "
            "enabled=?, updated_at=? WHERE id=?",
            (payload.get("name", "").strip(), contract_type,
             payload.get("stance") or "甲方", payload.get("description") or "",
             _dumps(required), _dumps(payload.get("rule_ids") or []),
             _dumps(payload.get("kb_categories") or []), payload.get("extra_prompt") or "",
             1 if is_default else 0, 1 if payload.get("enabled", True) else 0,
             _now(), playbook_id))
    return get_playbook(playbook_id)


def delete_playbook(playbook_id: int) -> bool:
    """删除方案；历史任务持有 playbook_json 快照，不受影响。"""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM review_playbooks WHERE id=?", (playbook_id,))
    return cur.rowcount > 0


def seed_default_playbooks(conn) -> None:
    """写出厂方案（仅在 app_meta['playbooks_seeded'] 缺失时由 init_db 调用）。

    必备条款直接从 CONTRACT_PROFILES 迁移为快照结构；规则 id 按 category
    现场解析——这样即使规则是 datachain 导入的、id 不固定，也能正确圈定。
    """
    from review.completeness_checker import CONTRACT_PROFILES

    profile_map = {p["type"]: p for p in CONTRACT_PROFILES}
    rule_rows = conn.execute("SELECT id, category FROM risk_rules").fetchall()
    now = _now()

    for spec in _DEFAULT_PLAYBOOK_SPECS:
        profile = profile_map.get(spec["contract_type"]) or {}
        required = [
            {"name": name, "keywords": list(keywords),
             "severity": severity, "suggestion": suggestion}
            for name, keywords, severity, suggestion in profile.get("required", [])
        ]
        cats = set(spec.get("rule_categories") or [])
        # 空 category 集合 = 不裁剪（全部启用规则），与 rule_ids=[] 语义一致
        rule_ids = [r["id"] for r in rule_rows if not cats or r["category"] in cats]
        conn.execute(
            "INSERT INTO review_playbooks(name, contract_type, stance, description, "
            "required_clauses, rule_ids, kb_categories, extra_prompt, is_default, enabled, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
            (spec["name"], spec["contract_type"], spec["stance"], spec["description"],
             _dumps(_normalize_required(required)), _dumps(rule_ids),
             _dumps(spec["kb_categories"]), spec["extra_prompt"],
             1 if spec["is_default"] else 0, now, now))
