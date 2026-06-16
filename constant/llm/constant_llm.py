"""
配置不同模型的特定参数
"""
import os
from typing import Optional


def _parse_bool_env(raw: Optional[str]) -> Optional[bool]:
    """把环境变量字符串解析成布尔。无法识别时返回 None(表示"未配置")。

    接受 true/false/1/0/yes/no/on/off(大小写不敏感)。这样 IS_TOOL="True"、
    IMAGE_ABILITY="False" 这类 .env 写法都能正确识别;空串或乱填则视为未配置,
    回退到 llm_dict / 默认值。
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return None


def _parse_token_count_env(raw: Optional[str]) -> Optional[int]:
    """把环境变量里的 token 数解析成 int,支持 K/M 后缀。无法识别返回 None。

    支持 "1024K" / "1M" / "200000" 这类写法(大小写不敏感,允许下划线/逗号分隔)。
    中转站常用 K/M 表述窗口大小,直接写裸数字反而少见,因此这里专门兼容后缀。
    """
    if raw is None:
        return None
    s = str(raw).strip().replace("_", "").replace(",", "").lower()
    if not s:
        return None
    multiplier = 1
    if s.endswith("k"):
        multiplier, s = 1024, s[:-1]
    elif s.endswith("m"):
        multiplier, s = 1024 * 1024, s[:-1]
    try:
        value = float(s) * multiplier
    except (TypeError, ValueError):
        return None
    ivalue = int(value)
    return ivalue if ivalue > 0 else None


class ConstantLLM:
    # ---- 环境变量键名 ----
    # 换 API 服务商(尤其中转站)后,模型名常和 llm_dict 的键对不上(例如
    # "deepseek-ai/DeepSeek-V4-Flash" vs "deepseek-v4-flash"),导致 llm_dict
    # lookup 落空、能力全部退回默认值。为此允许用环境变量直接指定当前模型的能力,
    # 优先级高于 llm_dict:env 配了就用 env,没配才查 llm_dict,再没有才用默认。
    ENV_IS_TOOL = "IS_TOOL"
    ENV_IS_REASONING = "IS_REASONING"
    ENV_MAX_TOKENS = "MAX_TOKENS"
    ENV_IMAGE_ABILITY = "IMAGE_ABILITY"

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
    #
    # 注意:以上字段都可被同名环境变量(IS_TOOL/IMAGE_ABILITY/MAX_TOKENS/
    # IS_REASONING)覆盖,优先级高于本表。换服务商导致模型名对不上时,用 env
    # 兜底即可,不必往本表塞中转站的奇怪模型名。
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
    def _registry_field(cls, model: str | None, field: str):
        """从 llm_dict 读某个字段;model 为空或未登记返回 None。"""
        if not model:
            return None
        config = cls.llm_dict.get(model)
        if not isinstance(config, dict):
            return None
        return config.get(field)

    @classmethod
    def resolve_is_tool(cls, model: str | None, default: bool = True) -> bool:
        """是否支持 function calling。env(IS_TOOL) > llm_dict > default。"""
        env_val = _parse_bool_env(os.getenv(cls.ENV_IS_TOOL))
        if env_val is not None:
            return env_val
        reg = cls._registry_field(model, "is_tool")
        if isinstance(reg, bool):
            return reg
        return default

    @classmethod
    def resolve_is_reasoning(cls, model: str | None, default: bool = False) -> bool:
        """是否为 reasoning/thinking 模型。env(IS_REASONING) > llm_dict > default。"""
        env_val = _parse_bool_env(os.getenv(cls.ENV_IS_REASONING))
        if env_val is not None:
            return env_val
        reg = cls._registry_field(model, "is_reasoning")
        if isinstance(reg, bool):
            return reg
        return default

    @classmethod
    def resolve_image_ability(cls, model: str | None, default: bool = False) -> bool:
        """是否支持原生视觉输入。env(IMAGE_ABILITY) > llm_dict > default。"""
        env_val = _parse_bool_env(os.getenv(cls.ENV_IMAGE_ABILITY))
        if env_val is not None:
            return env_val
        reg = cls._registry_field(model, "image_ability")
        if isinstance(reg, bool):
            return reg
        return default

    @classmethod
    def model_max_tokens(cls, model: str | None, default: int | None = None) -> int:
        """读取模型真实上下文窗口长度。

        这个方法是所有“上下文窗口”相关逻辑的统一入口。之前很多地方写死 8000，
        会导致 TUI 显示、ContextBuilder 预算和自动 compact 阈值互相不一致。

        取值优先级:环境变量 MAX_TOKENS(支持 1024K / 1M 写法) > llm_dict[model]
        ["max_tokens"] > default/DEFAULT_MAX_TOKENS。换服务商后模型名对不上
        llm_dict 时,直接在 .env 配 MAX_TOKENS 即可,不必改本表。
        """
        fallback = int(default or cls.DEFAULT_MAX_TOKENS)
        env_val = _parse_token_count_env(os.getenv(cls.ENV_MAX_TOKENS))
        if env_val is not None:
            return max(env_val, 1)
        reg = cls._registry_field(model, "max_input_tokens") or cls._registry_field(model, "max_tokens")
        try:
            max_tokens = int(reg) if reg is not None else fallback
        except (TypeError, ValueError):
            return fallback
        return max(max_tokens, 1) if max_tokens > 0 else fallback

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
