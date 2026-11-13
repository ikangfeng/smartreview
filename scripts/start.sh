#!/usr/bin/env bash
# ===== SmartReview 一键启动（Linux/macOS）=====
# 用法：先按 backend/.env.example 配置 backend/.env，再执行本脚本。

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[1/3] 启动后端 FastAPI（8002）..."
(
  cd "$SCRIPT_DIR/../backend"
  # .env.local 存放真实 API Key（不入库）；存在则注入，避免只用占位符的 .env 启动
  if [ -f .env.local ]; then set -a; . ./.env.local; set +a; fi
  nohup python -m uvicorn main:app --host 0.0.0.0 --port 8002 > /tmp/smartreview-backend.log 2>&1 &
)
echo "  后端日志：/tmp/smartreview-backend.log"

echo "[2/3] 安装前端依赖（首次）..."
(cd "$SCRIPT_DIR/../frontend" && [ -d node_modules ] || npm install)

echo "[3/3] 启动前端 Vite（5174）..."
(cd "$SCRIPT_DIR/../frontend" && npm run dev)
