# =============================================================================
# 运行时自定义 LLM 提供商配置仓储 - model_config_repository.py
# =============================================================================
# 与 .env 静态配置互补：允许用户在"模型配置"页运行时维护一个
# OpenAI 兼容端点（典型场景 = 本地 vLLM 部署的微调模型），
# 持久化于 SQLite 单行表 llm_profile（id=1），进程内即时生效。
# 对外写入口仅 save_profile 一处：单行表只有"整行覆盖"语义，
# 不提供 insert/delete，配合 CHECK(id=1) 从入口与库两层守住"全局仅一份"。
# =============================================================================

from datetime import datetime
from typing import Optional

from db.database import get_conn

# 空配置的规范形态（未启用/未保存时返回它）
EMPTY_PROFILE = {
    "enabled": False,
    "base_url": "",
    "api_key": "",
    "model": "",
    "updated_at": None,
}

# 灰度（canary）空配置：与主配置同表不同列，保持"全局仅一份"
EMPTY_CANARY = {
    "enabled": False,
    "percent": 0,
    "base_url": "",
    "api_key": "",
    "model": "",
}


def _now() -> str:
    """统一时间戳格式 'YYYY-MM-DD HH:MM:SS'（与 database.py 一致）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_table() -> None:
    """幂等建表：仓储可脱离 init_db 独立使用（便于测试/迁移）。"""
    with get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_profile ("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"  # CHECK(id=1) 在库层锁死"单行表"不变量
            " enabled INTEGER DEFAULT 0,"
            " base_url TEXT DEFAULT '',"
            " api_key TEXT DEFAULT '',"
            " model TEXT DEFAULT '',"
            " updated_at TEXT)"
        )
        # 预置 id=1 空行：此后 save_profile 用 UPDATE 必然命中一行，
        # 保存逻辑无需再写"不存在则 INSERT"的分支
        conn.execute(
            "INSERT OR IGNORE INTO llm_profile(id, enabled, base_url, api_key, model, updated_at) "
            "VALUES (1, 0, '', '', '', ?)", (_now(),))
        # 灰度列渐进式补齐：存量库没有这些列，ALTER 幂等追加；
        # 放在同一张单行表里，避免再引入一张"全局仅一份"的表
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(llm_profile)").fetchall()}
        for col, ddl in (
            ("canary_enabled", "INTEGER DEFAULT 0"),
            ("canary_percent", "INTEGER DEFAULT 0"),
            ("canary_base_url", "TEXT DEFAULT ''"),
            ("canary_api_key", "TEXT DEFAULT ''"),
            ("canary_model", "TEXT DEFAULT ''"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE llm_profile ADD COLUMN {col} {ddl}")


def get_profile() -> dict:
    """读取当前自定义 LLM 配置；无记录时返回 EMPTY_PROFILE。"""
    _ensure_table()   # 读前先确保表与单行存在：仓储可独立运行，不必依赖 init_db 先执行
    with get_conn() as conn:
        row = conn.execute(
            "SELECT enabled, base_url, api_key, model, updated_at "
            "FROM llm_profile WHERE id = 1").fetchone()
    if row is None:
        # 仅当单行被外部手工删除时兜底（正常流程下 _ensure_table 已保证行存在）
        return dict(EMPTY_PROFILE)
    return {
        "enabled": bool(row["enabled"]),   # SQLite 存 0/1 整数 → 转 bool，返回结构对业务更友好
        "base_url": row["base_url"] or "",
        "api_key": row["api_key"] or "",
        "model": row["model"] or "",
        "updated_at": row["updated_at"],
    }


def save_profile(enabled: bool, base_url: str, api_key: str, model: str) -> dict:
    """保存配置并返回完整记录。所见即所得：api_key 以入参为准，空字符串即清除。"""
    _ensure_table()  # 先保证 id=1 行存在，否则下方 UPDATE 会静默更新 0 行（不报错但等于没存）
    with get_conn() as conn:
        # 单行全量覆盖写，而非字段级合并：表单提交的即最终态，无增量补丁语义
        conn.execute(
            "UPDATE llm_profile SET enabled=?, base_url=?, api_key=?, model=?, updated_at=? "
            "WHERE id = 1",
            (1 if enabled else 0, base_url, api_key, model, _now()))
    # 重新读回再返回：保证返回值与库中真实状态一致，调用方不必拿入参自行拼装
    return get_profile()


def mask_api_key(api_key: str) -> str:
    """脱敏展示：sk-abc...wxyz；短 Key 直接打码。"""
    if not api_key:
        return ""
    if len(api_key) <= 10:
        return "******"  # 过短的 Key 首尾各留几位已无辨识意义，整体打码
    return f"{api_key[:6]}****{api_key[-4:]}"  # 保留首 6/尾 4 便于辨认是哪把 Key，中间全部隐藏


def get_canary() -> dict:
    """读取灰度发布配置；未配置时返回 EMPTY_CANARY。"""
    _ensure_table()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT canary_enabled, canary_percent, canary_base_url, canary_api_key, canary_model "
            "FROM llm_profile WHERE id = 1").fetchone()
    if row is None:
        return dict(EMPTY_CANARY)
    percent = int(row["canary_percent"] or 0)
    return {
        "enabled": bool(row["canary_enabled"]),
        "percent": min(100, max(0, percent)),  # 库层兜底：脏数据也不能出现 >100% 的分流
        "base_url": row["canary_base_url"] or "",
        "api_key": row["canary_api_key"] or "",
        "model": row["canary_model"] or "",
    }


def save_canary(enabled: bool, percent: int, base_url: str, api_key: str, model: str) -> dict:
    """保存灰度配置并返回规范化后的记录（percent 收敛到 0-100）。"""
    _ensure_table()
    percent = min(100, max(0, int(percent or 0)))
    with get_conn() as conn:
        conn.execute(
            "UPDATE llm_profile SET canary_enabled=?, canary_percent=?, canary_base_url=?, "
            "canary_api_key=?, canary_model=?, updated_at=? WHERE id = 1",
            (1 if enabled else 0, percent, base_url, api_key, model, _now()))
    return get_canary()
