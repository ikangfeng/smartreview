# =============================================================================
# SQLite 基础连接与建表/种子脚本 - database.py
# =============================================================================
# 每次调用新建短连接（sqlite3 线程安全约束），配合 WAL 模式支持
# 并发审查任务（多线程落进度）与 API 读取并行。
# SCHEMA_SQL 集中定义全部表结构（含字段说明），init_db() 在此建表并完成
# 默认风控规则的幂等 seed；各仓储的 _ensure_table 仅作脱离入口时的兜底。
# =============================================================================

import sqlite3
from contextlib import contextmanager

from config.settings import settings


def connect() -> sqlite3.Connection:
    """打开一个新的 SQLite 连接（每次新建、用完即关，杜绝跨线程共享连接）。"""
    settings.ensure_dirs()
    conn = sqlite3.connect(str(settings.db_file_path), timeout=30)  # 写锁竞争时最多阻塞 30s 再抛异常
    conn.row_factory = sqlite3.Row    # 行按列名访问（row["status"]），语义比数字下标清晰
    conn.execute("PRAGMA journal_mode=WAL")   # WAL：读写不互斥，审查写进度与 API 读列表可并行
    conn.execute("PRAGMA foreign_keys=ON")    # SQLite 外键默认关闭且随连接生效，故每个新连接都须显式开启
    return conn


@contextmanager
def get_conn():
    """短连接上下文管理器：自动提交/回滚/关闭。"""
    conn = connect()
    try:
        yield conn
        conn.commit()      # 块内正常走完统一提交，调用方无需各自 commit
    except Exception:
        conn.rollback()    # 任一条 SQL 抛错即整体回滚，半截数据（如只删了子表）不会落库
        raise
    finally:
        conn.close()       # 短连接模式：无论成败都关闭，归还文件句柄并结束事务上下文


SCHEMA_SQL = """
-- 全部表结构集中于此，由 init_db() 统一执行；新表均用 IF NOT EXISTS，
-- 服务重启重复执行安全，也便于"脚本重建表结构、不动已有数据"的演进。

-- 审查任务主表：一行 = 一次上传文件的整单审查（任务维度记录）
CREATE TABLE IF NOT EXISTS review_tasks (
    task_id        TEXT PRIMARY KEY,          -- 全局 uuid：results 关联与前端轮询均以此为准
    file_id        TEXT,                      -- 上传文件存储标识（审查完成后按此前缀清理临时文件）
    filename       TEXT,                      -- 原始文件名（列表/报告展示用）
    contract_name  TEXT,                      -- 从文档头部提取的合同名称
    party_a        TEXT,                      -- 甲方（本产品以甲方立场审查）
    party_b        TEXT,                      -- 乙方（合同相对方）
    total_amount   TEXT,                      -- 合同总额原文（如"500万元"，保留单位，仅展示不计算）
    total_clauses  INTEGER DEFAULT 0,         -- 条款总数（进度分母；与 clauses_json 同源冗余，免反序列化取数）
    status         TEXT DEFAULT 'pending',    -- pending/running/completed/failed（任务状态机）
    current        INTEGER DEFAULT 0,         -- 已审条款数（进度分子，逐条完成时实时刷新）
    error          TEXT,                      -- 终态 failed 时的失败原因（供列表/详情展示）
    clauses_json   TEXT,                      -- 拆解出的条款全量快照（报告生成/归档复用，任务与上游文件解耦）
    full_text      TEXT,                      -- 合同全文（结果页"原文对照"左栏数据源，上传文件清理后仍可回溯）
    contract_issues TEXT,                     -- 合同完整性体检 JSON（合同类型 + 缺失条款/数值冲突/主体不一致）
    reasonableness_json TEXT,                 -- 合同级合理性评估 JSON（0-100 评分 + 结论 + 问题分布）
    summary_json   TEXT,                      -- 风险汇总 {"高":n,"中":n,"低":n,"无":n}（列表页免二次聚合）
    report_path    TEXT,                      -- 生成的 Word 报告落盘路径（供前端下载）
    playbook_id    INTEGER,                   -- 本次审查所用方案（review_playbooks.id）
    playbook_name  TEXT,                      -- 方案名称快照（列表/报告直接展示，避免连表）
    playbook_json  TEXT,                      -- 方案配置全量快照（方案后续修改不影响历史任务复现）
    masked_count   INTEGER DEFAULT 0,         -- 送审前脱敏的敏感信息处数（当事人/账号/证件号等）
    created_at     TEXT,                      -- 创建时间（列表默认倒序排序键）
    finished_at    TEXT                       -- 到达终态（completed/failed）的时间
);

-- 审计留痕：法务场景要求"谁、在什么时候、对哪份合同/哪条规则做了什么"可查。
-- 只追加不修改（append-only），detail_json 存变更前后值，便于追责与回放。
CREATE TABLE IF NOT EXISTS audit_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,                -- ISO 时间
    actor        TEXT DEFAULT '',              -- 操作人（演示环境取 X-Operator，缺省"演示用户"）
    action       TEXT NOT NULL,                -- task_created/disposition_updated/revision_applied/report_downloaded/...
    target_type  TEXT NOT NULL,                -- task/rule/playbook/llm/knowledge
    target_id    TEXT DEFAULT '',              -- 目标业务 ID（task_id / rule_id / playbook_id）
    task_id      TEXT DEFAULT '',              -- 关联审查任务（便于在结果页按任务查时间线）
    detail_json  TEXT DEFAULT '{}'             -- 结构化明细（旧值/新值/条款号等）
);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);

-- 审查方案（Playbook）：按“合同类型 + 立场 + 企业口径”组合必备条款清单、
-- 规则集、知识库范围与附加提示词；发起审查时选用，任务落库时做全量快照。
CREATE TABLE IF NOT EXISTS review_playbooks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,  -- 方案主键（审查任务/前端引用）
    name             TEXT NOT NULL,                      -- 方案名称（如“技术服务合同·甲方版”）
    contract_type    TEXT,                               -- 适用合同类型（与完整性检查的类型口径一致）
    stance           TEXT DEFAULT '甲方',                -- 审查立场：甲方/乙方/中立（注入提示词）
    description      TEXT,                               -- 方案说明（前端列表/详情展示）
    required_clauses TEXT,                               -- JSON 必备条款清单 [{name,keywords,severity,suggestion}]
    rule_ids         TEXT,                               -- JSON 规则 id 列表；空数组=全部启用规则
    kb_categories    TEXT,                               -- JSON 知识库范围 ["law","judicial","case","upload"]；空=不限
    extra_prompt     TEXT,                               -- 附加审查提示词（叠加到系统提示词之后）
    is_default       INTEGER DEFAULT 0,                  -- 同合同类型的默认方案（1=自动匹配时优先）
    enabled          INTEGER DEFAULT 1,                  -- 启用开关（0=不参与自动匹配，仍可用于历史查看）
    created_at       TEXT,
    updated_at       TEXT
);

-- 逐条审查结果表：与 review_tasks 一对多；未声明外键，删除时由仓储手动级联
CREATE TABLE IF NOT EXISTS review_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增主键，无业务含义，仅内部引用
    task_id          TEXT NOT NULL,                      -- 归属任务（按此查询/级联删除，配下方索引）
    clause_order     INTEGER,                            -- 条款在原合同中的先后序号（结果按此还原顺序）
    clause_id        TEXT,                               -- 条款标记原文（"第一条"/降级为"P1"），报告定位标题
    title            TEXT,                               -- 条款标题（条号后首句提取，可为 NULL）
    content          TEXT,                               -- 条款正文（规则/模型判定的输入依据）
    char_index       INTEGER,                            -- 条款标记在全文中的字符偏移（原文回溯定位）
    page             INTEGER,                            -- 条款起点所在页（PDF 逐页定位；Word/TXT 恒为 1）
    risk_level       TEXT,                               -- 高/中/低/无（汇总统计按此分组）
    risk_description TEXT,                               -- 风险说明（规则或模型通道输出）
    law_basis        TEXT,                               -- 援引的法条/检索片段（结论可追溯）
    suggestion       TEXT,                               -- 修改建议（随报告输出）
    confidence       REAL,                               -- 判定置信度 0~1（规则通道恒为 0.95）
    channel          TEXT,                               -- 来源通道 rule/llm/fallback（命中方式统计口径）
    quality_issues   TEXT,                               -- 文本质量问题 JSON 数组（错别字/歧义/格式/术语/数值冲突）
    reasonableness_issues TEXT,                          -- 商业合理性问题 JSON 数组（甲方视角：失衡/不对等/不可执行）
    revised_text     TEXT,                               -- 模型给出的改写后条款全文（修订对照报告数据源）
    disposition      TEXT DEFAULT '未处理',              -- 处置状态：未处理/已采纳/已修改/接受风险/已忽略
    disposition_note TEXT,                               -- 处置备注（法务说明为何接受/忽略或如何修改）
    disposition_at   TEXT,                               -- 最近一次处置时间
    created_at       TEXT                                -- 落库时间（=该条审查完成时刻）
);
CREATE INDEX IF NOT EXISTS idx_results_task ON review_results(task_id);  -- "按任务拉全部结果"是最高频查询，务必走索引

-- 风控规则表（P2 可配置）：确定性判断通道的数据源，由规则引擎 rules.py 消费
CREATE TABLE IF NOT EXISTS risk_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,     -- 自增主键（规则管理 CRUD 引用）
    name        TEXT NOT NULL,                         -- 规则名称（如"违约金/赔偿上限超标"）
    category    TEXT,                                  -- 分类标签（违约责任/知识产权/争议解决…）
    matcher_kind TEXT NOT NULL,                        -- percent_ceiling/ip_rights/keyword_any：规则引擎据此分发匹配器
    params_json TEXT,                                  -- 匹配参数（regex/threshold/keywords…，结构随 matcher_kind 而变）
    risk_level  TEXT NOT NULL,                         -- 命中后标记的等级（多规则命中时取严重度最高者）
    description TEXT,                                  -- 风险说明模板（命中时拼入 verdict 展示）
    suggestion  TEXT,                                  -- 修改建议模板
    enabled     INTEGER DEFAULT 1,                     -- 软开关：0=停用但仍保留行，随时可再启用
    updated_at  TEXT                                   -- 最近编辑时间
);

-- 运行时自定义 LLM 提供商（单行表 id 恒为 1；OpenAI 兼容，通常指向本地 vLLM 微调模型）
CREATE TABLE IF NOT EXISTS llm_profile (
    id          INTEGER PRIMARY KEY CHECK (id = 1),    -- CHECK(id=1) 在库层锁死"全局仅一份运行时配置"
    enabled     INTEGER DEFAULT 0,                     -- 1=启用自定义，0=按 .env 默认链(vllm→siliconflow)
    base_url    TEXT DEFAULT '',                       -- OpenAI 兼容端点（如 http://localhost:8000/v1）
    api_key     TEXT DEFAULT '',                       -- vLLM 通常无鉴权，可留空
    model       TEXT DEFAULT '',                       -- 调用的模型名
    updated_at  TEXT
);

-- 应用级键值元数据（当前用于记录"默认规则已完成 seed"，保证用户删除规则后
-- 重启不会因 risk_rules 为空而重新灌入内置规则）
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_db() -> None:
    """建表并幂等 seed 默认风控规则（仅首次 seed，用户删除后不复活）。"""
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)  # 多语句脚本一次执行；表均 IF NOT EXISTS，重启重复执行安全
        _migrate_schema(conn)           # 存量库补列（CREATE TABLE IF NOT EXISTS 不会给旧表加字段）
        # ---- 幂等 seed：判据是 app_meta 标记，而不是"规则表是否为空" ----
        seeded = conn.execute(
            "SELECT value FROM app_meta WHERE key='rules_seeded'").fetchone()
        if seeded is None:
            # 标记缺失才考虑 seed；标记一旦写入便永久保留——用户此后删光规则、
            # 重启时也走不到该分支，出厂规则不会"复活"
            from db.review_repository import DEFAULT_RULES
            now = _now()
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM risk_rules").fetchone()["n"]
            # 双保险：若上次 seed 中途崩溃（规则插了一部分但标记未落库），
            # count>0 则跳过插入只补标记，避免把同一批默认规则重复灌入
            if count == 0:
                for r in DEFAULT_RULES:
                    conn.execute(
                        "INSERT INTO risk_rules(name, category, matcher_kind, params_json, "
                        "risk_level, description, suggestion, enabled, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (r["name"], r["category"], r["matcher_kind"], r["params_json"],
                         r["risk_level"], r["description"], r["suggestion"], 1, now))
            # 用 upsert 而非裸 INSERT：即使标记行因并发/前序异常已存在也不抛唯一键冲突
            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES('rules_seeded','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'")
        # 单行模型配置兜底（后续由"模型配置"页更新）；INSERT OR IGNORE 保证不覆盖已保存的配置
        conn.execute(
            "INSERT OR IGNORE INTO llm_profile(id, enabled, base_url, api_key, model, updated_at) "
            "VALUES (1, 0, '', '', '', ?)", (_now(),))
        # ---- 幂等 seed：出厂审查方案（与规则同一套"一次性标记"策略）----
        pb_seeded = conn.execute(
            "SELECT value FROM app_meta WHERE key='playbooks_seeded'").fetchone()
        if pb_seeded is None:
            from db.playbook_repository import seed_default_playbooks
            seed_default_playbooks(conn)
            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES('playbooks_seeded','1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """存量库渐进式补列（幂等）。

    为什么需要：SCHEMA_SQL 全是 CREATE TABLE IF NOT EXISTS，对已存在的表
    不会追加新字段。旧库升级后 review_results 缺 quality_issues 会直接导致
    INSERT 列数不匹配而整条落库失败，故在此按列存在性做 ALTER TABLE。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(review_results)").fetchall()}
    if "quality_issues" not in cols:
        conn.execute("ALTER TABLE review_results ADD COLUMN quality_issues TEXT")

    # 任务级完整性体检结果：旧库同样缺列，补齐后 create_task 才不会列数失配
    task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(review_tasks)").fetchall()}
    if "contract_issues" not in task_cols:
        conn.execute("ALTER TABLE review_tasks ADD COLUMN contract_issues TEXT")
    if "reasonableness_json" not in task_cols:
        conn.execute("ALTER TABLE review_tasks ADD COLUMN reasonableness_json TEXT")
    if "full_text" not in task_cols:
        conn.execute("ALTER TABLE review_tasks ADD COLUMN full_text TEXT")
    # 审查方案快照：存量任务无来源方案，补列为 NULL（历史记录展示为“未使用方案”）
    if "playbook_id" not in task_cols:
        conn.execute("ALTER TABLE review_tasks ADD COLUMN playbook_id INTEGER")
    if "playbook_name" not in task_cols:
        conn.execute("ALTER TABLE review_tasks ADD COLUMN playbook_name TEXT")
    if "playbook_json" not in task_cols:
        conn.execute("ALTER TABLE review_tasks ADD COLUMN playbook_json TEXT")

    # 版本管理：同一合同经"修订→复审"形成版本链，root 标识同一合同族，
    # version 单调递增；parent 指向上一版，便于结果页展示"由哪一版修订而来"
    if "root_task_id" not in task_cols:
        conn.execute("ALTER TABLE review_tasks ADD COLUMN root_task_id TEXT")
    if "parent_task_id" not in task_cols:
        conn.execute("ALTER TABLE review_tasks ADD COLUMN parent_task_id TEXT")
    if "version" not in task_cols:
        conn.execute("ALTER TABLE review_tasks ADD COLUMN version INTEGER DEFAULT 1")
    if "masked_count" not in task_cols:
        conn.execute("ALTER TABLE review_tasks ADD COLUMN masked_count INTEGER DEFAULT 0")

    # 条款级合理性问题：与 quality_issues 同批落库，旧库需补列
    if "reasonableness_issues" not in cols:
        conn.execute("ALTER TABLE review_results ADD COLUMN reasonableness_issues TEXT")
    # 修订文本与处置闭环字段（P0：原文对照 / 修订建议 / 处置）
    if "revised_text" not in cols:
        conn.execute("ALTER TABLE review_results ADD COLUMN revised_text TEXT")
    if "disposition" not in cols:
        conn.execute("ALTER TABLE review_results ADD COLUMN disposition TEXT DEFAULT '未处理'")
    if "disposition_note" not in cols:
        conn.execute("ALTER TABLE review_results ADD COLUMN disposition_note TEXT")
    if "disposition_at" not in cols:
        conn.execute("ALTER TABLE review_results ADD COLUMN disposition_at TEXT")
    # 用户手动修改的改写稿（选"已修改"时存用户自己编辑的版本，合并时优先用）
    if "custom_revised_text" not in cols:
        conn.execute("ALTER TABLE review_results ADD COLUMN custom_revised_text TEXT")

    # 条款级 LLM token 用量：成本核算的数据源（规则通道该行留 NULL）
    for col in ("prompt_tokens", "completion_tokens", "llm_provider"):
        if col not in cols:
            conn.execute(f"ALTER TABLE review_results ADD COLUMN {col} "
                         + ("INTEGER DEFAULT 0" if col.endswith("tokens") else "TEXT"))

    # 任务级成本汇总：报告页直接读列展示，免去每次 SUM 聚合
    for col in ("total_prompt_tokens", "total_completion_tokens"):
        if col not in task_cols:
            conn.execute(f"ALTER TABLE review_tasks ADD COLUMN {col} INTEGER DEFAULT 0")

    # 灰度发布：记录本次任务/条款由哪个变体产出，供 A/B 对比与回溯
    if "llm_variant" not in task_cols:
        conn.execute("ALTER TABLE review_tasks ADD COLUMN llm_variant TEXT DEFAULT 'baseline'")
    if "llm_variant" not in cols:
        conn.execute("ALTER TABLE review_results ADD COLUMN llm_variant TEXT DEFAULT 'baseline'")


def _now() -> str:
    """统一时间戳格式 'YYYY-MM-DD HH:MM:SS'（TEXT 定长，字符串比较即时间序）。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
