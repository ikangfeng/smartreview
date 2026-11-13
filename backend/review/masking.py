# =============================================================================
# 送审脱敏 - masking.py
# =============================================================================
# 法务客户的核心顾虑：合同原文（当事人、账号、证件号、联系方式）不能被
# 外部大模型看到。本模块在送审前把敏感信息替换为占位符，模型返回后再还原：
#   - 当事人名称：按任务快照的甲/乙方精确替换为【甲方主体】/【乙方主体】；
#   - 其他公司名、邮箱、手机号、身份证号、长账号：统一编号占位；
#   - 金额/百分比/日期不脱敏（模型做责任上限、账期判断必须依赖这些数值）。
# 规则引擎在本地跑原文，不受影响；仅 LLM 与向量检索请求使用脱敏文本。
# 占位符刻意不含“公司”等关键词，避免被二次识别。
# =============================================================================

import re
import threading
from typing import Dict, Tuple

# 公司名：2-30 个中英文/括号字符 + 公司后缀（先于其他规则执行）
_COMPANY_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,30}?"
    r"(?:有限责任公司|股份有限公司|有限公司|集团有限公司|集团)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MOBILE_RE = re.compile(r"1[3-9]\d{9}")
_ID_RE = re.compile(r"\b\d{17}[\dXx]\b")
# 12-19 位连续数字视为银行账号/统一社会信用代码等长编号
_ACCOUNT_RE = re.compile(r"\b\d{12,19}\b")


class TextMasker:
    """任务级脱敏器：同一名称在整份合同内保持同一占位符，便于还原。"""

    def __init__(self, contract_meta: Dict):
        self._lock = threading.Lock()
        self._mapping: Dict[str, str] = {}      # 占位符 → 原文
        self._reverse: Dict[str, str] = {}      # 原文 → 占位符
        self._counter: Dict[str, int] = {}
        self.count = 0                          # 实际替换次数（用于“已脱敏 N 处”）
        for key, label in (("party_a", "甲方主体"), ("party_b", "乙方主体")):
            value = (contract_meta or {}).get(key)
            value = str(value).strip() if value else ""
            # 元数据抽取失败时可能只得到“甲方/乙方”等泛称，替换会破坏条款语义
            if len(value) >= 3 and value not in (
                    "甲方", "乙方", "买方", "卖方", "需方", "供方", "委托方", "受托方"):
                self._register(value, label)

    # ---------- 内部 ----------
    def _register(self, original: str, label: str) -> str:
        existing = self._reverse.get(original)
        if existing:
            return existing
        index = self._counter.get(label, 0) + 1
        self._counter[label] = index
        placeholder = f"【{label}】" if label in ("甲方主体", "乙方主体") else f"【{label}{index}】"
        self._mapping[placeholder] = original
        self._reverse[original] = placeholder
        return placeholder

    def _assign(self, original: str, label: str) -> str:
        with self._lock:
            self.count += 1
            return self._register(original, label)

    def _mask_text(self, text: str) -> str:
        if not text:
            return text
        # 先替换已知当事人全称（避免被公司正则重复拆解）
        for original, placeholder in list(self._reverse.items()):
            if original in text:
                self.count += text.count(original)
                text = text.replace(original, placeholder)
        text = _COMPANY_RE.sub(lambda m: self._assign(m.group(0), "主体"), text)
        text = _EMAIL_RE.sub(lambda m: self._assign(m.group(0), "邮箱"), text)
        text = _ID_RE.sub(lambda m: self._assign(m.group(0), "证件号"), text)
        text = _MOBILE_RE.sub(lambda m: self._assign(m.group(0), "手机号"), text)
        text = _ACCOUNT_RE.sub(lambda m: self._assign(m.group(0), "账号"), text)
        return text

    # ---------- 对外 ----------
    def mask_text(self, text: str) -> str:
        """脱敏一段文本（模型可见内容）。"""
        return self._mask_text(text)

    def mask_clause(self, clause: Dict) -> Dict:
        """脱敏条款副本（不改动原文，供规则引擎继续使用）。"""
        masked = dict(clause)
        masked["content"] = self._mask_text(clause.get("content") or "")
        masked["title"] = self._mask_text(clause.get("title") or "")
        return masked

    def mask_meta(self, meta: Dict) -> Dict:
        """脱敏合同公共信息（当事人、合同名）。"""
        masked = dict(meta)
        for key in ("party_a", "party_b", "contract_name"):
            if masked.get(key):
                masked[key] = self._mask_text(str(masked[key]))
        return masked

    def restore(self, obj):
        """递归还原模型输出中的所有占位符（dict/list/str 通用）。"""
        if isinstance(obj, str):
            text = obj
            for placeholder, original in self._mapping.items():
                text = text.replace(placeholder, original)
            return text
        if isinstance(obj, list):
            return [self.restore(item) for item in obj]
        if isinstance(obj, dict):
            return {key: self.restore(value) for key, value in obj.items()}
        return obj


def mask_text_once(text: str, contract_meta: Dict) -> Tuple[str, int]:
    """一次性脱敏（无还原需求，如 RAG 查询串）；返回文本与替换次数。"""
    masker = TextMasker(contract_meta)
    masked = masker.mask_text(text)
    return masked, masker.count
