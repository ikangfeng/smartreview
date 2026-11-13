# =============================================================================
# 元数据提取 - metadata_extractor.py
# =============================================================================
# 从合同文本正则提取：合同名称 / 甲方 / 乙方 / 合同金额。
# 约定：提取结果仅在内存中传递，绝不写入日志（PRD 7.2 敏感信息要求）。
# =============================================================================

import os
import re
from typing import Optional


def extract_contract_name(full_text: str, filename: str = "") -> str:
    """提取合同名称：优先匹配“合同名称：xxx”显式字段，退化到正文头部短行，最后用文件名兜底。"""
    m = re.search(r"(?:合同名称|合同名|项目名称)\s*[为是：:]\s*([^\n]{2,60})", full_text)
    if m:
        return m.group(1).strip()
    # 取条款正文前的首个非空短行作为名称候选
    head = full_text.strip().split("\n", 8)[:8]
    for line in head:
        line = line.strip()
        if 2 <= len(line) <= 60 and not line.startswith("第"):
            return line
    base = os.path.splitext(os.path.basename(filename))[0]
    return base or "未命名合同"


def _extract_party(full_text: str, labels: tuple) -> Optional[str]:
    """按“标签：主体”模式提取当事人名称；labels 为该角色的同义标签（甲方/委托方/买方…）。"""
    # 主格式：甲方（简称）？：极电科技有限公司 —— 括号内容受限，避免贪婪吞文
    pattern = re.compile(
        rf"(?:{'|'.join(labels)})\s*(?:[（(][^）)]{{0,20}}[)）])?\s*[：:]\s*"
        rf"([^\s，,。；;\n]{{2,40}})")
    for m in pattern.finditer(full_text):
        return m.group(1).strip()
    # 兜底：无冒号形式，如"甲方 某某公司"（限制在行首 300 字内避免误抓）
    pattern2 = re.compile(
        rf"(?:{'|'.join(labels)})\s+([\u4e00-\u9fa5A-Za-z0-9（）()]{{2,40}})")
    for m in pattern2.finditer(full_text[:1000]):
        return m.group(1).strip()
    return None


def extract_party_a(full_text: str) -> Optional[str]:
    """提取甲方（含委托方/买方/定作人等甲方同义角色）。"""
    return _extract_party(full_text, ("甲方", "委托方", "买方", "定作人"))


def extract_party_b(full_text: str) -> Optional[str]:
    """提取乙方（含受托方/卖方/承揽人等乙方同义角色）。"""
    return _extract_party(full_text, ("乙方", "受托方", "卖方", "承揽人"))


def extract_total_amount(full_text: str) -> Optional[str]:
    """提取合同总金额，返回规范化的可读字符串，如 '500万元'。"""
    patterns = [
        r"合同(?:总)?(?:金额|价款|总价|总额|价)\s*[为是：:]\s*"
        r"(?:人民币\s*)?([0-9,，]+(?:\.[0-9]+)?)\s*(亿元|万元|元|万)?",
        r"([¥￥])\s*([0-9,，]+(?:\.[0-9]+)?)\s*(亿元|万元|元|万)?",
    ]
    for pat in patterns:
        m = re.search(pat, full_text)
        if not m:
            continue
        groups = m.groups()
        if groups[0] in ("¥", "￥"):
            amount = groups[1]
            unit = groups[2] or ""
        else:
            amount = groups[0]
            unit = groups[1] or ""
        clean = amount.replace(",", "").replace("，", "")
        if not clean or clean.startswith("0"):
            continue
        return f"{amount}{unit}"
    return None


def extract_metadata(full_text: str, filename: str = "") -> dict:
    """统一入口。返回：contract_name/party_a/party_b/total_amount。"""
    return {
        "contract_name": extract_contract_name(full_text, filename),
        "party_a": extract_party_a(full_text),
        "party_b": extract_party_b(full_text),
        "total_amount": extract_total_amount(full_text),
    }
