"""简易 LLM 调用器 — 框架内部使用，只负责发消息、返回文本，不涉及工具调用。"""
import os
import time
from openai import OpenAI
from dotenv import load_dotenv
from typing import List, Dict

load_dotenv()


class CbAgentsLLMBasic:
    """轻量 LLM 客户端，用于框架内部文本生成场景（总结记忆、分类内容等）。"""

    def __init__(
        self,
        model: str = None,
        api_key: str = None,
        base_url: str = None,
        timeout: int = None,
        max_retries: int = 2,
    ):
        self.model = model or os.getenv("LLM_MODEL_ID")
        api_key = api_key or os.getenv("LLM_API_KEY")
        base_url = base_url or os.getenv("LLM_BASE_URL")
        timeout = timeout or int(os.getenv("LLM_TIMEOUT", 60))

        if not all([self.model, api_key, base_url]):
            raise ValueError("LLM_MODEL_ID、LLM_API_KEY、LLM_BASE_URL 必须在 .env 中配置或显式传入")

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self.max_retries = max_retries

    # ---- 公开接口 ----

    def ask(
        self,
        prompt: str,
        system: str = None,
        temperature: float = 0.0,
        max_tokens: int = None,
    ) -> str:
        """快捷调用：只需传 user prompt，自动包装 messages。

        这是最常用的入口。调用方无需手动构造 messages 列表。

        Args:
            prompt: 用户输入（必需）
            system: 系统提示词（可选）
            temperature: 生成随机性，默认 0
            max_tokens: 输出长度上限

        Returns:
            模型回复文本，失败时返回空字符串
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, temperature, max_tokens)

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = None,
    ) -> str:
        """标准 messages 调用，带自动重试。

        Args:
            messages: [{"role": "...", "content": "..."}]
            temperature: 生成随机性
            max_tokens: 输出长度上限

        Returns:
            模型回复文本，失败时返回空字符串
        """
        for attempt in range(self.max_retries + 1):
            try:
                kwargs = dict(model=self.model, messages=messages, temperature=temperature)
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens
                response = self.client.chat.completions.create(**kwargs)
                return response.choices[0].message.content or ""
            except Exception:
                if attempt < self.max_retries:
                    wait = 2 ** attempt  # 指数退避: 1s, 2s
                    time.sleep(wait)
                    continue
                # 重试耗尽，不再向上抛，返回空让调用方自行处理
                return ""


# --- 使用示例 ---
if __name__ == "__main__":
    try:
        llm = CbAgentsLLMBasic()
        result = llm.ask("什么是嵌入模型？", system="用一句话回答。")
        print(result)
    except ValueError as e:
        print(e)
