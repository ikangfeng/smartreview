"""contract_splitter 纯逻辑单测：条款拆解（含降级）与页码定位。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from review.contract_splitter import split_clauses  # noqa: E402


SAMPLE = """采购合同

第一条　总则
本合同由甲乙双方在平等自愿的基础上订立。

第二条　合同标的
乙方向甲方提供 IT 运维服务，服务期限为一年。

第三条　违约责任
若甲方逾期付款，每日按合同总额的 0.05% 支付违约金，上限不超过合同总额的 30%。

第四条　争议解决
因本合同引起的争议，提交甲方所在地人民法院管辖。
"""


def test_split_regular_clauses():
    clauses = split_clauses(SAMPLE)
    assert len(clauses) == 4
    assert [c["clause_id"] for c in clauses] == ["第一条", "第二条", "第三条", "第四条"]
    assert clauses[0]["title"] == "总则"
    assert clauses[2]["title"] == "违约责任"
    assert "0.05%" in clauses[2]["content"]


def test_char_index_and_content_boundary():
    clauses = split_clauses(SAMPLE)
    # 第二条正文不应包含第三条内容
    assert "违约金" not in clauses[1]["content"]
    # char_index 指向条款标记起点
    assert SAMPLE[clauses[0]["char_index"]:clauses[0]["char_index"] + 3] == "第一条"


def test_fallback_paragraph_split():
    # 不含"第X条"结构的文本降级为段落硬切
    text = "段落甲。\n\n段落乙。\n\n段落丙。"
    clauses = split_clauses(text)
    assert len(clauses) >= 2
    assert clauses[0]["clause_id"] == "P1"
    assert all(c["clause_id"].startswith("P") for c in clauses)


def test_arabic_clause_number():
    text = "第10条 数据安全\n双方均应采取合理措施保护数据。\n第11条 保密义务\n双方对合同内容保密。"
    clauses = split_clauses(text)
    assert len(clauses) == 2
    assert clauses[0]["clause_id"] == "第10条"


def test_page_offset_calculation():
    # 单页文本：所有条款 page=1
    clauses = split_clauses(SAMPLE, page_offsets=[0])
    assert all(c["page"] == 1 for c in clauses)
