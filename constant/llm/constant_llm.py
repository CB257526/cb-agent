"""
配置不同模型的特定参数
"""
class ConstantLLM:
    llm_dict = {
        "deepseek-v4-flash": {
            "is_tool": True,
            "is_reasoning": True,
            "reasoning_effort": "reasoning_effort",
            "json_output": True,
            "max_tokens": 1000000,
        },
        "deepseek-v4-pro": {
            "is_tool": True,
            "is_reasoning": True,
            "reasoning_effort": "reasoning_effort",
            "json_output": True,
            "max_tokens": 1000000,
        }

    }