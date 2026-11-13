# =============================================================================
# 向量数据库模块 - ChromaVectorStore
# =============================================================================
# 复用 zhiqida-rag core/vectorstores.py 的 ChromaDB 适配器，并吸收 smartbrief
# v26 的改进：文档 ID 采用 md5 稳定 ID（source + 内容哈希），重建索引幂等去重，
# 避免旧版 `hash()+index` 导致的重建重复入库。
# =============================================================================

import hashlib
import os
from typing import List, Optional, Tuple

from config.settings import settings
from rag.loaders import Document
from rag.embeddings import Embeddings


def stable_id(source: Optional[str], content: str) -> str:
    """基于来源与内容的稳定 ID（跨重建不变，天然去重）。"""
    # 以 source+content 拼键哈希：同一文件重载/重建时 ID 不变，
    # 即可通过"已存在则跳过"实现幂等去重；source 为 None 时用空串兜底
    key = f"{source or ''}|{content}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


class ChromaVectorStore:
    """ChromaDB 持久化向量库：cosine 距离，md5 稳定 ID。"""

    def __init__(self, embeddings: Embeddings, persist_dir: Optional[str] = None,
                 collection_name: Optional[str] = None):
        """持久化目录与集合名缺省时回退到全局 settings。"""
        self.embeddings = embeddings
        self.persist_dir = persist_dir or str(settings.chroma_dir_path)
        self.collection_name = collection_name or settings.collection_name
        # 客户端/集合延迟初始化（首次操作时按需创建），见 _lazy_init
        self._client = None
        self._collection = None

    def _lazy_init(self):
        """延迟创建 ChromaDB 客户端与集合：未依赖 chromadb 时也能先导入本模块。"""
        if self._client is None:
            try:
                import chromadb
                from chromadb.config import Settings as ChromaSettings
            except ImportError:
                raise ImportError("chromadb 未安装，请执行: pip install chromadb")
            os.makedirs(self.persist_dir, exist_ok=True)
            self._client = chromadb.PersistentClient(
                path=self.persist_dir,
                # 关闭匿名遥测上报，数据不出内网
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            # get_or_create：不存在才创建、存在则复用 —— 重建索引幂等的前提
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                # 指定 cosine 空间：检索侧"1-distance"转相似度才有意义
                metadata={"hnsw:space": "cosine"},
            )

    def add_documents(self, documents: List[Document],
                      embeddings: Optional[List[List[float]]] = None) -> int:
        """批量入库；相同 md5 ID 的文档自动跳过。返回新增条数。"""
        self._lazy_init()
        if embeddings is None:
            # 未显式传向量时一次性批量嵌入（比逐条调用省一半网络往返）
            embeddings = self.embeddings.embed_documents([d.page_content for d in documents])

        ids = [stable_id(d.metadata.get("source"), d.page_content) for d in documents]

        # 先查库内已有 ID 再过滤：避免重复写入相同内容，跨进程/跨重启同样生效
        existing = self._collection.get(ids=ids)["ids"]
        existing_set = set(existing) if existing else set()

        new_ids, new_embeds, new_texts, new_metas = [], [], [], []
        for i, doc in enumerate(documents):
            if ids[i] in existing_set:
                continue
            new_ids.append(ids[i])
            new_embeds.append(embeddings[i])
            new_texts.append(doc.page_content)
            new_metas.append(doc.metadata)
        if new_ids:
            self._collection.add(ids=new_ids, embeddings=new_embeds,
                                 documents=new_texts, metadatas=new_metas)
        return len(new_ids)

    def similarity_search(self, query_embedding: List[float], k: int = 10,
                          where: Optional[dict] = None) -> List[Tuple[Document, float]]:
        """相似度检索：distance -> similarity = 1 - distance。"""
        self._lazy_init()
        results = self._collection.query(
            query_embeddings=[query_embedding], n_results=max(k, 1),
            where=where, include=["documents", "metadatas", "distances"],
        )
        docs: List[Tuple[Document, float]] = []
        if results["ids"] and results["ids"][0]:
            for i in range(len(results["ids"][0])):
                doc = Document(page_content=results["documents"][0][i],
                               metadata=results["metadatas"][0][i])
                # cosine 距离越小越相似，故用 1-distance 转成"越大越相关"的分数，
                # 与重排序、阈值过滤共用同一量纲
                docs.append((doc, 1 - results["distances"][0][i]))
        return docs

    def get_documents(self, where: Optional[dict] = None) -> List[dict]:
        """读取集合内记录（含 content/metadata），供知识库管理列表/详情使用。"""
        self._lazy_init()
        if self._collection.count() == 0:
            return []
        result = self._collection.get(where=where, include=["metadatas", "documents"])
        items: List[dict] = []
        ids = result.get("ids") or []
        for i in range(len(ids)):
            items.append({
                "id": ids[i],
                "content": result["documents"][i],
                "metadata": result["metadatas"][i] or {},
            })
        return items

    def delete_by_ids(self, ids: List[str]) -> None:
        """按稳定 ID 批量删除（知识库管理：删除单个文档的全部 chunk）。"""
        self._lazy_init()
        if ids:
            self._collection.delete(ids=ids)

    def count(self) -> int:
        """返回集合内记录总数（chunk 数）。"""
        self._lazy_init()
        return self._collection.count()

    def delete_collection(self):
        """删除整个集合并置空本地引用，使后续访问按需重建。"""
        self._lazy_init()
        self._client.delete_collection(self.collection_name)
        self._collection = None

    def get_stats(self) -> dict:
        """知识库统计：总量与来源分布。"""
        self._lazy_init()
        total = self._collection.count()
        if total == 0:
            return {"total": 0, "sources": {}}
        got = self._collection.get(limit=total, include=["metadatas"])
        sources: dict = {}
        for meta in got["metadatas"]:
            # 单条记录可能缺失 metadata（手写插入的历史数据），None 时归为 unknown
            src = (meta or {}).get("source", "unknown")
            sources[src] = sources.get(src, 0) + 1
        return {"total": total, "sources": sources}
