# =============================================================================
# 文本分块模块 - Chunkers（精简复用 zhiqida-rag core/chunkers.py）
# =============================================================================
# 保留工业界最常用的 RecursiveChunker + 法律专用 LawArticleChunker：
#   - RecursiveChunker：段落/句子递归切分，一般文档用
#   - LawArticleChunker：法规文件优先按"第 X 条"切，每条一个 chunk
# =============================================================================

import re
from typing import List

from rag.loaders import Document


class BaseChunker:
    """分块器抽象基类。"""

    def chunk(self, documents: List[Document]) -> List[Document]:
        """子类实现：把输入文档列表切分为带 chunk_index 的块列表。"""
        raise NotImplementedError


class RecursiveChunker(BaseChunker):
    """递归分块器：尽量保持语义完整的自包含块。"""

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 64):
        """chunk_overlap 使相邻块首尾交叠，避免切在语义中间时丢失上下文。"""
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        # 分隔符按"语义边界强度"降序排列：段落>行>句号>逗号>空格
        # ——先保证在大边界处切分，实在没有大边界才逐级降级到字符
        self.separators = ["\n\n", "\n", "。", ".", "；", "，", ",", " ", ""]

    def chunk(self, documents: List[Document]) -> List[Document]:
        """逐文档切分；每个块继承来源 metadata 并标注块序号与大小。"""
        chunks: List[Document] = []
        for doc in documents:
            for i, chunk_text in enumerate(self._recursive_split(doc.page_content)):
                # 展开继承 source/page_number 等原 metadata 以便溯源，
                # 另加 chunk_index/chunk_size 便于调参与还原文档顺序
                chunks.append(Document(page_content=chunk_text, metadata={
                    **doc.metadata,
                    "chunk_index": i,
                    "chunk_size": len(chunk_text),
                }))
        return chunks

    def _recursive_split(self, text: str) -> List[str]:
        """按分隔符优先级递归切分，块不超过 chunk_size。"""
        if len(text) <= self.chunk_size:
            # 整段已足够小：直接整体返回，避免无谓切分破坏语义
            return [text]

        chunks: List[str] = []
        for sep in self.separators:
            if sep == "":
                # 兜底分支：走到这说明所有分隔符都不在文本中
                #（如超长无标点连续字符），只能按步长硬切；步长减去重叠量
                # 使相邻块仍共享 chunk_overlap 的上下文
                chunks = [text[i:i + self.chunk_size]
                          for i in range(0, len(text), self.chunk_size - self.chunk_overlap)]
                break
            if sep in text:
                segments = text.split(sep)
                current = ""
                for segment in segments:
                    # 贪心累积：把段拼进当前块仍未超长就并入，
                    # 否则说明"当前块已满"，先落盘再以该段起新块
                    candidate = current + sep + segment if current else segment
                    if len(candidate) <= self.chunk_size:
                        current = candidate
                    else:
                        if current:
                            chunks.append(current)
                        # 即使 segment 自身超长也不能丢弃：交给下方递归处理
                        current = segment
                if current:
                    chunks.append(current)
                break

        result: List[str] = []
        for chunk in chunks:
            if len(chunk) > self.chunk_size:
                # 降级切分后仍超长（如单个超长段）：递归到下一级分隔符再切
                result.extend(self._recursive_split(chunk))
            else:
                result.append(chunk)
        return result


class LawArticleChunker(BaseChunker):
    """法律专用分块器：按"第 X 条"切法规文件，每条一个 chunk。

    为什么单独一个 chunker：通用 RecursiveChunker 按 512 字符硬切，
    会把一条法条切在中间（例如"第五百八十五条 当事人可以约定违约金……"
    被拆成 2 块），检索时命中其中一半、法条引用就不完整。
    本 chunker 优先识别法条标记，每条独立成块；
    条款超长（>chunk_size）时才对单条内部做递归切分兜底；
    文件里完全没"第 X 条"结构时，回退到 RecursiveChunker 行为。
    """

    # 法条标记：中文数字 + 阿拉伯数字 + "之一"（民法典有"第 X 条之一"）
    _ARTICLE_RE = re.compile(
        r"第\s*([零一二三四五六七八九十百千万〇两\d]+)\s*条(之[一二三四五六七八九十\d]+)?")

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 64):
        """法规条通常长于普通段落，chunk_size 放大到 1024 保留完整法条。"""
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        # 内部兜底：单条法条超长时对该条内部做递归切分
        self._fallback = RecursiveChunker(chunk_size=chunk_size,
                                           chunk_overlap=chunk_overlap)

    def chunk(self, documents: List[Document]) -> List[Document]:
        chunks: List[Document] = []
        for doc in documents:
            text = doc.page_content
            matches = list(self._ARTICLE_RE.finditer(text))
            if len(matches) < 2:
                # 没有法条结构（非法规文件）：回退通用分块器，保证能用
                chunks.extend(self._fallback.chunk([doc]))
                continue

            # 有法条结构：逐条切分
            article_chunks = []
            for idx, m in enumerate(matches):
                start = m.start()
                end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
                block = text[start:end].strip()
                if not block:
                    continue
                # 给 metadata 加 article_number 便于溯源
                meta = {**doc.metadata, "article_number": m.group(0).strip()}
                if len(block) <= self.chunk_size:
                    article_chunks.append(Document(page_content=block, metadata=meta))
                else:
                    # 单条法条超长：内部递归切分，但保留同一条的 article_number
                    for sub_text in self._fallback._recursive_split(block):
                        if sub_text.strip():
                            article_chunks.append(Document(page_content=sub_text.strip(), metadata=meta))

            # 法条前面可能有"总则/第一章"等标题，单独作为一个前置 chunk
            if matches and matches[0].start() > 0:
                prefix = text[:matches[0].start()].strip()
                if prefix and len(prefix) > 10:
                    chunks.append(Document(
                        page_content=prefix,
                        metadata={**doc.metadata, "article_number": "前言/总则"}))

            chunks.extend(article_chunks)

        # 统一 chunk_index，让后续管理页面能按序号展示
        for i, c in enumerate(chunks):
            c.metadata["chunk_index"] = i
            c.metadata["chunk_size"] = len(c.page_content)
        return chunks
