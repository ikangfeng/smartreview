# =============================================================================
# LLM 客户端 - 多 Provider 容错路由（改造自 zhiqida-rag core/rag_chain.py::LLMClient）
# =============================================================================
# 目标：微调权重（vLLM）与云端（SiliconFlow）共用一套 OpenAI 兼容协议。
# 行为：
#   1. 按配置顺序探测提供商（GET /models 探活，超时即视为不可用）；
#   2. 调用失败自动切换下一个提供商；
#   3. JSON 输出优先用 response_format 约束，模型不支持时自动去掉重试。
#
# 为什么需要容错路由（而不是只配一个端点）：微调模型跑在本地 vLLM，
# 可能因显存/服务未就绪而短暂不可用，若审查链路强依赖它，一次故障
# 就会拖垮整条链路。因此把"本地微调"当首选、"云端通用模型"当兜底：
# 探测失败或调用异常时自动降级，用"结果可能来自不同模型"换取"可用性"。
#
# 超时/重试策略（教学点）：SDK 内层自动重试一律关闭（max_retries=0），
# 改由本层按 provider 维度切换。理由：对同一超时/故障端点做内层重试，
# 只会成倍放大端到端延迟而大概率仍失败；换一个提供商才是有效恢复。
# 超时则统一收敛到 settings.llm_timeout_seconds，防止调用方各设阈值
# 导致"快端点被慢超时拖死"或"慢推理被快超时误杀"。
# =============================================================================

from dataclasses import dataclass
from typing import List, Optional, Tuple

from config.settings import settings
from config.logger import get_logger

log = get_logger("llm")


def _usage_dict(resp) -> dict:
    """从 OpenAI 兼容响应中提取 token 用量；端点不返回时给零值。

    为何不直接透传 resp.usage：不同兼容实现返回结构不一（可能为 None、
    也可能缺字段），统一收敛成三个确定的整数字段，下游累加才安全。
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(u, "total_tokens", 0) or 0),
    }


@dataclass
class LLMResult:
    """一次成功调用的结果：正文 + 实际命中的提供商 + token 用量。

    记录 provider 不只是信息性字段：审查方/日志可据此判断"这份回答
    来自本地微调模型还是云端降级"，用于后续质量归因与告警。

    usage 用于成本核算：兼容端点通常返回 prompt/completion/total_tokens，
    个别本地端点可能不返回，此时 usage 为空字典——调用方必须按"缺省 0"
    处理，绝不能因为缺用量就判定调用失败。
    """

    text: str
    provider: str  # "vllm" | "siliconflow" | "custom" | "canary"
    usage: dict = None  # {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
    variant: str = "baseline"  # "baseline" | "canary"（灰度成功后标记）

    def __post_init__(self):
        """缺省填空字典：调用方无需到处判 None。"""
        if self.usage is None:
            self.usage = {}


class Provider:
    """单个 OpenAI 兼容提供商。"""

    def __init__(self, name: str, base_url: str, api_key: str, model: str,
                 timeout: int):
        """描述一个 OpenAI 兼容端点；timeout 为该端点的单次请求超时（秒）。"""
        self.name = name
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        """惰性构造并复用 OpenAI SDK 客户端。

        惰性：只有真正要探测/调用时才建连接，避免空转资源；复用（而非
        每次 new）则省去重复建连开销。注意此层不持有"可用性"状态，
        探测结果一律由上层 LLMClient 统一缓存。
        """
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.api_key or "sk-local",  # vLLM 通常不校验 Key
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=0,  # 内层重试关闭，恢复手段在上层按 provider 切换（见模块头）
            )
        return self._client

    def is_available(self) -> bool:
        """探测可用性：能列出模型即视为可用。

        为什么用 GET /models 而不是发一条消息：选路探测要"快且廉价"——
        不触发推理、不消耗 token/配额，5s 短超时让故障端点尽快被淘汰，
        保证探测自身不成为请求的瓶颈。
        """
        try:
            client = self._get_client()
            client.models.list(timeout=5)
            return True
        except Exception:
            return False

    def chat(self, messages: List[dict], json_mode: bool) -> Tuple[str, dict]:
        """发起一次对话补全并返回 (文本, usage)；失败抛异常，由上层切换提供商。

        json_mode=True 时优先用 response_format 约束输出为合法 JSON；
        兼容端点不支持该参数时会抛错，此时退化重试一次普通补全——
        之所以要匹配错误子串才退化：避免把 5xx/限流等"真异常"也当作
        "不支持 JSON 模式"误吞，导致静默产出非 JSON 文本。

        返回 usage 是为了成本核算：调用方（scheduler）把它按条款累加到
        任务级，最终向用户展示"这份合同花了多少 token / 多少钱"。
        """
        client = self._get_client()
        kwargs = dict(model=self.model, messages=messages,
                      temperature=settings.llm_temperature,
                      max_tokens=settings.llm_max_tokens)
        if json_mode:
            try:
                resp = client.chat.completions.create(
                    **kwargs, response_format={"type": "json_object"})
                return resp.choices[0].message.content or "", _usage_dict(resp)
            except Exception as e:
                # 部分兼容端点不支持 response_format：去掉后重试一次
                if "response_format" in str(e).lower() or "invalid" in str(e).lower() \
                        or "not supported" in str(e).lower():
                    resp = client.chat.completions.create(**kwargs)
                    return resp.choices[0].message.content or "", _usage_dict(resp)
                raise
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or "", _usage_dict(resp)


class LLMClient:
    """多 Provider 容错路由客户端。

    支持"运行时自定义提供商"（通常指向本地 vLLM 部署的微调模型）：
    通过 set_custom() 传入 OpenAI 兼容端点，自定义配置存在时排在最前，
    失败仍会按序降级到 .env 配置的默认链（vllm → siliconflow）。
    """

    def __init__(self):
        """初始化空路由客户端：候选链与可用缓存均为空，首次调用时按配置惰性构建。"""
        # 三个字段都是"派生缓存"而非重复配置，set_custom() 时统一失效：
        # _providers（候选链）与 _available_cache（上次成功者）都随配置
        # 变化整体重建，保证"新配置立即生效、旧探测结论不残留"。
        self._custom: Optional[dict] = None  # 运行时自定义 provider（DB 配置注入）
        self._providers: Optional[List[Provider]] = None
        self._available_cache: Optional[str] = None  # 最近一次成功调用的 provider 名
        # 失败冷却表 {provider_name: 恢复时间戳}：并发场景下仅靠"上次成功"排序不够——
        # 8 个线程同时起跑时谁都还没有成功记录，会各自白试一次坏端点（实测 7 条款
        # 各吃一次 vllm 502）。失败一次即冷却，让后续请求直接跳过，避免成倍放大延迟。
        self._cooldown: dict = {}
        # 灰度发布：canary 端点只在被选中的任务上优先尝试，失败自动回落基线链；
        # 连续失败达阈值即熔断 10 分钟，避免把整批审查拖成"每条都先等一次坏端点"
        self._canary: Optional[dict] = None
        self._canary_fail: int = 0
        self._canary_open_until: float = 0.0

    def set_custom(self, enabled: bool, base_url: str, api_key: str, model: str) -> None:
        """设置/清除运行时自定义提供商并重置探测缓存。

        置空条件（enabled=False 或 base/model 空）与 registry 的注入逻辑
        对齐：任何时刻调用都安全，关停即自动回落 .env 默认链。
        """
        if enabled and base_url.strip() and model.strip():
            self._custom = {
                "base_url": base_url.strip(),
                "api_key": api_key.strip(),
                "model": model.strip(),
            }
        else:
            self._custom = None
        # 配置变了，候选链与"上次成功"结论都可能已过时，必须一起作废，
        # 否则下次 _chat 会按旧端点列表路由或命中旧 provider 缓存。
        self._providers = None
        self._available_cache = None

    def custom_active(self) -> bool:
        """是否处于"自定义模型"启用状态（无需网络探测）。"""
        return self._custom is not None

    def custom_info(self) -> Optional[dict]:
        """返回当前自定义 provider 配置的副本；未启用时返回 None。

        返回副本而非内部引用，避免调用方无意中改坏内部状态。
        """
        return dict(self._custom) if self._custom else None

    # ---------------- 灰度发布（canary） ----------------
    def set_canary(self, enabled: bool, percent: int, base_url: str,
                   api_key: str, model: str) -> None:
        """设置/关闭灰度端点；percent=0 或端点不全即视为关闭。"""
        percent = min(100, max(0, int(percent or 0)))
        if enabled and percent > 0 and base_url.strip() and model.strip():
            self._canary = {
                "percent": percent,
                "base_url": base_url.strip(),
                "api_key": api_key.strip(),
                "model": model.strip(),
            }
        else:
            self._canary = None
        # 配置变更即重置熔断，让新配置立刻有机会被验证
        self._canary_fail = 0
        self._canary_open_until = 0.0

    def canary_active(self) -> bool:
        return self._canary is not None

    def canary_status(self) -> dict:
        """供 /health 与模型配置页展示：是否启用、分流比例、是否熔断中。"""
        import time as _time
        if not self._canary:
            return {"active": False, "percent": 0, "circuit_open": False}
        return {
            "active": True,
            "percent": self._canary["percent"],
            "circuit_open": _time.time() < self._canary_open_until,
        }

    def pick_variant(self, task_id: str) -> str:
        """按任务稳定分流：同一 task_id 永远得到同一变体（便于复现与归因）。

        用哈希而非随机数：任务重试/重新查询时不会在两个模型间跳变，
        保证"一份合同的所有条款由同一模型审查"这一前提成立。
        """
        import hashlib
        import time as _time
        if not self._canary or _time.time() < self._canary_open_until:
            return "baseline"
        bucket = int(hashlib.sha256(str(task_id).encode()).hexdigest()[:8], 16) % 100
        return "canary" if bucket < self._canary["percent"] else "baseline"

    def _canary_provider(self) -> "Provider":
        c = self._canary or {}
        return Provider("canary", c.get("base_url", ""), c.get("api_key", ""),
                        c.get("model", ""), settings.llm_timeout_seconds)

    def _build_providers(self) -> List[Provider]:
        """构造候选链：自定义端点排最前，其后按 .env 顺序接默认链。"""
        providers: List[Provider] = []
        if self._custom:
            providers.append(Provider("custom", self._custom["base_url"],
                                      self._custom["api_key"], self._custom["model"],
                                      settings.llm_timeout_seconds))
        for name in settings.provider_names:
            if name == "vllm":
                providers.append(Provider("vllm", settings.vllm_base_url,
                                          settings.vllm_api_key, settings.vllm_model,
                                          settings.llm_timeout_seconds))
            elif name == "siliconflow":
                if settings.sf_api_key:  # 未配 Key 直接跳过云端：纳入只会白等一轮 401 才降级
                    providers.append(Provider("siliconflow", settings.sf_base_url,
                                              settings.sf_api_key, settings.sf_model,
                                              settings.llm_timeout_seconds))
        return providers

    def _providers_list(self) -> List[Provider]:
        """惰性构建并缓存候选链：配置未变时复用，避免每次调用重复构造。"""
        if self._providers is None:
            self._providers = self._build_providers()
        return self._providers

    def active_provider(self) -> str:
        """探测当前可用提供商（用于 /health 展示），全部不可用返回 none。"""
        info = self.active_info()
        return info[0] if info else "none"

    def active_info(self) -> Optional[Tuple[str, str]]:
        """返回首个可用提供商的 (名称, 模型)，全部不可用返回 None。

        副作用有意保留：探测失败即写入冷却表、成功即写入"上次成功"缓存。
        /health 与启动预热都会走到这里，因此第一批并发审查无需各自
        白试坏端点（否则 8 线程 × 每个坏端点一次失败）。
        """
        import time as _time
        now = _time.time()
        for p in self._providers_list():
            if p.is_available():
                self._available_cache = p.name
                return (p.name, p.model)
            self._cooldown[p.name] = now + 120
        return None

    def chat_json(self, messages: List[dict], variant: str = "baseline") -> LLMResult:
        """以 JSON 模式调用；按序尝试各提供商。全部失败抛出 RuntimeError。"""
        return self._chat(messages, json_mode=True, variant=variant)

    def chat(self, messages: List[dict], variant: str = "baseline") -> LLMResult:
        """以普通模式调用；按序尝试各提供商。全部失败抛出 RuntimeError。"""
        return self._chat(messages, json_mode=False, variant=variant)

    def _chat(self, messages: List[dict], json_mode: bool,
              variant: str = "baseline") -> LLMResult:
        """调度入口：灰度优先 → 基线链逐个尝试，任一成功即返回；全败抛 RuntimeError。"""
        import time as _time
        errors = []
        providers = self._providers_list()

        # 灰度：被选中的任务先用 canary 端点；失败不阻断，继续走基线链兜底。
        # 连续失败 3 次熔断 10 分钟——否则一次坏配置会让半数任务每条都白等一轮。
        if variant == "canary" and self._canary and _time.time() >= self._canary_open_until:
            try:
                text, usage = self._canary_provider().chat(messages, json_mode=json_mode)
                self._canary_fail = 0
                return LLMResult(text=text, provider="canary", usage=usage, variant="canary")
            except Exception as e:
                self._canary_fail += 1
                errors.append(f"canary: {e}")
                log.warning("canary_failed", extra={
                    "fail_count": self._canary_fail, "error": str(e)[:200]})
                if self._canary_fail >= 3:
                    self._canary_open_until = _time.time() + 600
                    log.error("canary_circuit_open", extra={
                        "cooldown_s": 600, "fail_count": self._canary_fail})

        if not providers:
            raise RuntimeError("未配置任何可用的 LLM 提供商（请检查 backend/.env）")

        # 把"上一次成功"的提供商排最前：本地 vLLM 通常长期稳定，命中缓存
        # 可让大多数请求跳过重复探测直达目标，显著压低平均延迟；缓存只在
        # 真正成功后更新，因此"上次故障的端点"不会残留在最前位。
        ordered = providers
        if self._available_cache:
            ordered = sorted(providers,
                             key=lambda p: 0 if p.name == self._available_cache else 1)

        # 冷却期内的端点直接跳过（冷却表为空时等价于原候选链）。
        # 若全部都在冷却，则回退到完整链——宁可再试一次，也不能无端点可用。
        import time as _time
        now = _time.time()
        alive = [p for p in ordered if self._cooldown.get(p.name, 0) < now]
        if alive:
            ordered = alive

        # 单端点失败不重试、直接切下一个：多数故障（超时/5xx/鉴权）在同一
        # 端点重试仍会复现（重试策略见模块头）；全败时把各端点错误合并成
        # 一条 RuntimeError 抛出，让上层日志一眼看清整条链各自的失败原因。
        for p in ordered:
            try:
                text, usage = p.chat(messages, json_mode=json_mode)
                self._available_cache = p.name
                return LLMResult(text=text, provider=p.name, usage=usage)
            except Exception as e:
                errors.append(f"{p.name}: {e}")
                # 失败即冷却 120 秒：并发线程随后会跳过该端点，不再逐条白等
                self._cooldown[p.name] = _time.time() + 120
                # 记录每一次降级切换：这是排查"结果为什么来自云端而非本地微调"的唯一线索
                log.warning("provider_failed", extra={
                    "provider": p.name, "model": p.model, "error": str(e)[:200],
                    "cooldown_s": 120})
        raise RuntimeError("所有 LLM 提供商均调用失败。详情: " + " | ".join(errors))


# 模块级单例：全进程共享同一路由客户端，让自定义配置、候选链与"上次成功"
# 缓存只维护一份；业务侧 import llm_client 直接用，无需各自 new 导致状态分叉。
llm_client = LLMClient()
