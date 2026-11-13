# =============================================================================
# 检索器模块 - Retrievers & Reranker
# =============================================================================
# 复用 zhiqida-rag core/retrievers.py 的向量检索 + 重排序两级结构。
# 与源实现的差异：
#   - 删除 BM25 检索（中文按单字切分效果差），保留语义向量检索为主体；
#   - 可选 Reranker：API 可用走 SiliconFlowReranker，否则降级本地词重叠。
# =============================================================================

import re
from typing import List, Optional, Tuple

from config.settings import settings
from rag.loaders import Document
from rag.embeddings import Embeddings
from rag.vectorstores import ChromaVectorStore


class VectorRetriever:
    """向量检索器：query 语义嵌入 → 向量库相似度 → 阈值过滤 → 降序返回。"""

    def __init__(self, vectorstore: ChromaVectorStore, embeddings: Embeddings,
                 k: int = 5, threshold: float = 0.4):
        """k 为候选数，threshold 过滤低相关噪声（低于它的命中不进 Prompt）。"""
        self.vectorstore = vectorstore
        self.embeddings = embeddings
        self.k = k
        self.threshold = threshold

    def retrieve_with_scores(self, query: str, where: Optional[dict] = None) -> List[Tuple[Document, float]]:
        """语义检索 → 阈值过滤 → 按相似度降序，返回 (Document, score) 列表。"""
        query_embedding = self.embeddings.embed_query(query)
        results = self.vectorstore.similarity_search(query_embedding, k=self.k, where=where)
        # 低于阈值的命中通常是弱相关噪声，混入会稀释 Prompt 中法条引用的质量
        filtered = [(doc, score) for doc, score in results if score >= self.threshold]
        # 降序：最相关的排最前，便于上游截断取 Top-K
        filtered.sort(key=lambda x: x[1], reverse=True)
        return filtered

    def retrieve(self, query: str, where: Optional[dict] = None) -> List[Document]:
        """仅返回命中文档（丢弃分数），供 Prompt 组装直接消费。"""
        return [doc for doc, _ in self.retrieve_with_scores(query, where=where)]


def _tokenize(text: str) -> set:
    """本地词重叠重排序使用的粗分词（字母数字块 + 中文字符）。"""
    # 正则两分支各司其职：英文/数字按连续块整体分词；
    # 中文无空格分隔、按单字切（单字是最小语义单元），
    # 二者并集即可支撑词重叠粗排序，无需引入重型分词库
    return set(re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fa5]", text.lower()))


class Reranker:
    """本地重排序器（词重叠/Jaccard），无网络依赖的降级方案。"""

    def __init__(self, k: int = 3):
        """k：重排后保留的文档数。"""
        self.k = k

    def rerank(self, query: str, documents: List[Document]) -> List[Document]:
        """按 query 与文档的词重叠度(Jaccard)降序，取前 k 个。"""
        if len(documents) <= self.k:
            # 候选本就不足 k：重排不会带来增益，直接返回省去无谓计算
            return documents
        query_tokens = _tokenize(query)
        scored = []
        for doc in documents:
            # Jaccard = 交集/并集：以并集归一化，抵消长文本"词多易命中"的偏差
            union = query_tokens | _tokenize(doc.page_content)
            score = len(query_tokens & _tokenize(doc.page_content)) / max(len(union), 1)
            scored.append((doc, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [doc for doc, _ in scored[:self.k]]


class SiliconFlowReranker(Reranker):
    """硅基流动 Reranker API 实现；调用失败自动降级为本地词重叠。"""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None, k: int = 3, timeout: Optional[float] = None):
        """Reranker 参数均可缺省回退 settings；Key/端点/模型回退见下方行内注释。"""
        # Reranker 复用 SF 主 key（sf_api_key），未配置时用嵌入 key 兜底
        self.api_key = api_key or settings.sf_api_key or settings.sf_embedding_api_key
        # 支持独立 rerank 端点（rerank_base_url），留空回落主 SF base
        self.base_url = base_url or settings.rerank_base_url or settings.sf_base_url
        self.model = model or settings.rerank_model
        self.k = k
        # timeout 显式传入时优先，否则用 settings 中的秒数（网络抖动不至于拖垮审查主链）
        self.timeout = timeout if timeout is not None else settings.rerank_timeout_seconds

    def rerank(self, query: str, documents: List[Document]) -> List[Document]:
        """调用远程 Reranker API；任一步失败均降级为本地词重叠重排。"""
        if not documents or len(documents) <= self.k:
            return documents
        try:
            import httpx
            # rstrip('/') 兼容 base_url 配置里末尾多余斜杠，避免拼出 "//rerank"
            url = f"{self.base_url.rstrip('/')}/rerank"
            resp = httpx.post(
                url,
                json={"model": self.model, "query": query,
                      "documents": [d.page_content for d in documents]},
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            # API 以 index 引用原始文档下标，须按 index 回取，不能按返回顺序对位；
            # index 边界校验防御异常响应导致越界崩溃
            scored = [(documents[item["index"]], item["relevance_score"])
                      for item in data.get("results", [])
                      if 0 <= item.get("index", -1) < len(documents)]
        except Exception:
            # 网络/Key/解析任一环节异常：整体降级为本地词重叠，保证检索链路不中断
            return super().rerank(query, documents)
        # relevance_score 越大越相关，降序后截取前 k 个
        scored.sort(key=lambda x: x[1], reverse=True)
        return [doc for doc, _ in scored[:self.k]]


def create_reranker() -> Reranker:
    """工厂：配置启用且具备 API Key 时使用 SiliconFlowReranker，否则本地降级。"""
    if settings.rerank_enabled and (settings.sf_api_key or settings.sf_embedding_api_key):
        return SiliconFlowReranker(k=settings.rerank_top_k)
    return Reranker(k=settings.rerank_top_k)
