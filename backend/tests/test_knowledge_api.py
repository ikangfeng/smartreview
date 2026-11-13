# =============================================================================
# 知识库管理 API 离线测试
# =============================================================================
# 只覆盖无需真实嵌入 API 的路径（空库列表/404/删除 0/白名单拒绝/参数校验），
# 保证无网络、无 SF Key 时全绿；嵌入真实调用在浏览器页面验证。
# =============================================================================

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from main import app

    with TestClient(app) as c:
        yield c


def test_kb_documents_list_empty_shape(client):
    """文档列表总是返回 {"documents": [...]}。"""
    resp = client.get("/knowledge/documents")
    assert resp.status_code == 200
    assert isinstance(resp.json()["documents"], list)


def test_kb_document_detail_missing_404(client):
    """不存在的文档详情返回 404。"""
    resp = client.get("/knowledge/document", params={"source": "/nonexist/law.txt", "category": "law"})
    assert resp.status_code == 404


def test_kb_document_detail_bad_category_422(client):
    """分类参数校验。"""
    resp = client.get("/knowledge/document", params={"source": "x.txt", "category": "other"})
    assert resp.status_code == 422


def test_kb_delete_missing_returns_zero(client):
    """删除不存在的文档返回 deleted=0（幂等，不抛错）。"""
    resp = client.delete("/knowledge/document", params={"source": "/nonexist/law.txt", "category": "law"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0


def test_kb_upload_reject_unsupported_type(client):
    """白名单外类型在上传入口即被拒绝。"""
    resp = client.post(
        "/knowledge/documents",
        files={"file": ("a.xlsx", b"x", "application/vnd.ms-excel")},
        data={"category": "law"},
    )
    assert resp.status_code == 400
    assert "不支持" in resp.json()["detail"]


def test_kb_upload_reject_bad_category(client):
    """非法分类返回 422。"""
    resp = client.post(
        "/knowledge/documents",
        files={"file": ("a.txt", "hi", "text/plain")},
        data={"category": "other"},
    )
    assert resp.status_code == 422


def test_kb_search_empty_query_422(client):
    resp = client.post("/knowledge/search", json={"query": "  "})
    assert resp.status_code == 422


def test_kb_search_offline_returns_hits_list(client):
    """离线/无 Key 时检索应优雅降级为 hits=[]，而非 500。"""
    resp = client.post("/knowledge/search", json={"query": "违约金上限", "k": 3})
    assert resp.status_code == 200
    assert isinstance(resp.json()["hits"], list)
