#!/usr/bin/env python3
# =============================================================================
# 从 datachain/data/risk_rules.json 灌入 46 条完整风控规则
# =============================================================================
# - 幂等：按 name 去重，已存在同名规则跳过不覆盖
# - 不会触碰 app_meta.rules_seeded 标记（出厂规则 vs 完整规则是两套独立种子）
# - 执行：cd backend && python scripts/seed_full_rules.py
# =============================================================================

import json
import os
import sys
from datetime import datetime

# 确保 backend 根在 sys.path（脚本从 scripts/ 下运行）
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

# datachain risk_rules.json 路径（datachain 与 smartreview 同级，在"最后一舞"目录下）
# backend/scripts/seed_full_rules.py  →  BACKEND_DIR = smartreview/backend
# 往上 smartreview/ → "32.【合同审查引擎实现】/" → "最后一舞"/
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)          # smartreview/
PARENT_DIR = os.path.dirname(PROJECT_ROOT)           # "32.【合同审查引擎实现】/"
GRANDPARENT_DIR = os.path.dirname(PARENT_DIR)         # "最后一舞"/
DATACHAIN_ROOT = os.path.join(
    GRANDPARENT_DIR, "29.【 行业指令数据集构建与标准化处理】", "datachain")
JSON_PATH = os.path.join(DATACHAIN_ROOT, "data", "risk_rules.json")

# rule_id 前缀 → (matcher_kind, params) 映射表
# 这些规则有明确数值阈值，percent_ceiling 能做精确数值比较
_PERCENT_RULES = {
    "LIABILITY-001": {"threshold": 20.0},  # 违约金上限 20%
    "LIABILITY-002": {"threshold": 0.1},   # 每日违约金 0.1%
    "LIABILITY-006": {"threshold": 20.0},  # 定金 20%
    "PAY-001":       {"threshold": 30.0},   # 预付款 30%
}


def classify_matcher(rule: dict) -> tuple:
    """根据 rule_id 前缀决定 matcher_kind 和 params。

    返回 (matcher_kind, params_dict)
    """
    rid = rule.get("rule_id", "")
    trigger = rule.get("trigger", "")

    if rid in _PERCENT_RULES:
        params = {
            "regex": r"(\d+(?:\.\d+)?)\s*%",
            "threshold": _PERCENT_RULES[rid]["threshold"],
        }
        return "percent_ceiling", params

    # 其余全部走 keyword_regex（trigger 字段直接当正则模式）
    return "keyword_regex", {"triggers": trigger}


def main():
    if not os.path.isfile(JSON_PATH):
        print(f"❌ risk_rules.json 不存在: {JSON_PATH}")
        sys.exit(1)

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        src = json.load(f)

    rules = src.get("rules", [])
    print(f"📖 读入 risk_rules.json: {len(rules)} 条规则")

    # ---- 连接 DB ----
    from db.database import connect
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = connect()

    # ---- 已有规则名集合（去重） ----
    existing = {row["name"] for row in
                conn.execute("SELECT name FROM risk_rules").fetchall()}
    print(f"🗄️  DB 已有 {len(existing)} 条规则（按 name 去重）")

    # ---- 逐条转换 + 灌入 ----
    inserted = 0
    skipped = 0
    percent_count = 0
    regex_count = 0

    for r in rules:
        name = r.get("name", "").strip()
        if not name:
            skipped += 1
            continue
        if name in existing:
            skipped += 1
            continue

        matcher_kind, params = classify_matcher(r)
        params_json = json.dumps(params, ensure_ascii=False)

        if matcher_kind == "percent_ceiling":
            percent_count += 1
        else:
            regex_count += 1

        conn.execute(
            "INSERT INTO risk_rules(name, category, matcher_kind, params_json, "
            "risk_level, description, suggestion, enabled, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                name,
                r.get("risk_type") or r.get("category"),
                matcher_kind,
                params_json,
                r.get("level") or r.get("risk_level", "中"),
                r.get("threshold") or r.get("description"),
                r.get("suggestion"),
                1,  # enabled
                now,
            ),
        )
        inserted += 1

    conn.commit()
    conn.close()

    # ---- 汇总 ----
    total = inserted + skipped
    print(f"\n✅ 灌入完成:")
    print(f"   新增 {inserted} 条（percent_ceiling={percent_count}, keyword_regex={regex_count}）")
    print(f"   跳过 {skipped} 条（同名已存在或 name 为空）")
    print(f"   源文件总计 {len(rules)} 条")

    # ---- 打印最终 DB 统计 ----
    conn = connect()
    rows = conn.execute(
        "SELECT risk_level, COUNT(*) AS n FROM risk_rules GROUP BY risk_level ORDER BY n DESC"
    ).fetchall()
    matchers = conn.execute(
        "SELECT matcher_kind, COUNT(*) AS n FROM risk_rules GROUP BY matcher_kind"
    ).fetchall()
    total_in_db = conn.execute("SELECT COUNT(*) AS n FROM risk_rules").fetchone()["n"]
    conn.close()

    print(f"\n📊 DB 最终统计（共 {total_in_db} 条）:")
    for r in rows:
        print(f"   等级 {r['risk_level']}: {r['n']} 条")
    print("   ---")
    for m in matchers:
        print(f"   matcher {m['matcher_kind']}: {m['n']} 条")


if __name__ == "__main__":
    main()
