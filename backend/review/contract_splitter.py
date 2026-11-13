# =============================================================================
# 合同文本抽取 - contract_splitter.py
# =============================================================================
# 职责：
#   1. 从 PDF/DOCX/TXT 抽取全文与分页信息（页数、每页起始偏移）；
#   2. 识别章节层级（一、二、…第一章 / 1.1 / 第 X 条），输出结构化 clauses；
#   3. 过滤合同元数据（签章页、编号、日期、甲乙方抬头）；
#   4. 降级：无任何结构时按空行段落硬切。
# =============================================================================

import bisect
import os
import re
from typing import List, Optional

from rag.loaders import DocumentLoader, PDFLoader

# ---- 条款结构正则（按置信度从高到低，越精准越优先）----

# 精准：1.1、2.3、10.7 —— 数字点分，最常见的合同子条款
# 只匹配 "1.1 " 前缀（不带 $ 行尾锚定），m.end() 正好指向正文起点
_SUB_CLAUSE_RE = re.compile(
    # \s* 而非 \s+：OCR 还原的扫描件常把"1.1 甲方"识别成"1.1甲方"（丢空格），
    # 要求必须有空格会让整份扫描件退化成单条"正文"
    r"^(\d+)\.(\d+)\s*", re.MULTILINE)

# 次精准：1、2、3 —— 单数字 + 顿号/点 + 空格（章标题或一级条款）
# 加 (?!\d) 负向前瞻：后面不能紧跟另一个数字，排除 1.1、2.3 这种子条款
_DIGIT_TITLE_RE = re.compile(
    r"^(\d+)[、.．]\s*(?!\d)(.+)$", re.MULTILINE)

# 中文：一、二、三、...十、 —— 章标题
_CN_TITLE_RE = re.compile(
    r"^([一二三四五六七八九十]+)[、.．\s]+(.+)$", re.MULTILINE)

# 传统合同：第 X 条（之前的主模式，保留兼容）
_ARTICLE_RE = re.compile(
    r"第\s*([零一二三四五六七八九十百千万\d]+)\s*条")

# 标题清洗：去掉首尾标点
_TITLE_CLEAN = re.compile(r"^[、.．:：\s　\-—]+|[、.．:：\s　\-—]+$")

# ---- 元数据过滤 ----

# 合同抬头：甲方/乙方/委托方/服务方/合同编号/签订日期
_HEADER_META_RE = re.compile(
    r"^(甲方|乙方|委托方|受托方|服务方|发包方|承包方|采购方|供应方|合同编号|签订日期|签订地点|合同名称)\s*[:：]")

# 签章页：盖章、法定代表人、授权代表、日期行
_SIGN_PAGE_RE = re.compile(
    r"(盖章|章\)|法定代表人|授权代表|签订日期|\d{4}年\d{1,2}月\d{1,2}日|^\s*日期[:：]|\(盖章\))")

# 合同尾部杂项：一式两份、生效、附件说明
_TAIL_META_RE = re.compile(
    r"(一式\s*[一二三四五六七八九十\d]+\s*份|自.*(签字|盖章)之日起生效|附件为本合同不可分割的组成部分|与本合同具有同等法律效力)")

# 签署区起始行：条款正文到这里就结束。
# 注意：不能因为正文里出现"一式两份""具有同等法律效力"就丢弃整条——
# 那会把「七、其他」这类正常条款整节误杀，正确做法是只截掉末尾签署区。
_SIGN_TAIL_RE = re.compile(
    r"^\s*(?:甲方|乙方)\s*[（(]\s*盖章|^\s*法定代表人|^\s*授权代表|"
    r"^\s*日期\s*[:：]|^\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*$")


def _trim_contract_tail(content: str) -> str:
    """截掉条款正文末尾混入的签署区（甲方盖章/法定代表人/日期等）。"""
    lines = []
    for line in (content or "").split("\n"):
        if _SIGN_TAIL_RE.match(line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip()

SUPPORTED_EXTS = {".pdf", ".docx", ".txt"}


def read_contract_pages(file_path: str) -> dict:
    """读取合同文件 → {"pages": [...每页文本...], "full_text": ..., "page_count": n}"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        docs = PDFLoader().load(file_path)
        page_texts = [d.page_content for d in docs]
        # OCR 页数随解析结果上抛：上传接口据此提示“已对扫描件启用 OCR”
        ocr_pages = sum(1 for d in docs if (d.metadata or {}).get("ocr"))
        return {"pages": page_texts, "page_count": len(page_texts),
                "ocr_pages": ocr_pages}
    docs = DocumentLoader().load(file_path)
    text = docs[0].page_content if docs else ""
    paragraphs = [p for p in text.split("\n") if p.strip()]
    page_count = max(1, (len(paragraphs) + 39) // 40)
    return {"pages": [text], "page_count": page_count, "ocr_pages": 0}


def _build_text_index(pages: List[str]) -> dict:
    full_text = "\n".join(pages)
    offsets = []
    cursor = 0
    for i, page in enumerate(pages):
        offsets.append(cursor)
        cursor += len(page) + 1
    return {"full_text": full_text, "page_offsets": offsets}


def _page_of(char_index: int, page_offsets: List[int]) -> int:
    pos = bisect.bisect_right(page_offsets, char_index)
    return max(pos, 1)


def _is_meta_line(line: str) -> bool:
    """判断一行是否是合同元数据（跳过不审查）。"""
    line = line.strip()
    if not line or len(line) < 3:
        return True
    if _HEADER_META_RE.match(line):
        return True
    if _SIGN_PAGE_RE.search(line):
        return True
    if _TAIL_META_RE.search(line):
        return True
    if re.match(r"^\d{4}年\d{1,2}月\d{1,2}日$", line):
        return True
    # 文档标题（如 "技术开发委托合同"）：以合同/协议类词结尾的短行才算，
    # 不能只看"含合同二字"——否则 "本合同项下税费由乙方承担。" 这类正文会被误杀
    if len(line) <= 25 and line.endswith(("合同", "协议", "协议书", "补充协议")):
        return True
    return False


def _strip_embedded_chapter_titles(content: str) -> str:
    """如果子条款内容里包含下一个章节标题（因为章节在 event 列表里），
    可能被 _SUB_CLAUSE_RE 覆盖范围遗漏，把它切掉。"""
    lines = content.split("\n")
    kept = []
    for line in lines:
        stripped = line.strip()
        # 中文章节 / 数字章节标题：如果出现在中间行，说明是混入的
        if (_CN_TITLE_RE.match(stripped) or _DIGIT_TITLE_RE.match(stripped)) and kept:
            # 出现在非首行 → 是混入的下一章标题，丢弃
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _extract_chapter_heading(text_block: str) -> Optional[str]:
    """从一个文本块的开头提取章节标题。"""
    lines = text_block.split("\n")
    for line in lines[:3]:  # 只看前 3 行
        m = _CN_TITLE_RE.match(line.strip())
        if m:
            return f"{m.group(1)}、{m.group(2).strip()}"
        m = _DIGIT_TITLE_RE.match(line.strip())
        if m and "." not in line.split("、")[0]:  # 排除 1.1 这种子条款
            return f"{m.group(1)}. {m.group(2).strip()}"
    return None


def split_clauses(text: str, page_offsets: Optional[List[int]] = None) -> List[dict]:
    """
    分层拆解合同（策略从强到弱，命中即返回）：
      1. 子条款正则（1.1 / 2.3 模式）——最精准，同时用章节正则做归属标注；
      2. 传统"第 X 条"；
      3. 中文章标题（一、/ 二、）；
      4. 降级：空行段落硬切 + 元数据过滤。
    """
    page_offsets = page_offsets or [0]
    text = text.strip()

    # ============================================================
    # 策略 1：子条款正则（1.1 / 2.3 模式）——最精准
    # ============================================================
    sub_matches = list(_SUB_CLAUSE_RE.finditer(text))
    if len(sub_matches) >= 3:
        # 同时提取章节位置（章节可能在子条款之间）
        # 合并章节标题 + 子条款为一个有序列表
        chapter_events = []  # [(char_index, 'chapter', title), (char_index, 'sub', match_obj)]
        for m in _CN_TITLE_RE.finditer(text):
            chapter_events.append((m.start(), "chapter", f"{m.group(1)}、{m.group(2).strip()}"))
        for m in _DIGIT_TITLE_RE.finditer(text):
            if "." not in m.group(1) and m.start() < 50000:  # 排除子条款
                chapter_events.append((m.start(), "chapter", f"{m.group(1)}. {m.group(2).strip()}"))
        for m in sub_matches:
            chapter_events.append((m.start(), "sub", m))
        chapter_events.sort(key=lambda x: x[0])

        clauses = []
        current_chapter = None
        for idx, (pos, kind, payload) in enumerate(chapter_events):
            if kind == "chapter":
                current_chapter = payload
                continue
            # kind == "sub"
            m = payload
            # 结束位置：下一个 sub 或 chapter 事件的起始
            next_event_start = chapter_events[idx + 1][0] if idx + 1 < len(chapter_events) else len(text)
            content = text[m.end():next_event_start].strip()
            content = _trim_contract_tail(content)
            if not content or len(content) < 4:
                continue
            # 只过滤真正的页眉/标题类内容；条款正文里含"一式两份"等字样不再整条丢弃
            if _HEADER_META_RE.match(content[:80]):
                continue
            # 如果 content 里包含章节标题（说明被截断了），去掉
            content = _strip_embedded_chapter_titles(content)
            if not content:
                continue
            clauses.append({
                "clause_id": f"{m.group(1)}.{m.group(2)}",
                "title": None,
                "content": content,
                "char_index": m.start(),
                "page": _page_of(m.start(), page_offsets),
                "chapter": current_chapter,
            })
        if clauses:
            return clauses

    # ============================================================
    # 策略 2：传统"第 X 条"
    # ============================================================
    art_matches = list(_ARTICLE_RE.finditer(text))
    if len(art_matches) >= 2:
        clauses = []
        for idx, m in enumerate(art_matches):
            start = m.end()
            end = art_matches[idx + 1].start() if idx + 1 < len(art_matches) else len(text)
            content = text[start:end].strip()
            if not content:
                continue
            # 清理标题：第 X 条之后的第一行
            title_line = content.split("\n", 1)[0].strip()
            title = _TITLE_CLEAN.sub("", title_line) if title_line else None
            clauses.append({
                "clause_id": m.group(0).strip(),
                "title": title if title and title != content else None,
                "content": content,
                "char_index": m.start(),
                "page": _page_of(m.start(), page_offsets),
                "chapter": _extract_chapter_heading(text[max(0, m.start()-80):m.start()]),
            })
        if clauses:
            return clauses

    # ============================================================
    # 策略 3：中文章标题（一、二、）
    # ============================================================
    cn_matches = list(_CN_TITLE_RE.finditer(text))
    if len(cn_matches) >= 2:
        clauses = []
        for idx, m in enumerate(cn_matches):
            start = m.end()
            end = cn_matches[idx + 1].start() if idx + 1 < len(cn_matches) else len(text)
            content = text[start:end].strip()
            if not content:
                continue
            # 子条款进一步切分
            sub_clauses = _split_by_sub_clauses(content)
            heading = f"{m.group(1)}、{m.group(2).strip()}"
            for sc in sub_clauses:
                clauses.append({
                    "clause_id": sc["clause_id"],
                    "title": None,
                    "content": sc["content"],
                    "char_index": start + text[start:].find(sc["content"]),
                    "page": _page_of(start, page_offsets),
                    "chapter": heading,
                })
        if clauses:
            return clauses

    # ============================================================
    # 策略 4：最终降级——空行段落硬切 + 元数据过滤
    # ============================================================
    clauses = []
    # 用双换行切段落，单换行保留在段落内
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    i = 0
    for para in paragraphs:
        i += 1
        if _is_meta_line(para[:60]):
            continue
        clauses.append({
            "clause_id": f"P{i}",
            "title": None,
            "content": para,
            "char_index": text.find(para),
            "page": _page_of(text.find(para), page_offsets),
            "chapter": None,
        })
    return clauses


def _split_by_sub_clauses(block: str) -> List[dict]:
    """在一个章内，按 1.1 / 2.3 模式进一步切分；找不到就整块返回。"""
    sub_matches = list(_SUB_CLAUSE_RE.finditer(block))
    if len(sub_matches) < 2:
        return [{"clause_id": "正文", "content": block}]
    out = []
    for idx, m in enumerate(sub_matches):
        start = m.end()
        end = sub_matches[idx + 1].start() if idx + 1 < len(sub_matches) else len(block)
        content = block[start:end].strip()
        content = _trim_contract_tail(content)
        if not content:
            continue
        out.append({"clause_id": f"{m.group(1)}.{m.group(2)}", "content": content})
    return out


def parse_contract(file_path: str) -> dict:
    """入口：读取文件 → 拆解条款。返回 {meta, page_count, clauses}。"""
    read = read_contract_pages(file_path)
    pages = read["pages"]
    index = _build_text_index(pages)
    clauses = split_clauses(index["full_text"], index["page_offsets"])
    return {
        "full_text": index["full_text"],
        "page_count": read["page_count"],
        "clauses": clauses,
        "ocr_pages": read.get("ocr_pages", 0),
    }
