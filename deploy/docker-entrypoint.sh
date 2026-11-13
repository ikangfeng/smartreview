#!/usr/bin/env sh
# =============================================================================
# SmartReview 后端容器入口
#   1) 幂等构建法律知识库索引（md5 稳定 ID，重复执行自动跳过）
#   2) 执行 CMD（uvicorn）
# 语料为空 / 嵌入 Key 缺失时跳过索引，不影响服务启动。
# =============================================================================
set -e

echo "[entrypoint] 幂等构建法律知识库索引（backend/data/kb）..."
if python seed_kb.py; then
    echo "[entrypoint] 知识库索引就绪。"
else
    echo "[entrypoint] 索引跳过或失败（语料为空 / 嵌入 Key 未配置？），服务照常启动。"
fi

echo "[entrypoint] 启动 SmartReview 后端..."
exec "$@"
