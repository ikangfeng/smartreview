# =============================================================================
# 法条引用校验 - law_basis_checker.py
# =============================================================================
# 法律审查产品的可信度底线：LLM 输出的 law_basis 可能编造不存在的法条，
# 直接照单全收会摧毁报告可信度。本模块用 datachain 沉淀的 36 条法条白名单
# 做“命中校验”：
#   1. 归一化后与白名单条目互相包含 → 通过；
#   2. 《法律名》+第X条：法律名须在白名单内、条号须在该法的收录范围内；
#   3. RAG 检索材料中同时出现该法律名与条号（阿拉伯/中文数字）→ 视为佐证通过。
# 不通过的不删除原文，而是追加“（待核验：未命中法条白名单，请人工核对）”，
# 既保留模型线索，又明确提示法务复核（避免把幻觉当权威依据）。
# =============================================================================

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

_WHITELIST_PATH = Path(__file__).resolve().parent.parent / "config" / "law_basis_whitelist.json"

_LAW_RE = re.compile(r"《([^》]+)》")
_ARTICLE_RE = re.compile(r"第\s*([0-9]+)\s*条(?:\s*(?:至|到|-|~|～)\s*第?\s*([0-9]+)\s*条)?")
_NORM_RE = re.compile(r"[\s，。；、：:（）()《》〈〉“”\"'·\-—_/]")
_PENDING_MARK = "待核验"


@lru_cache(maxsize=1)
def _whitelist() -> dict:
    """加载并解析白名单：法律名集合 + 各法收录的条号集合 + 原始条目。"""
    data = json.loads(_WHITELIST_PATH.read_text(encoding="utf-8"))
    refs = [str(item.get("ref") or "") for item in data.get("items", [])]
    laws, articles = set(), {}
    for ref in refs:
        names = _LAW_RE.findall(ref)
        laws.update(names)
        for m in _ARTICLE_RE.finditer(ref):
            start = int(m.group(1))
            end = int(m.group(2) or start)
            for n in range(min(start, end), max(start, end) + 1):
                for name in names:
                    articles.setdefault(name, set()).add(n)
    return {"refs": [r for r in refs if r],
            "laws": laws,
            "articles": articles,
            "norm_refs": {_norm(r) for r in refs if r}}


def _norm(text: str) -> str:
    """归一化：去空白/标点/书名号，容忍“参照”“第X条至第Y条”等写法差异。"""
    return _NORM_RE.sub("", text or "")


def _cn_number(n: int) -> str:
    """阿拉伯数字转中文条号（RAG 法条原文多为“第五百八十五条”）。"""
    digits = "零一二三四五六七八九"
    if n < 10:
        return digits[n]
    if n < 20:
        return "十" + (digits[n % 10] if n % 10 else "")
    if n < 100:
        return digits[n // 10] + "十" + (digits[n % 10] if n % 10 else "")
    if n < 1000:
        rest = n % 100
        head = digits[n // 100] + "百"
        if rest == 0:
            return head
        return head + ("零" + digits[rest] if rest < 10 else _cn_number(rest))
    if n < 10000:
        rest = n % 1000
        head = digits[n // 1000] + "千"
        if rest == 0:
            return head
        return head + ("零" + _cn_number(rest) if rest < 100 else _cn_number(rest))
    return str(n)


def _article_forms(n: int) -> set:
    """同一条号的阿拉伯/中文两种写法。"""
    return {str(n), _cn_number(n)}


def _rag_verified(citation: str, rag_context: str) -> bool:
    """RAG 材料能否佐证该引用：法律名与每个条号都出现在检索上下文中。"""
    if not rag_context:
        return False
    names = _LAW_RE.findall(citation)
    if not names:
        return False
    rag_norm = _norm(rag_context)
    for name in names:
        if _norm(name) not in rag_norm:
            return False
    for m in _ARTICLE_RE.finditer(citation):
        start = int(m.group(1))
        end = int(m.group(2) or start)
        for n in range(min(start, end), max(start, end) + 1):
            if not any(form in rag_norm for form in _article_forms(n)):
                return False
    return True


def is_whitelisted(citation: str, rag_context: str = "") -> bool:
    """判断一条 law_basis 是否通过白名单/RAG 校验。空引用视为通过。"""
    if not citation or not citation.strip():
        return True
    wl = _whitelist()
    norm = _norm(citation)

    # 路径一：整体与白名单条目互相包含（覆盖“参照《民法典》合同编技术合同章”等宽泛写法）
    for ref_norm in wl["norm_refs"]:
        if ref_norm and (ref_norm in norm or norm in ref_norm):
            return True

    names = _LAW_RE.findall(citation)
    articles = []
    for m in _ARTICLE_RE.finditer(citation):
        start = int(m.group(1))
        end = int(m.group(2) or start)
        articles.extend(range(min(start, end), max(start, end) + 1))

    # 路径一·补充：模型直接复述 RAG 法条原文（常不带书名号）→ 原文包含即视为佐证。
    # 只对足够长的片段生效，避免极短片段偶然命中造成“假通过”。
    if rag_context and len(norm) >= 12:
        rag_norm = _norm(rag_context)
        if norm in rag_norm:
            return True
        for size in (40, 24, 12):
            if len(norm) >= size and norm[:size] in rag_norm:
                return True

    # 路径二：无具名法律（如“税收相关法律法规”）：只认白名单原文包含匹配
    if not names:
        return False

    # 路径三：法律名与条号逐一校验；条号不在该法收录范围内时用 RAG 佐证兜底
    for name in names:
        if name not in wl["laws"]:
            return _rag_verified(citation, rag_context)
        allowed = wl["articles"].get(name) or set()
        if not allowed:
            continue  # 该法只有宽泛条目（如《反不正当竞争法》相关规定）
        for n in articles:
            if n not in allowed:
                return _rag_verified(citation, rag_context)
    return True


def annotate_law_basis(text: Optional[str], rag_context: str = "") -> str:
    """校验并标注 law_basis：未通过白名单的引用追加“待核验”提示。"""
    if not text or not str(text).strip():
        return ""
    citation = str(text).strip()
    if _PENDING_MARK in citation:
        return citation
    if is_whitelisted(citation, rag_context):
        return citation
    return f"{citation}（待核验：未命中法条白名单，请人工核对）"
