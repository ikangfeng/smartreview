# =============================================================================
# 嵌入模型模块 - Embeddings（复用 zhiqida-rag core/embeddings.py 实现）
# =============================================================================
# 通过 OpenAI 兼容的 Embeddings API 调用 bge-m3，无需本地 GPU。
# 工厂函数 create_embeddings() 保持签名不变，便于后续替换为本地模型。
# =============================================================================

from typing import List, Optional

from config.settings import settings


class Embeddings:
    """嵌入模型抽象基类。"""

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """子类实现：批量文本 → 向量列表（顺序与输入一一对应）。"""
        raise NotImplementedError

    def embed_query(self, text: str) -> List[float]:
        """子类实现：单条查询 → 向量（查询侧须与文档侧用同一模型/端点）。"""
        raise NotImplementedError


class SiliconFlowEmbeddings(Embeddings):
    """硅基流动 API 嵌入实现（兼容 OpenAI Embeddings 格式）。"""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None):
        """三类参数均可缺省回退 settings；优先级链见下方行内注释。"""
        # 回退链：显式参数 > 专用嵌入 Key > 硅基流动主 Key，
        # 允许只配一个主 Key 即可跑通全部功能
        self.api_key = api_key or settings.sf_embedding_api_key or settings.sf_api_key
        self.base_url = base_url or settings.embedding_base_url
        self.model = model or settings.embedding_model
        # 客户端延迟到首次调用再建（见 _lazy_init），避免模块导入即触发副作用
        self._client = None

    def _lazy_init(self):
        """延迟初始化 OpenAI 兼容客户端。"""
        if self._client is None:
            if not self.api_key:
                # 提前抛出带配置指引的异常，比 API 返回 401 更易排查
                raise RuntimeError(
                    "未配置嵌入 API Key。请在 backend/.env 设置 "
                    "SMARTREVIEW_SF_EMBEDDING_API_KEY（硅基流动）。")
            from openai import OpenAI
            # base_url 指向 OpenAI 兼容端点（如硅基流动 /v1），故可复用同一 SDK
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """批量文本向量化：按 index 重排，保证返回顺序与输入对齐。"""
        self._lazy_init()
        response = self._client.embeddings.create(model=self.model, input=texts)
        # 服务端返回顺序不保证与输入一致，必须先按 index 排序再取值，
        # 否则向量与文本错位会污染整个向量库
        sorted_data = sorted(response.data, key=lambda x: x.index)
        return [item.embedding for item in sorted_data]

    def embed_query(self, text: str) -> List[float]:
        """单条查询向量化（复用同一模型，确保与文档向量同分布可比）。"""
        self._lazy_init()
        response = self._client.embeddings.create(model=self.model, input=[text])
        return response.data[0].embedding


def create_embeddings() -> Embeddings:
    """工厂函数：创建嵌入模型实例（后续可扩展为本地嵌入实现）。"""
    return SiliconFlowEmbeddings()
