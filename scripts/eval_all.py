# =============================================================================
# 批量评测 + CI 卡口 - eval_all.py
# =============================================================================
# 单个 eval.py 解决"一份合同准不准"，本脚本解决"整套能力有没有退化"。
# 它扫描 samples/eval/*.json，逐份评测，最后汇总加权指标并按阈值给出退出码，
# 因此可以直接作为 CI 门禁：不达标就让流水线红掉。
#
# 两种取数模式：
#   1. 默认（快、省 token）：复用标注文件里记录的 task_id 去读已有结果；
#   2. --run（慢、烧 token、完全可复现）：自己上传+发起审查+等完成再评测。
#      CI 里建议用 --run，因为只有它保证"评的是当前代码"。
#
# 退出码约定（CI 卡口的核心）：
#   0 = 全部达标；1 = 指标不达标；2 = 环境/数据异常（如后端未启动）
# =============================================================================

import argparse
import importlib.util
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "samples" / "eval"
CONTRACT_DIR = ROOT / "samples" / "contracts"


def _load_eval_module():
    """按文件路径加载 eval.py（避免 'eval' 与内置函数同名带来的导入歧义）。"""
    spec = importlib.util.spec_from_file_location(
        "sr_eval", Path(__file__).resolve().parent / "eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ev = _load_eval_module()


def _opener():
    """绕过系统代理：localhost 请求不应走 http_proxy（否则会被 502 拦截）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_file(base: str, path: Path) -> dict:
    boundary = "----SRB"
    data = path.read_bytes()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{base}/upload", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.loads(_opener().open(req, timeout=120).read())


def _post_json(base: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    return json.loads(_opener().open(req, timeout=120).read())


def _wait_task(base: str, task_id: str, timeout_s: int = 300) -> str:
    """轮询直到任务终态，返回最终状态。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = json.loads(_opener().open(f"{base}/tasks/{task_id}/status", timeout=30).read())
        if st.get("status") in ("completed", "failed"):
            return st["status"]
        time.sleep(3)
    return "timeout"


def run_one(base: str, gold: dict, gold_path: Path, do_run: bool) -> dict:
    """对一份标注执行评测；返回指标（失败时带 error 字段）。"""
    task_id = gold.get("task_id") or ""
    if do_run:
        contract = CONTRACT_DIR / (gold.get("contract_file") or "")
        if not contract.exists():
            return {"file": gold_path.name, "error": f"合同不存在：{contract}"}
        up = _post_file(base, contract)
        rv = _post_json(base, "/review", {
            "file_id": up.get("file_id"),
            "contract_type": gold.get("contract_type") or "其他",
            "stance": gold.get("stance") or "甲方",
        })
        task_id = rv.get("task_id") or ""
        status = _wait_task(base, task_id)
        if status != "completed":
            return {"file": gold_path.name, "error": f"审查未完成：{status}"}

    if not task_id:
        return {"file": gold_path.name,
                "error": "标注未记录 task_id，且未启用 --run（无法取数）"}

    data = ev.fetch_results(task_id, base)
    items = data.get("items") or []
    metrics = ev.evaluate(gold, ev.build_actual(items), ev.build_injected(items))
    return {"file": gold_path.name, "task_id": task_id, **metrics}


def main() -> int:
    ap = argparse.ArgumentParser(description="SmartReview 批量评测 / CI 卡口")
    ap.add_argument("--base-url", default="http://localhost:8002")
    ap.add_argument("--run", action="store_true",
                    help="重新上传并审查（可复现，但慢且消耗 token）")
    ap.add_argument("--min-recall", type=float, default=0.80,
                    help="高危召回率下限（默认 0.80）")
    ap.add_argument("--min-precision", type=float, default=0.80,
                    help="高危精确率下限（默认 0.80）")
    ap.add_argument("--min-injection", type=float, default=1.00,
                    help="注入检出率下限（默认 1.00，注入检测是确定性规则）")
    ap.add_argument("--only", default="",
                    help="只评测文件名/合同名包含该子串的标注（调试用，如 --only 注入）")
    ap.add_argument("--dir", default="",
                    help="标注目录（默认 samples/eval；holdout 用 samples/eval/holdout）")
    ap.add_argument("--report-only", action="store_true",
                    help="只报告指标、不按阈值失败（用于 holdout 泛化观测）")
    ap.add_argument("--json-out", default="", help="把汇总指标写入 JSON（供流水线归档）")
    args = ap.parse_args()

    gold_dir = Path(args.dir) if args.dir else EVAL_DIR
    if not gold_dir.is_absolute():
        gold_dir = ROOT / gold_dir
    golds = sorted(gold_dir.glob("*.json"))
    if args.only:
        golds = [g for g in golds
                 if args.only in g.name or args.only in g.read_text(encoding="utf-8")]
    if not golds:
        print(f"未找到标注文件：{gold_dir}", file=sys.stderr)
        return 2

    def _noop(*a, **k):
        pass

    print(f"\n{'=' * 74}\n  SmartReview 批量评测  ({len(golds)} 份标注"
          f"{'，重新审查模式' if args.run else '，复用已有任务'})\n{'=' * 74}")

    results = []
    for gp in golds:
        gold = json.loads(gp.read_text(encoding="utf-8"))
        print(f"\n▶ {gold.get('contract_file') or gp.stem}")
        try:
            m = run_one(args.base_url, gold, gp, args.run)
        except Exception as e:
            m = {"file": gp.name, "error": str(e)[:200]}
        results.append(m)
        if m.get("error"):
            print(f"  ✗ {m['error']}")
        else:
            inj = (f" | 注入检出 {m['injection_recall']:.1%} "
                   f"({m['injection_detected']}/{m['injection_expected']})"
                   if m["injection_expected"] else "")
            print(f"  task_id={m['task_id']}")
            print(f"  覆盖率 {m['coverage']:.1%} | 高危召回 {m['recall_high']:.1%} "
                  f"({m['high_hit']}/{m['high_expected']}) | "
                  f"高危精确 {m['precision_high']:.1%} | 过判 {m['over_judged']}{inj}")

    ok = [r for r in results if not r.get("error")]
    if not ok:
        print("\n没有可用结果，判定为环境异常", file=sys.stderr)
        return 2

    # 加权汇总：按标注的高危条款总数加权，而不是按合同份数平均，
    # 否则一份 2 条标注的合同会和一份 22 条的合同权重相同
    tot_high = sum(r["high_expected"] for r in ok)
    tot_hit = sum(r["high_hit"] for r in ok)
    tot_pred = sum(r["pred_high"] for r in ok)
    recall = tot_hit / tot_high if tot_high else 1.0
    # 精确率按"判高且标注≥中"的数量近似：用各份 precision*pred 求和 / 总 pred
    tot_pred_ok = sum(round(r["precision_high"] * r["pred_high"]) for r in ok)
    precision = tot_pred_ok / tot_pred if tot_pred else 1.0
    # 注入检出：只统计有安全标注的合同，加权口径与召回一致
    tot_inj = sum(r["injection_expected"] for r in ok)
    tot_inj_hit = sum(r["injection_detected"] for r in ok)
    injection = tot_inj_hit / tot_inj if tot_inj else 1.0

    print(f"\n{'=' * 74}")
    print(f"  汇总（加权，按高危条款数）")
    print(f"  ── 高危召回率   {recall:.1%}  ({tot_hit}/{tot_high})   阈值 ≥ {args.min_recall:.0%}")
    print(f"  ── 高危精确率   {precision:.1%}  (判高 {tot_pred} 条)   阈值 ≥ {args.min_precision:.0%}")
    if tot_inj:
        print(f"  ── 注入检出率   {injection:.1%}  ({tot_inj_hit}/{tot_inj})   阈值 ≥ {args.min_injection:.0%}")
    print(f"  异常合同       {len(results) - len(ok)} 份")
    print(f"{'=' * 74}\n")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "run" if args.run else "reuse",
            "weighted": {"recall_high": round(recall, 4),
                         "precision_high": round(precision, 4),
                         "injection_recall": round(injection, 4),
                         "high_expected": tot_high, "high_hit": tot_hit,
                         "injection_expected": tot_inj, "injection_detected": tot_inj_hit},
            "thresholds": {"min_recall": args.min_recall,
                           "min_precision": args.min_precision,
                           "min_injection": args.min_injection},
            "contracts": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  指标已归档：{out}\n")

    if args.report_only:
        print("ℹ️  report-only：本组指标仅用于泛化观测，不参与门禁判定\n")
        return 0

    failed = (recall < args.min_recall or precision < args.min_precision
              or injection < args.min_injection
              or len(results) != len(ok))
    if failed:
        print("❌ CI 卡口未通过：指标低于阈值或存在异常合同")
        return 1
    print("✅ CI 卡口通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
