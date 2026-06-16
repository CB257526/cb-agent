"""
配置不同模型的特定参数
"""
class ConstantLLM:
    # 当模型没有在 llm_dict 中登记 max_tokens 时使用的兜底窗口。
    # 取 128000 是一个偏保守的中位值：临时模型 / 测试 fake model / 旧配置
    # 缺失 max_tokens 时用它兜底，避免一条配置缺失就让状态栏与自动 compact
    # 整体失效。真实模型应在 llm_dict 显式登记，不要依赖这个兜底。
    DEFAULT_MAX_TOKENS = 128000

    # agent 实际用于构造 prompt 和触发自动 compact 的安全比例。
    # 用户确认希望“取模型上下文长度的 80% 作为上下文”，剩余 20% 留给模型输出、
    # provider 侧额外开销以及 token 估算误差；后端与 TUI 都复用这个比例。
    CONTEXT_USAGE_RATIO = 0.8

    # 模型能力登记表。每个条目三个运行时字段会被真正消费：
    #   is_tool        -> cb_agents._is_able_Function_Calling，决定是否走 FC 代码路径
    #   image_ability  -> multimodal_input.model_supports_image，决定图片走原生视觉
    #                     还是降级 OCR
    #   max_tokens     -> model_max_tokens，喂给 Context% 与自动 compact 阈值
    # is_reasoning 暂时只作标注（thinking 模型），运行时未分流，保留备用。
    # max_tokens 一律填模型“上下文窗口（输入+输出总和）”的真实上限，不是单独的
    # max output；窗口数据来自各厂商官方文档（截至 2026-06）。
    llm_dict = {
        # ---- DeepSeek（OpenAI 兼容，非视觉，1M 上下文）----
        # deepseek-chat / deepseek-reasoner 已于 2026-07 并入 v4-flash 的
        # 非思考 / 思考模式，这里直接登记 v4 系列。
        "deepseek-v4-flash": {
            "is_tool": True,
            "is_reasoning": True,
            "max_tokens": 1000000,
            "image_ability": False,
        },
        "deepseek-v4-pro": {
            "is_tool": True,
            "is_reasoning": True,
            "max_tokens": 1000000,
            "image_ability": False,
        },
        # ---- 小米 MiMo（视觉 + 思考）----
        "mimo-v2.5-pro": {
            "is_tool": True,
            "is_reasoning": True,
            "max_tokens": 1000000,
            "image_ability": True,
        },
        # ---- Google Gemini（视觉，1M 上下文）----
        "gemini-3.5-flash": {
            "is_tool": True,
            "is_reasoning": False,
            "max_tokens": 1000000,
            "image_ability": True,
        },
        # ---- 阿里 Qwen（通义千问，OpenAI 兼容）----
        # Qwen3-Max：旗舰，262144 上下文，支持工具调用，非视觉。
        "qwen3-max": {
            "is_tool": True,
            "is_reasoning": False,
            "max_tokens": 262144,
            "image_ability": False,
        },
        # Qwen3.5-Plus：文本性能对标 Max，更快更省，支持图片+视频输入。
        "qwen3.5-plus": {
            "is_tool": True,
            "is_reasoning": True,
            "max_tokens": 1000000,
            "image_ability": True,
        },
        # Qwen3.5-Flash：轻量快速，1M 上下文，多模态。
        "qwen3.5-flash": {
            "is_tool": True,
            "is_reasoning": False,
            "max_tokens": 1000000,
            "image_ability": True,
        },
        # qwen-plus / qwen-flash：商业别名（已滚动升级到 Qwen3.5），1M 上下文。
        "qwen-plus": {
            "is_tool": True,
            "is_reasoning": False,
            "max_tokens": 1000000,
            "image_ability": True,
        },
        "qwen-flash": {
            "is_tool": True,
            "is_reasoning": False,
            "max_tokens": 1000000,
            "image_ability": False,
        },
        # ---- OpenAI GPT ----
        # GPT-5 系列：约 400K 上下文，支持工具调用与视觉输入。
        "gpt-5": {
            "is_tool": True,
            "is_reasoning": True,
            "max_tokens": 400000,
            "image_ability": True,
        },
        "gpt-5-mini": {
            "is_tool": True,
            "is_reasoning": True,
            "max_tokens": 400000,
            "image_ability": True,
        },
        # GPT-4.1：1M 上下文，视觉 + 工具调用。
        "gpt-4.1": {
            "is_tool": True,
            "is_reasoning": False,
            "max_tokens": 1000000,
            "image_ability": True,
        },
        # ---- Anthropic Claude ----
        # Claude 4.x（Opus / Sonnet）：标准 200K，部分支持 1M beta；这里登记
        # 稳定的 200K，需要 1M 时在 model id 上加 [1m] 后缀由 window.py 识别。
        "claude-opus-4-8": {
            "is_tool": True,
            "is_reasoning": True,
            "max_tokens": 200000,
            "image_ability": True,
        },
        "claude-sonnet-4-6": {
            "is_tool": True,
            "is_reasoning": True,
            "max_tokens": 200000,
            "image_ability": True,
        },
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
