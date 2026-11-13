# =============================================================================
# 全局配置模块 - Application Settings
# =============================================================================
# 采用 Pydantic Settings 统一管理配置：
#   1. 自动从环境变量读取（env_prefix="SMARTREVIEW_"）
#   2. 支持 .env 文件（backend/.env）
#   3. 所有路径锚定到 backend 根目录，避免工作目录变化导致找不到数据
#
# 取值优先级：真实环境变量 > backend/.env > 代码内默认值（Pydantic 自动合并）。
# 全模块共享文件末尾的唯一 settings 单例——配置只收敛到一处，任何模块
# 不要自行 os.environ 读取，否则改配置时会出现"多处漂移不一致"。
#
# 环境变量前缀：SMARTREVIEW_（示例：SMARTREVIEW_API_PORT=8002）
# =============================================================================

from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend 根目录：本文件位于 backend/config/ 下（__file__ 上级的上级）
BACKEND_DIR = Path(__file__).resolve().parent.parent
# .env 固定放 backend/ 下，随代码库管理；换机器只需复制一份即可改全套配置
ENV_FILE = BACKEND_DIR / ".env"


class Settings(BaseSettings):
    """SmartReview 全局配置。

    为何集中成一个类：让"哪些开关可调"在 IDE 里一处可见、可搜索；
    Pydantic 在实例化时完成 env/.env/默认值合并与类型校验，字段级
    注释即配置文档。
    """

    model_config = SettingsConfigDict(
        env_prefix="SMARTREVIEW_",  # 环境变量前缀，防与机器上其他变量冲突
        env_file=str(ENV_FILE),
        extra="ignore",  # 无关环境变量不报错，避免带 SMARTREVIEW_ 前缀的其他用途变量干扰
    )

    # ===== 服务 =====
    # 0.0.0.0 表示监听所有网卡：后端可能被局域网内前端/联调机访问，
    # 仅 127.0.0.1 会挡掉这类请求；端口 8002 避开本地 vLLM 的 8001 与常见 8000。
    api_host: str = "0.0.0.0"
    api_port: int = 8002

    # ===== 数据路径（锚定 backend 根目录）=====
    # 统一落在 backend/data 下，随项目整体迁移/备份；属性 *_path 提供
    # 解析后的绝对 Path（见文末只读属性段）
    data_dir: str = "data"
    upload_dir: str = "data/uploads"
    report_dir: str = "data/reports"
    kb_dir: str = "data/kb"
    chroma_persist_dir: str = "data/chroma_db"
    db_path: str = "data/smartreview.db"

    # ===== LLM 提供商（vLLM 本地优先，SiliconFlow 云端降级）=====
    vllm_base_url: str = "http://127.0.0.1:8001/v1"
    vllm_api_key: str = ""  # 本地 vLLM 默认不校验 Key，留空即可
    vllm_model: str = "Qwen2.5-14B-Instruct"  # .env 兜底；微调就绪后由 DB 配置注入覆盖

    sf_base_url: str = "https://api.siliconflow.cn/v1"
    sf_api_key: str = ""  # 留空 = 云端不参与降级（client 构建链时整段跳过）
    sf_model: str = "Qwen/Qwen2.5-14B-Instruct"

    # 提供商轮次顺序（逗号分隔，最终按 provider_names 属性解析）：
    # 本地 vLLM 在前、云端在后，调用按序探测、失败自动降级
    #（多 provider 容错路由的完整动机与策略见 llm/client.py 模块头）。
    llm_providers: str = "vllm,siliconflow"
    # 温度 0.1：审查裁决要求"稳定可复现"——同一条款不应因采样随机性得出
    # 不同风险结论；取接近 0 的低值而非 0，保留微量多样性防完全贪心退化。
    llm_temperature: float = 0.1
    # 单条款 JSON 裁决含风险描述/法条引用/修改建议，长条款需预留足够
    # 输出空间，1200 token 覆盖典型长度并留余量。
    llm_max_tokens: int = 1200
    # 单次请求超时（秒）：本地微调模型首次请求可能含权重加载、长上下文
    # 生成也偏慢，故阈值放宽；它是"最后护栏"，配合 llm/client 的
    # max_retries=0 + provider 级降级策略（重试不在内层做，见 client 模块头）。
    # 同时被 registry.probe_llm 用作探测默认超时。
    llm_timeout_seconds: int = 60

    # ===== 成本核算单价（元 / 1K tokens）=====
    # 默认按硅基流动 Qwen2.5-14B-Instruct 的量级估算；换成自建 vLLM 时
    # 把两项都设 0 即可（本地推理只有算力成本，无按 token 计费）。
    # 单价刻意做成配置而非硬编码：售前现场客户问"多少钱一份"，改这两个
    # 数字就能给出这家客户自己的报价，不用改代码。
    llm_price_input_per_1k: float = 0.0007
    llm_price_output_per_1k: float = 0.0028

    # ===== 嵌入 / 重排序（通过硅基流动 API）=====
    # 注意与对话/降级用的 sf_api_key 分开：Chat 与 Embedding/Rerank 常分属
    # 不同账户/计费项目，独立 Key 可单独轮换、互不牵连，也避免把大模型
    # Key 暴露给仅需做向量化的调用链路。
    sf_embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    # 独立重排序端点（留空则回落 sf_base_url）；Reranker 也复用 SF key
    rerank_base_url: str = ""
    # 重排只处理少量短块文本，无需沿用审查的 60s 长超时；独立短阈值保证
    # 精排环节拖沓/失败时不会拖慢主链路（调用失败由 retrievers 侧降级为
    # 本地词重叠重排兜底，见 rag/retrievers.py）。
    rerank_timeout_seconds: int = 30
    # 向量召回后精排，仅保留进入 Prompt 的法条块数：太多会稀释注意力并
    # 撑爆上下文，3 块是"相关性 vs 输入长度"的折中（召回候选量见 top_k）。
    rerank_top_k: int = 3

    # ===== 分块 =====
    # 法律文本入库前按字符切成块（RecursiveChunker 复用，见 rag/store.py）。
    chunk_size: int = 512
    # 相邻块重叠 64 字符：被切在中间的句/词在两边各留一份，检索时不因
    # 恰好被截断而漏掉关键表述。
    chunk_overlap: int = 64

    # ===== 检索 =====
    # 每条款召回 5 条候选（粗召回，宽进）；真正入 Prompt 的数量由
    # rerank_top_k 精排后决定——先宽后精，避免单靠向量分数一刀切。
    top_k: int = 5
    # 向量相似度下限，过滤明显无关块。不宜设太高：法条经改写/转述后
    # 字面相似度天然偏低，阈值过严会漏检真实相关条文。
    similarity_threshold: float = 0.4
    collection_name: str = "smartreview_law"

    # ===== 审查调度 =====
    # 审查任务本质是"等待 LLM 网络响应"（IO 型），线程池 8 路并发可在
    # 不显著增加 CPU 压力的前提下提升整份合同的吞吐；具体见 scheduler.py。
    workers: int = 8
    # 单条款送入 LLM 的内容上限：条款过长先截断再加标注，防上下文超限
    # 与成本失控（见 review/scheduler.py）。
    max_clause_chars: int = 2000
    upload_max_mb: int = 20

    # ===== 企业风控默认底线（风控规则表为空时作为兜底 seed 值）=====
    default_penalty_percent_ceiling: float = 20.0

    # ---- 路径解析（只读属性）----
    # 配置文件里只存相对字符串，使用时统一经 *_path 转成绝对 Path：
    # 调用方拿到的是"真实文件系统位置"，不会因 cwd 不同而定位错目录。

    @property
    def upload_dir_path(self) -> Path:
        """上传文件的落盘目录（uploads/）。"""
        return (BACKEND_DIR / self.upload_dir).resolve()

    @property
    def report_dir_path(self) -> Path:
        """生成的 Word 审查报告输出目录（reports/）。"""
        return (BACKEND_DIR / self.report_dir).resolve()

    @property
    def kb_dir_path(self) -> Path:
        """知识库源文件目录（kb/）：启动时把这里面的 .md/.txt/.docx 灌入 Chroma。"""
        return (BACKEND_DIR / self.kb_dir).resolve()

    @property
    def chroma_dir_path(self) -> Path:
        """Chroma 向量库持久化目录：向量落盘于此，重启后无需重建。"""
        return (BACKEND_DIR / self.chroma_persist_dir).resolve()

    @property
    def db_file_path(self) -> Path:
        """SQLite 数据库文件位置（app.db）。"""
        return (BACKEND_DIR / self.db_path).resolve()

    @property
    def provider_names(self) -> List[str]:
        """按配置顺序返回提供商名列表，如 ['vllm', 'siliconflow']。

        用属性解析而非字段：llm_providers 是"用户可写的原始字符串"，
        此处统一做去空格、小写化与去空项，消费方（client 构建候选链）
        拿到的永远是规整列表，不必各自处理脏输入。
        """
        return [name.strip().lower() for name in self.llm_providers.split(",") if name.strip()]

    def ensure_dirs(self) -> None:
        """确保运行所需的本地目录存在（启动早期调用一次，见 main.lifespan）。

        为什么启动就建：data/uploads、data/kb 等若缺失，后续上传/入库/写
        报告会在运行中途才抛 FileNotFoundError；提前幂等建齐可让错误
        "早暴露、早提示"，而不是在业务处理到一半时炸出来。
        """
        for p in (self.upload_dir_path, self.report_dir_path, self.kb_dir_path,
                  self.chroma_dir_path, self.db_file_path.parent):
            p.mkdir(parents=True, exist_ok=True)


# 模块级单例：导入时完成一次 env/.env 合并与校验，此后全应用共享同一实例
#（import 同一对象），避免各模块重复解析配置导致取值不一致。测试或特殊
# 场景需要不同配置时，可另行构造 Settings 实例覆盖。
settings = Settings()
