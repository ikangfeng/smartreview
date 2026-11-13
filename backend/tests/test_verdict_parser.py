"""verdict_parser 容错解析单测。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from review.verdict_parser import (ParseError, fallback_verdict,  # noqa: E402
                                   parse_verdict)


def test_parse_clean_json():
    text = '{"risk_level":"高","risk_description":"违约金超标","law_basis":"《民法典》第585条","suggestion":"修改","confidence":0.94}'
    v = parse_verdict(text)
    assert v["risk_level"] == "高"
    assert v["confidence"] == pytest.approx(0.94)
    assert "585" in v["law_basis"]


def test_parse_with_markdown_fence_and_noise():
    text = ('好的，审查结果如下：\n```json\n{"risk_level": "中", '
            '"risk_description": "知识产权共有", "law_basis": "", '
            '"suggestion": "改为甲方独占", "confidence": 0.8}\n```')
    v = parse_verdict(text)
    assert v["risk_level"] == "中"


def test_risk_level_coercion():
    v = parse_verdict('{"risk_level":"高风险","risk_description":"x","law_basis":"","suggestion":"","confidence":0.5}')
    assert v["risk_level"] == "高"


def test_missing_json_raises():
    with pytest.raises(ParseError):
        parse_verdict("抱歉，我无法分析该条款。")


def test_missing_description_raises():
    with pytest.raises(ParseError):
        parse_verdict('{"risk_level":"无","law_basis":"","suggestion":"","confidence":0.5}')


def test_fallback_verdict():
    v = fallback_verdict("模型超时")
    assert v["risk_level"] == "低"
    assert v["confidence"] == 0.0
    assert "人工复核" in v["risk_description"]
