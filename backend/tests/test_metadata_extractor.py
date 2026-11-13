"""metadata_extractor 元数据提取单测。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from review.metadata_extractor import (extract_metadata, extract_party_a,  # noqa: E402
                                       extract_party_b, extract_total_amount)


TEXT = """采购合同

甲方：极电科技有限公司
乙方：深圳市供应商股份有限公司

合同总金额：人民币500万元

第一条 合同标的
乙方提供 IT 运维服务。
"""


def test_extract_parties():
    assert extract_party_a(TEXT) == "极电科技有限公司"
    assert extract_party_b(TEXT) == "深圳市供应商股份有限公司"


def test_extract_amount():
    assert extract_total_amount(TEXT) == "500万元"


def test_extract_metadata_shape():
    meta = extract_metadata(TEXT, "采购合同.pdf")
    assert meta["contract_name"] == "采购合同"
    assert meta["party_a"] == "极电科技有限公司"
    assert meta["total_amount"] is not None


def test_missing_fields_return_none():
    assert extract_party_a("无双方信息的纯文本") is None
    assert extract_total_amount("没有金额信息") is None
