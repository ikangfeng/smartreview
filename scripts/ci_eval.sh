#!/usr/bin/env bash
# =============================================================================
# CI 准入卡口 - ci_eval.sh
# =============================================================================
# 一条命令跑完两道门禁，任一不通过即非零退出，可直接挂到 CI（GitHub Actions /
# Jenkins / GitLab CI）或本地 pre-push 钩子：
#
#   第 1 道：pytest         —— 确定性单测（解析器/规则引擎/API），秒级，硬门禁
#   第 2 道：eval_all.py    —— 语义质量评测（高危召回/精确率/注入检出），
#                              复用标注集记录的 task_id 取数，不重复烧 token
#
# 用法：
#   scripts/ci_eval.sh                     # 默认：复用已审查任务（快，不烧 token）
#   scripts/ci_eval.sh --run               # 重新上传并审查（可复现，消耗 token）
#   scripts/ci_eval.sh --base-url http://127.0.0.1:8002
#
# 环境变量（可覆盖阈值）：
#   MIN_RECALL=0.80  MIN_PRECISION=0.80  MIN_INJECTION=1.00
#   PYTHON=python3
#
# 退出码：0 通过 / 1 指标或单测不达标 / 2 环境异常（如后端未启动）
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
BASE_URL="http://127.0.0.1:8002"
RUN_MODE=0
MIN_RECALL="${MIN_RECALL:-0.80}"
MIN_PRECISION="${MIN_PRECISION:-0.80}"
MIN_INJECTION="${MIN_INJECTION:-1.00}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run) RUN_MODE=1; shift ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

echo "=============================================================="
echo "  SmartReview CI 卡口"
echo "  后端：$BASE_URL   模式：$([[ $RUN_MODE == 1 ]] && echo '重新审查' || echo '复用已有任务')"
echo "  阈值：召回≥${MIN_RECALL}  精确≥${MIN_PRECISION}  注入≥${MIN_INJECTION}"
echo "=============================================================="

# ---------- 第 1 道：确定性单测 ----------
echo ""
echo "▶ [1/2] pytest"
if ! (cd "$ROOT/backend" && "$PYTHON" -m pytest -q); then
  echo "❌ 单测未通过，CI 卡口失败" >&2
  exit 1
fi

# ---------- 第 2 道：语义质量评测 ----------
echo ""
echo "▶ [2/2] 质量评测"
if ! curl -sf --noproxy '*' "$BASE_URL/health" >/dev/null; then
  echo "❌ 后端不可用：$BASE_URL（请先启动 uvicorn，或改用 --base-url）" >&2
  exit 2
fi

OUT_DIR="$ROOT/logs/eval"
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/ci-$(date +%Y%m%d-%H%M%S).json"

ARGS=(--base-url "$BASE_URL"
      --min-recall "$MIN_RECALL"
      --min-precision "$MIN_PRECISION"
      --min-injection "$MIN_INJECTION"
      --json-out "$OUT_FILE")
[[ $RUN_MODE == 1 ]] && ARGS+=(--run)

if ! (cd "$ROOT" && "$PYTHON" scripts/eval_all.py "${ARGS[@]}"); then
  echo "❌ 质量评测未达标，CI 卡口失败（指标已归档：$OUT_FILE）" >&2
  exit 1
fi

# ---------- 附加：holdout 泛化观测（不参与门禁） ----------
# 门禁只看"调优集"，holdout 是从未参与规则调整的样本，用来观察是否过拟合。
# 它失败不拦流水线，但会在日志里留下真实泛化水平，避免"100% 自我感动"。
echo ""
echo "▶ [附加] holdout 泛化观测（不参与门禁）"
HOLDOUT_DIR="$ROOT/samples/eval/holdout"
if compgen -G "$HOLDOUT_DIR/*.json" > /dev/null; then
  "$PYTHON" "$ROOT/scripts/eval_all.py" --base-url "$BASE_URL" \
    --dir "$HOLDOUT_DIR" --report-only \
    || echo "⚠️  holdout 评测未能完成（不影响门禁，请检查标注 task_id）" >&2
else
  echo "（未配置 holdout 标注，跳过）"
fi

echo "✅ CI 卡口全部通过"
