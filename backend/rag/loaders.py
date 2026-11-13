# =============================================================================
# 文档加载模块 - Document Loader（精简复用 zhiqida-rag core/loaders.py）
# =============================================================================
# 策略模式：DocumentLoader 按扩展名路由到不同 Loader 子类。
# 为 SmartReview 精简：仅保留合同/法律语料场景的 PDF、DOCX、TXT、MD；
# 移除 Excel 加载器（openpyxl 不再依赖）。
# 统一数据模型 Document(page_content, metadata) 贯穿加载→分块→入库→检索。
# =============================================================================

import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Document:
    """统一文档数据模型：metadata 含 source/file_type/page_number 等。"""
    page_content: str
    metadata: dict


class BaseLoader:
    """加载器抽象基类。"""

    def load(self, file_path: str) -> List[Document]:
        """子类实现：把指定文件解析为 Document 列表。"""
        raise NotImplementedError


class TextLoader(BaseLoader):
    """TXT 加载器（UTF-8）。"""

    def load(self, file_path: str) -> List[Document]:
        """整篇读入为单个 Document（纯文本没有内部结构可拆）。"""
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        return [Document(page_content=text, metadata={"source": file_path, "file_type": "txt"})]


class MarkdownLoader(BaseLoader):
    """Markdown 加载器。"""

    def load(self, file_path: str) -> List[Document]:
        """整篇读入为单个 Document（保留 Markdown 原样，语义在分块阶段处理）。"""
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        return [Document(page_content=text, metadata={"source": file_path, "file_type": "markdown"})]


class DocxLoader(BaseLoader):
    """Word 加载器：按段落抽取非空文本，整篇为一个 Document。"""

    def load(self, file_path: str) -> List[Document]:
        """读取 docx 段落，剔除空段后以换行拼接返回。"""
        try:
            from docx import Document as DocxDocument
        except ImportError:
            raise ImportError("python-docx 未安装，请执行: pip install python-docx")
        doc = DocxDocument(file_path)
        # 剔除空/纯空白段落：它们不含语义，只会放大 token 开销与检索噪声
        paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
        # 用换行还原段落边界，便于后续分块器优先在段落处切分
        return [Document(page_content="\n".join(paragraphs),
                         metadata={"source": file_path, "file_type": "docx"})]


class OCRUnavailableError(RuntimeError):
    """检测到扫描件但 OCR 依赖未安装：交由上层转成可读的 4xx 提示。"""


# RapidOCR 引擎初始化较重（加载 ONNX 模型约 1-2s），进程内只建一次
_OCR_ENGINE = None
# 文本层少于该字符数视为“无有效文字层”（扫描页常残留页眉/页码噪声）
_OCR_MIN_CHARS = 20
# 单份文件最多 OCR 页数：OCR 约 1-2s/页，不设上限会让几百页扫描件把上传请求拖到超时
_OCR_MAX_PAGES = 20


def _ocr_available() -> bool:
    """OCR 依赖是否就绪（不触发模型加载，仅做模块探测）。"""
    import importlib.util
    return (importlib.util.find_spec("rapidocr_onnxruntime") is not None
            and importlib.util.find_spec("pymupdf") is not None)


def _get_ocr_engine():
    """惰性初始化 RapidOCR 引擎（跨平台、无需系统级 tesseract）。"""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise OCRUnavailableError(
                "检测到扫描版 PDF，但未安装 OCR 引擎。请执行："
                "pip install rapidocr_onnxruntime pymupdf") from e
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _render_pdf_page_png(file_path: str, page_index: int, dpi: int = 200) -> bytes:
    """用 PyMuPDF 把指定页渲染为 PNG（扫描件没有文字层，只能走图像 OCR）。"""
    try:
        import pymupdf
    except ImportError as e:
        raise OCRUnavailableError(
            "扫描件 OCR 需要 PyMuPDF。请执行：pip install pymupdf") from e
    with pymupdf.open(file_path) as doc:
        page = doc.load_page(page_index)
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def _ocr_result_to_text(result) -> str:
    """把 RapidOCR 的 [box, text, score] 列表按版面还原为多行文本。

    按文本框中心 y 聚类成行、行内按 x 排序；中文直接拼接（不插空格，
    避免把“知识产权”拆成“知识 产权”导致后续关键词匹配失效）。
    """
    if not result:
        return ""
    boxes = []
    for item in result:
        if not item or len(item) < 2:
            continue
        box, text = item[0], str(item[1]).strip()
        if not text or not box:
            continue
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        boxes.append((min(ys), max(ys), min(xs), text))
    if not boxes:
        return ""

    boxes.sort(key=lambda b: (b[0], b[2]))
    heights = [max(1.0, b[1] - b[0]) for b in boxes]
    tol = max(6.0, (sum(heights) / len(heights)) * 0.6)

    rows = []
    for y0, y1, x, text in boxes:
        cy = (y0 + y1) / 2
        if rows and abs(cy - rows[-1]["cy"]) <= tol:
            rows[-1]["items"].append((x, text))
            rows[-1]["cy"] = (rows[-1]["cy"] + cy) / 2
        else:
            rows.append({"cy": cy, "items": [(x, text)]})
    return "\n".join("".join(t for _, t in sorted(row["items"])) for row in rows).strip()


def _ocr_pdf_page(file_path: str, page_index: int) -> str:
    """渲染单页 → OCR → 文本；任何识别失败都返回空串交由调用方降级。"""
    import io

    import numpy as np
    from PIL import Image

    png = _render_pdf_page_png(file_path, page_index)
    # 统一转 RGB ndarray：RapidOCR 对 PNG bytes/路径的兼容性随版本而异
    image = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
    result, _ = _get_ocr_engine()(image)
    return _ocr_result_to_text(result)


class PDFLoader(BaseLoader):
    """PDF 加载器：每页生成一个 Document，保留 page_number 便于溯源。

    扫描件兜底：PyPDF2 只能读文字层，扫描版 PDF 会整页为空。此时将页面
    渲染成图片走 OCR（RapidOCR），再并入现有拆条流程；OCR 结果在 metadata
    打 ocr 标记，供上传接口提示“已启用 OCR”。
    """

    def load(self, file_path: str) -> List[Document]:
        """逐页抽取文本；无文字层的页面按需 OCR。"""
        try:
            import PyPDF2
        except ImportError:
            raise ImportError("PyPDF2 未安装，请执行: pip install PyPDF2")
        with open(file_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            page_texts = [(page.extract_text() or "") for page in reader.pages]

        has_text_layer = any(len(t.strip()) >= _OCR_MIN_CHARS for t in page_texts)
        can_ocr = _ocr_available()

        docs = []
        for i, text in enumerate(page_texts):
            ocr_used = False
            # 文字层过薄 → 疑似扫描页；仅在 OCR 可用且未超过页数上限时尝试
            if len(text.strip()) < _OCR_MIN_CHARS and can_ocr and i < _OCR_MAX_PAGES:
                try:
                    ocr_text = _ocr_pdf_page(file_path, i)
                except Exception as e:
                    print(f"[Loader] 第 {i + 1} 页 OCR 降级: {e}")
                    ocr_text = ""
                if ocr_text:
                    text, ocr_used = ocr_text, True
            if not text.strip():
                continue
            docs.append(Document(page_content=text, metadata={
                "source": file_path,
                "file_type": "pdf",
                "page_number": i + 1,
                "ocr": ocr_used,
            }))

        # 整份都扫不出文字：区分“扫描件缺 OCR 依赖”与“真空白/加密 PDF”，
        # 前者给出安装提示，后者保持原来的空结果行为
        if not docs and not has_text_layer and not can_ocr:
            raise OCRUnavailableError(
                "该 PDF 未包含可提取的文字层（疑似扫描件），且服务器未安装 OCR 引擎。"
                "请执行：pip install rapidocr_onnxruntime pymupdf")
        return docs


class DocumentLoader:
    """文档加载统一入口（Context 角色）。"""

    loaders = {
        ".txt": TextLoader(),
        ".md": MarkdownLoader(),
        ".docx": DocxLoader(),
        ".pdf": PDFLoader(),
    }

    # 文件类型白名单：上传与知识库共用
    ALLOWED_TYPES = ["pdf", "docx", "txt", "md"]

    def load(self, file_path: str) -> List[Document]:
        """按扩展名路由到对应 Loader；不支持的扩展名直接抛错。"""
        # lower() 统一小写：兼容 ".PDF" 等大写扩展名，否则会误判为不支持
        ext = os.path.splitext(file_path)[1].lower()
        loader = self.loaders.get(ext)
        if loader is None:
            raise ValueError(f"不支持的文件类型: {ext}")
        return loader.load(file_path)

    def load_directory(self, dir_path: str) -> List[Document]:
        """递归加载目录中所有受支持文件，单个文件失败不中断。"""
        all_docs: List[Document] = []
        if not os.path.isdir(dir_path):
            # 目录不存在视为"无语料"而非错误：调用方据此走空库降级分支
            return all_docs
        for root, _, files in os.walk(dir_path):
            # 排序使重建索引的遍历顺序确定，结果可复现、便于对比
            for file_name in sorted(files):
                file_path = os.path.join(root, file_name)
                ext = os.path.splitext(file_name)[1].lower()
                if ext in self.loaders:
                    try:
                        all_docs.extend(self.load(file_path))
                    except Exception as e:
                        # 单个坏文件（如损坏的 PDF）降级为跳过，不让整批索引失败
                        print(f"跳过 {file_path}: {e}")
        return all_docs
