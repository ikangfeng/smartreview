# =============================================================================
# 运行时自定义 LLM 配置 测试（仓储 / 客户端路由 / 连通性探测）
# =============================================================================

from db import model_config_repository as mrepo
from llm.client import LLMClient
from llm.registry import probe_llm


def test_mask_api_key():
    assert mrepo.mask_api_key("") == ""
    assert mrepo.mask_api_key("short") == "******"
    masked = mrepo.mask_api_key("sk-abcdefghijklmnopqrstuvwxyz")
    assert masked.startswith("sk-abc") and masked.endswith("wxyz") and "****" in masked


def test_profile_roundtrip_and_restore():
    # 先备份运行态配置：该用例直接读写开发库，若不还原会把用户已保存的
    # API Key 清空（CI 里跑一次 pytest 就会丢配置）
    backup = mrepo.get_profile() or {}
    try:
        saved = mrepo.save_profile(True, "http://127.0.0.1:8001/v1", "test-key", "Qwen2.5-14B-Instruct")
        assert saved["enabled"] is True
        assert saved["base_url"] == "http://127.0.0.1:8001/v1"
        assert saved["api_key"] == "test-key"
        assert saved["model"] == "Qwen2.5-14B-Instruct"

        loaded = mrepo.get_profile()
        assert loaded["enabled"] is True
        assert loaded["base_url"] == saved["base_url"]
        assert loaded["api_key"] == "test-key"
        assert loaded["updated_at"]
    finally:
        # 还原备份（而不是清空），避免污染开发库与已保存的模型配置
        mrepo.save_profile(
            bool(backup.get("enabled", False)),
            backup.get("base_url", ""),
            backup.get("api_key", ""),
            backup.get("model", ""),
        )


def test_client_custom_prepended_and_cleared():
    client = LLMClient()
    client.set_custom(True, "http://127.0.0.1:8001/v1", "custom-key", "SmartReview-FT-7B")
    assert client.custom_active() is True
    providers = client._providers_list()
    assert providers[0].name == "custom"
    assert providers[0].base_url == "http://127.0.0.1:8001/v1"
    assert providers[0].model == "SmartReview-FT-7B"

    client.set_custom(False, "http://127.0.0.1:8001/v1", "custom-key", "SmartReview-FT-7B")
    assert client.custom_active() is False
    assert client._providers_list()[0].name != "custom"


def test_probe_llm_negative_unreachable():
    """离线可跑：对必然拒绝的地址探测应快速返回 ok=False。"""
    ok, latency_ms, message = probe_llm("http://127.0.0.1:1/v1", "dummy-model", timeout=1.0)
    assert ok is False
    assert latency_ms >= 0
    assert isinstance(message, str) and message
