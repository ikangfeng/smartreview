# =============================================================================
# 修订后全文一致性校验 测试（编号重复/断层、交叉引用悬空）
# =============================================================================

from review.revision_checker import _cn_to_int, check_revised_document


def test_cn_numeral_conversion():
    assert _cn_to_int("三") == 3
    assert _cn_to_int("十") == 10
    assert _cn_to_int("十二") == 12
    assert _cn_to_int("二十") == 20
    assert _cn_to_int("一百零五") == 105
    assert _cn_to_int("35") == 35


def test_duplicate_heading_and_dangling_reference():
    text = (
        "第一条 总则\n"
        "双方按第二条规定执行。\n"
        "第二条 付款\n"
        "付款依据第三条处理。\n"
        "第二条 付款重复\n"
    )
    types = {i["type"] for i in check_revised_document(text)}
    assert "编号冲突" in types
    assert "引用悬空" in types


def test_clean_document_passes():
    text = "第一条 总则\n第二条 付款依据第一条执行。"
    assert check_revised_document(text) == []


def test_empty_text_returns_empty():
    assert check_revised_document("") == []
