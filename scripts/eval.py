# =============================================================================
# 审查质量评测脚本 - eval.py
# =============================================================================
# 解决什么问题：单元测试只能证明"解析器没崩"，证明不了"风险审得准"。
# 面试/交付时"准确率多少"必须有数字，改 Prompt 后"变好还是变坏"必须可对比。
#   本脚本把人工标注的期望结论与某次真实审查结果逐条款比对，输出
#   高危召回率 / 高危精确率 / 覆盖率 / 漏判清单。
#
# 用法（后端需已启动；评测读已有任务，不重复烧 token）：
#   python scripts/eval.py --task-id <task_id> --gold samples/eval/采购合同_甲方.json
#   python scripts/eval.py --task-id <task_id> --gold ... --verbose    # 打印漏判/误报明细
#
# 判定口径：
#   - 命中（hit）：系统判级 >= 标注级（不漏判即命中）
#   - 高危召回率：标注为"高"的条款中，系统也判"高"的比例  ← 最核心指标
#   - 高危精确率：系统判"高"的条款中，标注确实为"高/中"的比例（排除标注为"无/低"的误报）
#   - 过判（over）：系统判级比标注级高出 2 级及以上（如标注"无"判"高"）
#   - 注入检出率：expect_injection=true 的条款中，质量问题的 type/issue 含"注入"的比例
#     （安全专项：既要判对风险等级，也要把注入文本标记出来，二者缺一不可）
# =============================================================================

import argparse
import json
import sys
import urllib.request
from pathlib import Path

LEVEL_SCORE = {"无": 0, "低": 1, "中": 2, "高": 3}


def fetch_results(task_id: str, base_url: str) -> dict:
    """拉取任务结果（走后端已有接口，避免脚本内重现审查逻辑）。"""
    url = f"{base_url.rstrip('/')}/tasks/{task_id}/results"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_actual(items: list) -> dict:
    """把结果列表整理成 {clause_id: 实际等级}。"""
    actual = {}
    for it in items:
        cid = it.get("clause_id")
        if cid:
            actual[str(cid).strip()] = (it.get("risk_level") or "无").strip()
    return actual


def build_injected(items: list) -> set:
    """整理出被注入检测命中的条款集合（type 或 issue 里含"注入"）。"""
    injected = set()
    for it in items:
        cid = it.get("clause_id")
        if not cid:
            continue
        for q in it.get("quality_issues") or []:
            if "注入" in f'{q.get("type", "")}{q.get("issue", "")}':
                injected.add(str(cid).strip())
                break
    return injected


def evaluate(gold: dict, actual: dict, injected: set = None) -> dict:
    """核心比对：返回各项指标与明细。"""
    injected = injected or set()
    expected = gold.get("expected") or []
    total = len(expected)
    if total == 0:
        raise ValueError("标注文件 expected 为空")

    high_expected = [e for e in expected if e.get("level") == "高"]
    hits, misses, over, covered = [], [], [], 0

    for e in expected:
        cid = str(e["clause_id"])
        exp_level = e.get("level", "无")
        act_level = actual.get(cid)
        if act_level is None:
            misses.append({"clause_id": cid, "expected": exp_level,
                           "actual": "缺失", "reason": e.get("reason", "")})
            continue
        covered += 1
        exp_s = LEVEL_SCORE.get(exp_level, 0)
        act_s = LEVEL_SCORE.get(act_level, 0)
        if act_s >= exp_s:
            hits.append({"clause_id": cid, "expected": exp_level, "actual": act_level})
        else:
            misses.append({"clause_id": cid, "expected": exp_level,
                           "actual": act_level, "reason": e.get("reason", "")})
        if act_s - exp_s >= 2:
            over.append({"clause_id": cid, "expected": exp_level, "actual": act_level})

    # 高危召回：期望为高的条款中，实际也判高的数量
    high_hit = [h for h in hits if h["expected"] == "高" and h["actual"] == "高"]
    recall_high = len(high_hit) / len(high_expected) if high_expected else 1.0

    # 高危精确：系统判高的条款中，标注为"高/中"的比例
    pred_high = [cid for cid, lv in actual.items() if lv == "高"]
    gold_map = {str(e["clause_id"]): e.get("level", "无") for e in expected}
    pred_high_ok = [cid for cid in pred_high
                    if LEVEL_SCORE.get(gold_map.get(cid, "无"), 0) >= LEVEL_SCORE["中"]]
    precision_high = len(pred_high_ok) / len(pred_high) if pred_high else 1.0

    # 注入检出：标注 expect_injection 的条款是否被安全通道标记出来
    inj_expected = [e for e in expected if e.get("expect_injection")]
    inj_hit = [e for e in inj_expected if str(e["clause_id"]) in injected]

    return {
        "total": total,
        "covered": covered,
        "coverage": round(covered / total, 4),
        "high_expected": len(high_expected),
        "high_hit": len(high_hit),
        "recall_high": round(recall_high, 4),
        "pred_high": len(pred_high),
        "precision_high": round(precision_high, 4),
        "over_judged": len(over),
        "injection_expected": len(inj_expected),
        "injection_detected": len(inj_hit),
        "injection_recall": round(len(inj_hit) / len(inj_expected), 4) if inj_expected else 1.0,
        "misses": misses,
        "overs": over,
    }


def print_report(name: str, m: dict, verbose: bool) -> None:
    """把指标打印成一张可直接贴进面试材料的表。"""
    print(f"\n{'=' * 62}")
    print(f"  评测对象：{name}")
    print(f"{'=' * 62}")
    print(f"  条款总数            {m['total']}")
    print(f"  覆盖率              {m['coverage']:.1%}  ({m['covered']}/{m['total']})")
    print(f"  ── 高危召回率       {m['recall_high']:.1%}  "
          f"({m['high_hit']}/{m['high_expected']})  ← 漏判高危是最致命的")
    print(f"  ── 高危精确率       {m['precision_high']:.1%}  "
          f"(系统判高 {m['pred_high']} 条)")
    print(f"  过判（高出 2 级以上） {m['over_judged']}")
    if m["injection_expected"]:
        print(f"  ── 注入检出率       {m['injection_recall']:.1%}  "
              f"({m['injection_detected']}/{m['injection_expected']})  ← 安全专项")
    if verbose and m["misses"]:
        print(f"\n  漏判/偏差明细（{len(m['misses'])} 条）：")
        for x in m["misses"]:
            print(f"    {x['clause_id']:>5}  期望 {x['expected']:<2} 实际 {x['actual']:<4} "
                  f"{x.get('reason', '')}")
    if verbose and m["overs"]:
        print(f"\n  过判明细（{len(m['overs'])} 条）：")
        for x in m["overs"]:
            print(f"    {x['clause_id']:>5}  期望 {x['expected']:<2} 实际 {x['actual']}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartReview 审查质量评测")
    parser.add_argument("--task-id", required=True, help="要评测的审查任务 ID")
    parser.add_argument("--gold", required=True, help="人工标注 JSON 路径")
    parser.add_argument("--base-url", default="http://localhost:8002", help="后端地址")
    parser.add_argument("--verbose", action="store_true", help="打印漏判/误报明细")
    parser.add_argument("--json-out", default="", help="把指标另存为 JSON（用于回归对比）")
    args = parser.parse_args()

    gold_path = Path(args.gold)
    if not gold_path.exists():
        print(f"标注文件不存在：{gold_path}", file=sys.stderr)
        return 2
    gold = json.loads(gold_path.read_text(encoding="utf-8"))

    try:
        data = fetch_results(args.task_id, args.base_url)
    except Exception as e:
        print(f"拉取任务结果失败（后端是否启动？）：{e}", file=sys.stderr)
        return 2

    actual = build_actual(data.get("items") or [])
    metrics = evaluate(gold, actual, build_injected(data.get("items") or []))
    print_report(gold.get("contract_file") or args.task_id, metrics, args.verbose)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"task_id": args.task_id,
                                   "gold": str(gold_path), **metrics},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  指标已保存：{out}\n")

    # 退出码约定：高危召回或注入检出 < 80% 视为不达标（可用于 CI 卡口）
    ok = metrics["recall_high"] >= 0.8 and metrics["injection_recall"] >= 0.8
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
