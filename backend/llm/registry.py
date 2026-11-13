# =============================================================================
# 自定义 LLM 提供商标记 - registry.py
# =============================================================================
# 职责：
#   1. apply_custom_to_client()：把 llm_profile（DB 运行时配置）注入全局
#      llm_client，使自定义 OpenAI 兼容端点（典型 = 本地 vLLM 微调模型）
#      排到默认链之前生效；
#   2. probe_llm()：用一段最小对话对指定端点做连通性/模型名验证，
#      供"模型配置"页"测试连接"按钮与 API 使用。
#
# 为什么把配置放 DB 而不是 .env：微调模型的 base_url/key 属"运行时可变"
# 数据（训练迭代后端点/模型名常换），存 DB 后可在"模型配置"页热切换，
# 进程启动与保存后即时注入（见 main.lifespan / PUT /llm/config），
# 无需改环境变量或重启后端。
# =============================================================================

import time
from typing import Optional, Tuple

from config.settings import settings
from llm.client import LLMClient, llm_client


def apply_custom_to_client(client: LLMClient = llm_client) -> bool:
    """从 DB 读取 llm_profile 并注入 client；返回自定义配置是否已生效。

    调用时机：应用启动时（main.lifespan）与"模型配置"页每次保存后各一次，
    让新配置立即作用于下一次审查调用；通过 client.set_custom() 热注入，
    无需重启进程。未配置过时 get_profile() 回退 EMPTY_PROFILE（关停态），
    因此本函数幂等，任何时刻调用都安全。
    """
    from db import model_config_repository as mrepo
    profile = mrepo.get_profile()
    # enabled=False 或 base/model 为空时 set_custom() 内部会置空自定义，
    # 使调用链自动回落 .env 默认提供商——"关停/改回默认"无需特殊分支。
    client.set_custom(profile["enabled"], profile["base_url"],
                      profile["api_key"], profile["model"])
    return client.custom_active()


def apply_canary_to_client(client: LLMClient = llm_client) -> bool:
    """把 DB 中的灰度配置注入 client；返回灰度是否生效。"""
    from db import model_config_repository as mrepo
    cfg = mrepo.get_canary()
    client.set_canary(cfg["enabled"], cfg["percent"], cfg["base_url"],
                      cfg["api_key"], cfg["model"])
    return client.canary_active()


def probe_llm(base_url: str, model: str, api_key: Optional[str] = None,
              timeout: Optional[float] = None) -> Tuple[bool, float, str]:
    """最小对话探测：返回 (是否成功, 耗时毫秒, 响应文本/错误信息)。

    与 client.Provider.is_available() 的差异（教学点）：后者只 GET /models
    选路（轻量、不消耗推理），这里发一次真实对话——"测试连接"是用户在
    填完新端点后的显式动作，需要一并验证"模型名存在 + 推理链路通"，
    代价是可接受的一次性推理耗时。
    """
    timeout = timeout or settings.llm_timeout_seconds
    started = time.perf_counter()
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=(api_key or "").strip() or "sk-local",  # vLLM 通常不校验 Key
            base_url=base_url.strip(),
            timeout=timeout,
            max_retries=0,  # 探测只判一次：SDK 重试会拖长前端"转圈"时间
        )
        # temperature=0 / max_tokens=16：要的是"最短的确定性回复"，
        # 内容本身无意义，只证明服务可用，同时压低探测耗时与资源占用
        resp = client.chat.completions.create(
            model=model.strip(),
            messages=[{"role": "user", "content": "请只回复：连接成功"}],
            temperature=0,
            max_tokens=16,
        )
        text = (resp.choices[0].message.content or "").strip()
        latency = (time.perf_counter() - started) * 1000
        return True, round(latency, 1), text[:100]
    except Exception as e:  # 网络/鉴权/模型名错误统一上报给前端展示
        latency = (time.perf_counter() - started) * 1000
        return False, round(latency, 1), f"{type(e).__name__}: {e}"
