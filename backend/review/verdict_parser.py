# =============================================================================
# 裁决 JSON 解析 - verdict_parser.py
# =============================================================================
# 容错解析 LLM 输出：剥离 Markdown 围栏/多余文字 → 取首个 JSON 对象 → 校验字段。
# 保障 PRD"JSON 输出格式合规率≥98%"目标；失败抛 ParseError 交由上层重试。
# =============================================================================

import json
import re
from typing import Optional

ALLOWED_RISK_LEVELS = {"高", "中", "低", "无"}


class ParseError(Exception):
    """LLM 输出无法解析为合法裁决。"""


def extract_json_object(text: str) -> dict:
    """从任意文本中提取首个 JSON 对象。"""
    # 移除 Markdown 代码块围栏
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ParseError("未找到 JSON 对象")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ParseError(f"JSON 解码失败: {e}")


def _coerce_risk_level(value) -> str:
    """把 LLM 输出的风险等级收敛到 {高,中,低,无}：兼容“高风险”等口语写法，非法值一律兜底为“无”。"""
    if not isinstance(value, str):
        return "无"
    v = value.strip().replace("高风险", "高").replace("中风险", "中") \
        .replace("低风险", "低").replace("无风险", "无")
    if v in ALLOWED_RISK_LEVELS:
        return v
    return "无"


def parse_verdict(text: str) -> dict:
    """
    将 LLM 输出解析为规范化 verdict：
    {risk_level, risk_description, law_basis, suggestion, confidence}
    """
    raw = extract_json_object(text)
    risk_level = _coerce_risk_level(raw.get("risk_level"))
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5

    verdict = {
        "risk_level": risk_level,
        "risk_description": str(raw.get("risk_description") or "").strip(),
        "law_basis": str(raw.get("law_basis") or "").strip(),
        "suggestion": str(raw.get("suggestion") or "").strip(),
        "confidence": confidence,
        "quality_issues": _parse_quality_issues(raw.get("quality_issues")),
        "reasonableness_issues": _parse_reasonableness_issues(
            raw.get("reasonableness_issues")),
        "revised_text": _clean_revised_text(raw.get("revised_text")),
    }
    if not verdict["risk_description"]:
        raise ParseError("缺少 risk_description 字段")
    return verdict


# 文本质量问题的类型白名单：超出范围的归入"其他"，避免前端出现未知分类
QUALITY_TYPES = {"错别字", "语义歧义", "表述空泛", "术语不一致",
                 "数值冲突", "格式异常", "指代不明", "其他"}
_MAX_QUALITY_ITEMS = 8  # 单条条款最多保留 8 个质量问题，防止模型刷屏


def _parse_quality_issues(value) -> list:
    """把 LLM 输出的 quality_issues 收敛为规范结构。

    模型可能返回 list / JSON 字符串 / None，也常漏字段或类型超纲，
    这里统一兜底：只保留必要字段、类型收敛到白名单、severity 收敛到高中低。
    """
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if not isinstance(value, list):
        return []

    out = []
    for item in value[:_MAX_QUALITY_ITEMS]:
        if item is None:
            continue  # 模型偶尔吐 null 占位，直接跳过（否则 str(None) 会变成 "None" 文本）
        if not isinstance(item, dict):
            # 模型偶尔直接吐字符串数组，视为"其他"类问题的描述
            text = str(item).strip()
            if text:
                out.append({"type": "其他", "original": "", "issue": text,
                            "suggestion": "", "severity": "低"})
            continue
        issue_text = str(item.get("issue") or item.get("description") or "").strip()
        original = str(item.get("original") or item.get("text") or "").strip()
        if not issue_text and not original:
            continue  # 空条目直接丢弃
        qtype = str(item.get("type") or "其他").strip()
        severity = str(item.get("severity") or "低").strip().replace("风险", "")
        if severity not in {"高", "中", "低"}:
            severity = "低"
        out.append({
            "type": qtype if qtype in QUALITY_TYPES else "其他",
            "original": original,
            "issue": issue_text or "表述存在问题",
            "suggestion": str(item.get("suggestion") or "").strip(),
            "severity": severity,
        })
    return out


def fallback_verdict(reason: str) -> dict:
    """模型不可用/解析失败的兜底裁决：明确标注人工复核，不进高风险统计。"""
    return {
        "risk_level": "低",
        "risk_description": f"未能完成自动判断（{reason}），建议人工复核该条款。",
        "law_basis": "",
        "suggestion": "请人工复核条款内容。",
        "confidence": 0.0,
        "quality_issues": [],
        "reasonableness_issues": [],
        "revised_text": "",
    }


def _clean_revised_text(value) -> str:
    """清洗模型返回的改写条款：去代码围栏、去空白、限制长度，非字符串一律置空。"""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"```(?:json|text)?", "", value).strip()
    # 模型偶尔把整段 JSON 塞进来，这里只保留纯文本
    if text.startswith("{") and text.endswith("}"):
        return ""
    return text[:4000]


# 合理性问题类型白名单与方向枚举（与 prompts 中的 schema 保持一致）
REASON_TYPES = {"权利义务失衡", "责任不对等", "付款交付不匹配",
                "条款不可执行", "期限不合理", "其他"}
REASON_DIRECTIONS = {"对甲方不利", "对乙方不利", "双方失衡"}
_MAX_REASON_ITEMS = 6


def _parse_reasonableness_issues(value) -> list:
    """把模型输出的 reasonableness_issues 收敛为规范结构。

    模型常漏 direction、类型超纲或返回字符串数组，这里统一兜底：
    类型收敛到白名单、方向缺省为"对甲方不利"、severity 收敛到高中低。
    """
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if not isinstance(value, list):
        return []

    out = []
    for item in value[:_MAX_REASON_ITEMS]:
        if item is None:
            continue
        if not isinstance(item, dict):
            text = str(item).strip()
            if text:
                out.append({"type": "其他", "direction": "对甲方不利", "original": "",
                            "issue": text, "suggestion": "", "severity": "低"})
            continue
        issue_text = str(item.get("issue") or item.get("description") or "").strip()
        original = str(item.get("original") or item.get("text") or "").strip()
        if not issue_text and not original:
            continue
        rtype = str(item.get("type") or "其他").strip()
        direction = str(item.get("direction") or "对甲方不利").strip()
        severity = str(item.get("severity") or "低").strip().replace("风险", "")
        if severity not in {"高", "中", "低"}:
            severity = "低"
        out.append({
            "type": rtype if rtype in REASON_TYPES else "其他",
            "direction": direction if direction in REASON_DIRECTIONS else "对甲方不利",
            "original": original,
            "issue": issue_text or "商业安排存在失衡",
            "suggestion": str(item.get("suggestion") or "").strip(),
            "severity": severity,
        })
    return out
