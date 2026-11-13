# =============================================================================
# 结构化日志 - logger.py
# =============================================================================
# 解决什么问题：23 处 print 散落各处，出问题时无法回答三个问题——
#   这次审查属于哪个任务？哪一条条款慢/失败？换了模型或规则后效果差异在哪？
# print 是"人看的流水"，结构化日志是"机器可查的证据"。
#
# 设计取舍：
#   1. 双通道输出——控制台保持人类可读的单行摘要（开发看），
#      JSONL 文件逐行一个 JSON（日志系统/评测脚本可解析）；
#   2. trace_id 显式传参而非依赖 contextvars——ThreadPoolExecutor 不会
#      自动复制上下文，显式传参比隐式传播更难出"日志串错任务"的事故；
#   3. 日志写入失败绝不影响业务：文件句柄异常一律吞掉并降级为控制台输出。
# =============================================================================

import contextvars
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 当前线程/协程的 trace_id：让跨层模块（如 llm client）无需层层传参也能打上 trace。
# 注意：ThreadPoolExecutor 不会自动复制上下文，调度器需在工作线程内显式 set。
_trace_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def set_trace_id(trace_id: str) -> None:
    """在当前执行上下文绑定 trace_id（工作线程内调用）。"""
    _trace_var.set(trace_id or "")


def get_trace_id() -> str:
    """读取当前上下文绑定的 trace_id；未绑定时返回空串。"""
    return _trace_var.get()

# 日志目录：backend/logs（与 backend/data 平级，便于单独挂载/清理）
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "app.jsonl"

# 允许用环境变量关掉文件写入（测试/容器只读文件系统时）
_FILE_ENABLED = os.getenv("SMARTREVIEW_LOG_FILE", "1") != "0"

_lock = threading.Lock()


class JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化为单行 JSON；自定义字段通过 record.__dict__ 透传。"""

    # 这些是 LogRecord 的内置属性，不属于业务字段，序列化时排除
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        # trace_id 兜底：调用方没显式传 extra 时，用当前上下文的绑定值，
        # 保证跨层日志（llm/rag）也能归到同一次审查
        if not getattr(record, "trace_id", None):
            tid = get_trace_id()
            if tid:
                payload["trace_id"] = tid
        # 业务字段：调用时以 extra={...} 传入，这里原样铺开，便于按字段检索
        for k, v in record.__dict__.items():
            if k not in self._RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """控制台摘要：时间 | 级别 | 事件 | 关键业务字段（trace/clause/channel）。"""

    _KEYS = ("trace_id", "clause_order", "channel", "provider",
             "latency_ms", "total_tokens", "risk_level", "error")

    def format(self, record: logging.LogRecord) -> str:
        base = (f"{datetime.fromtimestamp(record.created):%H:%M:%S} "
                f"{record.levelname:<7} [{record.name}] {record.getMessage()}")
        extras = []
        for k in self._KEYS:
            v = getattr(record, k, None)
            if v not in (None, ""):
                extras.append(f"{k}={v}")
        return base + (" | " + " ".join(extras) if extras else "")


_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    """初始化根日志器（应用启动时调用一次，幂等）。

    控制台走可读格式，文件走 JSONL；文件写入失败只告警一次，
    不影响服务可用性——日志是辅助设施，不能成为故障点。
    """
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger("smartreview")
    root.setLevel(level)
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)

    if _FILE_ENABLED:
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
            fh.setFormatter(JsonFormatter())
            root.addHandler(fh)
        except Exception as e:  # 只读文件系统等场景：降级为纯控制台
            root.warning("日志文件不可写，已降级为控制台输出", extra={"error": str(e)})

    # uvicorn 的访问日志过于嘈杂，统一收敛到 WARNING；
    # 业务日志由各模块自己的 logger 输出
    for noisy in ("uvicorn.access", "httpx", "openai", "chromadb"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """取得业务日志器（自动挂在 smartreview 根下，避免重复配置 handler）。"""
    setup_logging()
    return logging.getLogger(f"smartreview.{name}")


def new_trace_id(prefix: str = "rev") -> str:
    """生成一次审查的追踪 ID：前缀 + 毫秒时间 + 随机后缀，便于人眼排序与排查。"""
    import uuid
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


class Timer:
    """耗时统计器：构造即开始计时，ms 属性随时可读。

    之所以不做成"只在 __exit__ 才算出 ms"：审查链路里同一个计时器要在
    多个 return 分支（规则/LLM/兜底）读取耗时，用 property 动态计算最简单，
    也避免"忘了调 __exit__ 导致耗时永远是 0"这类静默 bug。
    仍兼容 with 用法（重新起算一次）。
    """

    def __init__(self):
        self._t0 = time.perf_counter()

    @property
    def ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 1)

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc) -> bool:
        return False  # 不吞异常


def read_trace(trace_id: str, limit: int = 500) -> list[dict]:
    """按 trace_id 从 JSONL 中回读该次审查的全部结构化日志。

    用流式逐行读而非一次性 load：日志文件会持续增长，
    单次查询只关心一个 trace 的几百行。
    """
    if not _LOG_FILE.exists():
        return []
    out: list[dict] = []
    try:
        with _LOG_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("trace_id") == trace_id:
                    out.append(obj)
                    if len(out) >= limit:
                        break
    except Exception:
        return []
    return out
