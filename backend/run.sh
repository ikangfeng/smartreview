#!/usr/bin/env bash
# =============================================================================
# SmartReview 后端守护脚本
# =============================================================================
# 用法:
#   bash backend/run.sh start    # 启动（已运行则跳过）
#   bash backend/run.sh stop     # 停止
#   bash backend/run.sh restart  # 重启
#   bash backend/run.sh status   # 查看状态
#   bash backend/run.sh tail     # 实时看日志
# =============================================================================

BACKEND_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$BACKEND_DIR/.uvicorn.pid"
LOG_DIR="$BACKEND_DIR/../logs"
LOG_FILE="$LOG_DIR/backend.log"
PORT=8002
HEALTH_URL="http://127.0.0.1:${PORT}/health"

mkdir -p "$LOG_DIR"

_alive() {
    # 返回 0=存活健康, 非零=不存活
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null) || true
    [ -z "$pid" ] && return 1
    kill -0 "$pid" 2>/dev/null || return 1
    # 健康检查（忽略 curl/grep 失败码）
    local ok
    ok=$(curl -s --max-time 3 "$HEALTH_URL" 2>/dev/null | grep -c '"status":"ok"') || true
    [ "$ok" -gt 0 ]
}

cmd_start() {
    if _alive; then
        echo "[OK] SmartReview already running (PID=$(cat "$PID_FILE"), port $PORT)"
        return 0
    fi
    rm -f "$PID_FILE"

    echo "[..] Starting SmartReview backend on port $PORT ..."
    cd "$BACKEND_DIR" || { echo "[FAIL] cannot cd $BACKEND_DIR"; return 1; }

    # 加载本地私有覆盖配置（.env.local，被 gitignore 忽略）
    if [ -f "$BACKEND_DIR/.env.local" ]; then
        echo "[..] Loading .env.local ..."
        # shellcheck disable=SC1091
        source "$BACKEND_DIR/.env.local"
    fi

    # & 后台 + disown 脱离 shell 会话，防止父 shell 退出时 SIGHUP 杀进程
    nohup python -X faulthandler -u -m uvicorn main:app \
        --host 0.0.0.0 \
        --port "$PORT" \
        --log-level info \
        >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    disown "$pid" 2>/dev/null || true

    # 健康检查循环：用子 shell 避免 pipefail 影响主脚本
    local i
    for i in $(seq 1 30); do
        sleep 1
        if _alive; then
            echo "[OK] Started (PID=$pid, ${i}s)"
            return 0
        fi
    done
    echo "[FAIL] Start timeout. Log: $LOG_FILE"
    return 1
}

cmd_stop() {
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null) || true
    if [ -z "$pid" ]; then
        pid=$(lsof -ti ":$PORT" 2>/dev/null | head -1) || true
    fi
    if [ -z "$pid" ]; then
        echo "[OK] Not running"
        rm -f "$PID_FILE"
        return 0
    fi
    echo "[..] Stopping PID=$pid ..."
    kill "$pid" 2>/dev/null || true
    local _
    for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "[OK] Stopped"
}

cmd_status() {
    if _alive; then
        echo "[OK] Running (PID=$(cat "$PID_FILE"), port $PORT)"
        curl -s "$HEALTH_URL" 2>/dev/null | python3 -m json.tool 2>/dev/null || true
    else
        echo "[FAIL] Not running"
    fi
}

cmd_tail() {
    tail -f "$LOG_FILE"
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

case "${1:-}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    tail)    cmd_tail ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|tail}"
        exit 1
        ;;
esac
