# =============================================================================
# 法律知识库门面 - store.py（参照 smartbrief rag/store.py 的门面模式）
# =============================================================================
# 对上层（审查调度 / API）屏蔽"加载→分块→向量化→入库→检索"的组装细节。
# 职责：
#   - build_index()     从 data/kb 幂等构建/增量更新索引（md5 去重）
#   - retrieve()        语义检索 Top-K（可加重排序），异常时优雅降级为空
#   - upload/list/get/delete   知识库管理（对应前端"知识库"页）
#   - search()          知识库语义检索测试（/knowledge/search）
#   - stats()/clear()   知识库统计与清空
# 惰性初始化：向量库/检索器在首次使用时才创建，避免无 Key/无语料时启动失败。
# =============================================================================

import os
from pathlib import Path
from typing import List, Optional

from config.settings import settings
from rag.loaders import Document, DocumentLoader
from rag.chunkers import RecursiveChunker, LawArticleChunker
from rag.embeddings import Embeddings, create_embeddings
from rag.vectorstores import ChromaVectorStore
from rag.retrievers import VectorRetriever, create_reranker

# ---- 法律知识库分类（知识库管理页的筛选维度，参照 smartbrief 的分类思路）----
KB_CATEGORIES = ("law", "judicial", "case", "upload")
CATEGORY_LABELS = {
    "law": "法律法规",
    "judicial": "司法解释",
    "case": "指导案例",
    "upload": "上传文档",
}
# 上传文件统一落在 data/kb/uploads/，与预置语料同库管理、可被 build_index 重建
UPLOAD_DIR_NAME = "uploads"


def _category_of(source: str, meta_category: Optional[str] = None) -> str:
    """确定 chunk 归属的分类：优先已入库的 category 元数据，否则按语料所在子目录推断。"""
    if meta_category in KB_CATEGORIES:
        return meta_category
    parent = Path(source or "").parent.name
    if parent in KB_CATEGORIES:
        return parent
    if parent == UPLOAD_DIR_NAME:
        return "upload"
    return "law"


class LawKB:
    """法律知识库门面：smartreview_law 集合（民法典/司法解释/指导案例等）。"""

    def __init__(self):
        """三个组件句柄先置空，由下方 _get_* 在首次使用时惰性创建。"""
        self._embeddings: Optional[Embeddings] = None
        self._vectorstore: Optional[ChromaVectorStore] = None
        self._retriever: Optional[VectorRetriever] = None

    # ---- 组件惰性初始化 ----
    # 惰性意义：应用启动时不触网、不建库；缺 Key / 无语料的环境照常可用，
    # 直到真正检索/入库才按需实例化（对应模块头的设计说明）
    def _get_embeddings(self) -> Embeddings:
        """首次调用时经工厂创建嵌入实例，之后复用缓存。"""
        if self._embeddings is None:
            self._embeddings = create_embeddings()
        return self._embeddings

    def _get_store(self) -> ChromaVectorStore:
        """首次调用时创建向量库（内部依赖 embeddings，故先取嵌入实例）。"""
        if self._vectorstore is None:
            self._vectorstore = ChromaVectorStore(embeddings=self._get_embeddings())
        return self._vectorstore

    def _get_retriever(self) -> VectorRetriever:
        """首次调用时按 settings 组装检索器（k 与阈值取全局配置）。"""
        if self._retriever is None:
            store = self._get_store()
            self._retriever = VectorRetriever(
                vectorstore=store,
                embeddings=self._get_embeddings(),
                k=settings.top_k,
                threshold=settings.similarity_threshold,
            )
        return self._retriever

    # ---- 索引 ----
    def build_index(self) -> dict:
        """从 data/kb 加载 → 清洗 → 分块 → 向量化 → 入库（md5 幂等）。

        法规文件（category=law/judicial）优先用 LawArticleChunker 按"第 X 条"切分，
        每条一个 chunk，法条引用更精准；其余（上传文档等）用通用 RecursiveChunker。
        """
        store = self._get_store()
        loader = DocumentLoader()
        # 预创建两种 chunker：法规专用 + 通用
        law_chunker = LawArticleChunker(chunk_size=1024, chunk_overlap=64)
        gen_chunker = RecursiveChunker(chunk_size=settings.chunk_size,
                                       chunk_overlap=settings.chunk_overlap)

        documents = loader.load_directory(str(settings.kb_dir_path))
        if not documents:
            return {"added": 0, "total": store.count(), "message": "data/kb 下没有可索引的语料文件"}

        # 按 category 分流 chunker：法规用法条级切分，其余用通用分块
        law_cats = {"law", "judicial"}
        law_docs = [d for d in documents if _category_of(d.metadata.get("source", "")) in law_cats]
        other_docs = [d for d in documents if _category_of(d.metadata.get("source", "")) not in law_cats]

        for doc in documents:
            doc.metadata["category"] = _category_of(doc.metadata.get("source", ""))

        chunks: List[Document] = []
        if law_docs:
            chunks.extend(law_chunker.chunk(law_docs))
        if other_docs:
            chunks.extend(gen_chunker.chunk(other_docs))

        added = store.add_documents(chunks)
        return {"added": added, "total": store.count(),
                "sources": len(set(d.metadata.get("source") for d in documents)),
                "law_chunker": len(law_docs), "general_chunker": len(other_docs)}

    # ---- 检索 ----
    def retrieve(self, query: str, k: Optional[int] = None) -> List[Document]:
        """
        语义检索 + 可选重排序。返回空列表而非抛错：
        语料为空 / 嵌入 Key 缺失 / API 异常时审查链路均可正常降级。
        """
        k = k or settings.top_k
        if self._get_store().count() == 0:
            # 空库直接返回，连嵌入 API 都不调用（省一次无意义网络请求）
            return []
        try:
            retriever = self._get_retriever()
            hits = retriever.retrieve(query)
            if not hits:
                return []
            # 重排序：让最相关的法条块排到前面，只保留少量进入 Prompt
            reranker = create_reranker()
            reranked = reranker.rerank(query, hits)
            # rerank 已按 rerank_top_k 收敛，此处再截断属双保险
            return reranked[: settings.rerank_top_k]
        except Exception as e:  # 网络/Key 等一切异常：降级为无引用
            print(f"[LawKB] 检索降级（无引用返回）: {e}")
            return []

    def format_context(self, hits: List[Document]) -> str:
        """将检索命中的法条块格式化为 Prompt 上下文（带出处）。"""
        if not hits:
            return ""
        lines = []
        for i, doc in enumerate(hits, start=1):
            source = doc.metadata.get("source", "未知来源")
            page = doc.metadata.get("page_number")
            loc = f"（{source}，第{page}页）" if page else f"（{source}）"
            lines.append(f"[法条{i}]{loc}\n{doc.page_content.strip()}")
        return "\n\n".join(lines)

    def _resolve_under_kb(self, source: str) -> Optional[Path]:
        """把 chunk 元数据里的 source 解析为知识库目录内的路径（防越界删除）。"""
        if not source:
            return None
        path = Path(source)
        try:
            path.resolve().relative_to(settings.kb_dir_path.resolve())
        except (ValueError, OSError):
            return None
        return path

    # ---- 知识库管理（前端"知识库"页数据源，参照 smartbrief）----
    def upload_file(self, file_bytes: bytes, filename: str, category: str = "upload") -> dict:
        """上传语料：落盘 data/kb/uploads/ → 分格式加载 → 分块 → 向量化入库。

        :return: {"source","category","name","added","chunks"}（added 为新增块数）
        """
        # 校验：filename 不含路径分隔符（Path(filename).name != filename 时说明
        # 提交了 "../x" 之类的路径），防止写出知识库目录造成越界写文件
        if not filename or Path(filename).name != filename:
            raise ValueError("非法的文件名")
        ext = os.path.splitext(filename)[1].lower()
        if ext not in DocumentLoader.loaders:
            raise ValueError(f"不支持的文件类型: {ext or '无扩展名'}（支持 txt/md/docx/pdf）")
        # category 非法时兜底为 upload（前端可选值之外的类别不允许入库）
        category = category if category in KB_CATEGORIES else "upload"

        upload_dir = settings.kb_dir_path / UPLOAD_DIR_NAME
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / filename
        target.write_bytes(file_bytes)

        docs = DocumentLoader().load(str(target))
        for doc in docs:
            doc.metadata["category"] = category
            doc.metadata["uploaded_name"] = filename
        chunks = RecursiveChunker(chunk_size=settings.chunk_size,
                                  chunk_overlap=settings.chunk_overlap).chunk(docs)
        added = self._get_store().add_documents(chunks)
        return {"source": str(target), "category": category, "name": filename,
                "added": added, "chunks": len(chunks)}

    def list_documents(self) -> List[dict]:
        """文档列表：按 (category, source) 聚合 chunk，展示每个"文件"的概况。"""
        try:
            items = self._get_store().get_documents()
        except Exception as e:
            print(f"[LawKB] 读取文档列表失败: {e}")
            return []
        grouped: dict = {}
        for item in items:
            meta = item.get("metadata") or {}
            source = meta.get("source", "")
            if not source:
                continue
            category = _category_of(source, meta.get("category"))
            key = (category, source)
            entry = grouped.setdefault(key, {
                "category": category,
                "source": source,
                "name": Path(source).name,
                "file_type": meta.get("file_type", ""),
                "chunk_count": 0,
                "snippet": "",
            })
            entry["chunk_count"] += 1
            if not entry["snippet"]:
                entry["snippet"] = (item.get("content") or "")[:160]
        return sorted(grouped.values(), key=lambda x: (x["category"], x["name"]))

    def get_document_chunks(self, source: str, category: str) -> List[dict]:
        """指定 (source, category) 的全部 chunk（文档详情弹窗数据源）。"""
        try:
            items = self._get_store().get_documents(where={"source": source})
        except Exception as e:
            print(f"[LawKB] 读取文档详情失败: {e}")
            return []
        chunks = []
        for item in items:
            meta = item.get("metadata") or {}
            if _category_of(source, meta.get("category")) != category:
                continue
            chunks.append({"id": item["id"], "content": item["content"], "metadata": meta})
        # PDF 按页排序、其余按内容起始排序，尽量还原原文顺序
        chunks.sort(key=lambda x: (x["metadata"].get("page_number") or 0, x["content"][:200]))
        return chunks

    def delete_document(self, source: str, category: str) -> int:
        """删除指定 (source, category) 的全部 chunk，并移除知识库目录下的源文件。"""
        try:
            items = self._get_store().get_documents(where={"source": source})
        except Exception as e:
            print(f"[LawKB] 删除文档失败: {e}")
            return 0
        ids = []
        for item in items:
            meta = item.get("metadata") or {}
            if _category_of(source, meta.get("category")) != category:
                continue
            ids.append(item["id"])
        if ids:
            self._get_store().delete_by_ids(ids)
        path = self._resolve_under_kb(source)
        if path is not None and path.is_file():
            try:
                path.unlink()
            except OSError as e:
                print(f"[LawKB] 源文件删除失败（chunk 已删）: {e}")
        return len(ids)

    def search(self, query: str, k: int = 5, category: Optional[str] = None) -> List[dict]:
        """语义检索测试（/knowledge/search）：返回 content/source/category/score。"""
        if self._get_store().count() == 0:
            return []
        try:
            query_embedding = self._get_embeddings().embed_query(query)
            # 仅当 category 是合法分类时才按类过滤，否则不加 where 做全库检索
            where = {"category": category} if category in KB_CATEGORIES else None
            hits = self._get_store().similarity_search(query_embedding, k=k, where=where)
            return [
                {
                    "content": doc.page_content,
                    "source": doc.metadata.get("source", ""),
                    "category": _category_of(doc.metadata.get("source", ""),
                                             doc.metadata.get("category")),
                    "score": round(score, 4),
                }
                for doc, score in hits
            ]
        except Exception as e:
            print(f"[LawKB] 知识库检索测试失败: {e}")
            return []

    # ---- 统计与维护 ----
    def stats(self) -> dict:
        """知识库统计入口；向量库异常时返回带 error 的空统计而非抛错。"""
        try:
            return self._get_store().get_stats()
        except Exception as e:
            return {"total": 0, "sources": {}, "error": str(e)}

    def clear(self) -> None:
        """清空知识库：仅删除已初始化的集合，并重置句柄以便下次按需重建。"""
        if self._vectorstore is not None:
            self._get_store().delete_collection()
            # 句柄置空：重建索引/再次检索时会走惰性初始化创建全新集合
            self._vectorstore = None
            self._retriever = None


# 模块级单例（与 zhiqida-rag / smartbrief 一致的惰性单例惯例）
kb = LawKB()
