"""rules 规则引擎纯逻辑单测（确定性判断通道）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rules import (build_rule_verdict, evaluate_clause_rules,  # noqa: E402
                   match_ip_rights, match_keyword_any, match_percent_ceiling)


def _rule(kind, params=None, level="中"):
    return {"matcher_kind": kind, "params": params or {}, "risk_level": level,
            "name": "t", "description": "d", "suggestion": "s"}


def test_percent_ceiling_hit_above_threshold():
    rule = _rule("percent_ceiling", {
        "regex": r"(?:上限|最高|不超过)[^。\n]{0,40}?(\d+(?:\.\d+)?)\s*%",
        "threshold": 20.0}, level="高")
    assert match_percent_ceiling("违约金上限不超过合同总额的30%", rule["params"]) is True
    assert match_percent_ceiling("违约金上限不超过合同总额的10%", rule["params"]) is False


def test_ip_rights_logic():
    assert match_ip_rights("知识产权归双方共有") is True
    assert match_ip_rights("知识产权归乙方所有") is True
    assert match_ip_rights("知识产权归甲方独占所有") is False
    assert match_ip_rights("本合同不含知识产权条款") is False


def test_keyword_any():
    rule = _rule("keyword_any", {"keywords": ["乙方所在地人民法院", "仲裁"]})
    assert match_keyword_any("争议由乙方所在地人民法院管辖", rule["params"]) is True
    assert match_keyword_any("争议由甲方所在地人民法院管辖", rule["params"]) is False


def test_evaluate_returns_highest_severity():
    rules = [
        _rule("keyword_any", {"keywords": ["乙方所在地法院"]}, level="中"),
        _rule("percent_ceiling", {
            "regex": r"(?:上限|最高|不超过)[^。\n]{0,40}?(\d+(?:\.\d+)?)\s*%",
            "threshold": 20.0}, level="高"),
    ]
    verdict = evaluate_clause_rules("违约金上限为合同总额的30%；争议由乙方所在地法院管辖", rules)
    assert verdict is not None
    assert verdict["risk_level"] == "高"  # 高 > 中


def test_evaluate_no_hit_returns_none():
    rules = [_rule("keyword_any", {"keywords": ["乙方所在地法院"]})]
    assert evaluate_clause_rules("本条款为正常履约条款", rules) is None


def test_build_rule_verdict_shape():
    v = build_rule_verdict(_rule("ip_rights", level="中"))
    assert set(v) >= {"risk_level", "risk_description", "law_basis", "suggestion", "confidence"}
    assert v["risk_level"] == "中"
    assert v["confidence"] == 0.95
