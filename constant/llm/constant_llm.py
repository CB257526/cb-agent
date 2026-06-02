"""
配置不同模型的特定参数
"""
class ConstantLLM:
    # 当模型没有在 llm_dict 中登记 max_tokens 时使用的兜底窗口。
    # 这里保留 8000 是为了兼容测试 fake model、临时模型或旧配置，避免因为
    # 一条模型配置缺失就让状态栏/自动 compact 整体失效。
    DEFAULT_MAX_TOKENS = 8000

    # agent 实际用于构造 prompt 和触发自动 compact 的安全比例。
    # 用户确认希望“取模型上下文长度的 80% 作为上下文”，剩余 20% 留给模型输出、
    # provider 侧额外开销以及 token 估算误差；后端与 TUI 都复用这个比例。
    CONTEXT_USAGE_RATIO = 0.8

    llm_dict = {
        "deepseek-v4-flash": {
            "is_tool": True,
            "is_reasoning": True,
            "json_output": True,
            "max_tokens": 1000000,
            "image_ability": False,
        },
        "deepseek-v4-pro": {
            "is_tool": True,
            "is_reasoning": True,
            "json_output": True,
            "max_tokens": 1000000,
            "image_ability": False,
        },
        "mimo-v2.5-pro": {
            "is_tool": True,
            "is_reasoning": True,
            "json_output": True,
            "max_tokens": 1000000,
            "image_ability": True,
        }

    }

    @classmethod
    def model_max_tokens(cls, model: str | None, default: int | None = None) -> int:
        """读取模型真实上下文窗口长度。

        这个方法是所有“上下文窗口”相关逻辑的统一入口。之前很多地方写死 8000，
        会导致 TUI 显示、ContextBuilder 预算和自动 compact 阈值互相不一致。
        现在只要在 ``llm_dict[model]["max_tokens"]`` 中更新模型窗口，调用方就会
        自动使用最新值。
        """
        fallback = int(default or cls.DEFAULT_MAX_TOKENS)
        if not model:
            return fallback
        config = cls.llm_dict.get(model)
        if not isinstance(config, dict):
            return fallback
        try:
            max_tokens = int(config.get("max_tokens") or fallback)
        except Exception:
            return fallback
        return max(max_tokens, 1)

    @classmethod
    def context_window_tokens(
        cls,
        model: str | None,
        ratio: float | None = None,
        default: int | None = None,
    ) -> int:
        """返回 agent 可安全使用的上下文预算。

        分子来自模型真实窗口，分母使用 ``CONTEXT_USAGE_RATIO``，默认是 80%。
        自动 compact 的触发阈值、TUI 底部 Context 指标以及启动时的
        ContextBuilder 配置都应该使用这个值，避免“显示没满但请求已超窗”或
        “显示很满但后端还不压缩”的错位。
        """
        max_tokens = cls.model_max_tokens(model, default=default)
        safe_ratio = cls.CONTEXT_USAGE_RATIO if ratio is None else ratio
        try:
            safe_ratio = float(safe_ratio)
        except Exception:
            safe_ratio = cls.CONTEXT_USAGE_RATIO
        safe_ratio = min(max(safe_ratio, 0.01), 1.0)
        return max(1, int(max_tokens * safe_ratio))
