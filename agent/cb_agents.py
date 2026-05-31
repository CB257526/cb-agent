import os
import threading
from openai import OpenAI
from dotenv import load_dotenv
from typing import List, Dict, Optional, Any
import json
from constant.llm.constant_llm import ConstantLLM
from agent.event_bus import EventBus
from agent.events import (
    Cancelled, ReasoningDelta, TextDelta, TokenUsage, ToolCallPlanned,
)
# 加载 .env 文件中的环境变量
load_dotenv()


def _usage_to_dict(usage: Any) -> Optional[Dict[str, int]]:
    """OpenAI usage 对象 → dict。None 透传 None。"""
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "total_tokens": getattr(usage, "total_tokens", 0) or 0,
    }

class CbAgentsLLM:
    """
    通过OpenAI API调用大语言模型。
    它用于调用任何兼容OpenAI接口的服务，并默认使用流式响应。
    """
    def __init__(self, model: str = None, apiKey: str = None, baseUrl: str = None, timeout: int = None, provider: Optional[str] = "auto"):
        """
        初始化客户端。优先使用传入参数，如果未提供，则从环境变量加载。
        """
        self.model = model or os.getenv("LLM_MODEL_ID")
        apiKey = apiKey or os.getenv("LLM_API_KEY")
        baseUrl = baseUrl or os.getenv("LLM_BASE_URL")
        timeout = timeout or int(os.getenv("LLM_TIMEOUT", 60))
        self.provider = provider
        self.is_Function_Calling = self._is_able_Function_Calling()

        
        if not all([self.model, apiKey, baseUrl]):
            raise ValueError("模型ID、API密钥和服务地址必须被提供或在.env文件中定义。")

        self.client = OpenAI(api_key=apiKey, base_url=baseUrl, timeout=timeout)

    def _is_able_Function_Calling(self) -> bool:
        """根据模型提供商判断是否支持函数调用"""
        #实现根据模型提供商判断是否支持函数调用的逻辑
        return ConstantLLM.llm_dict[self.model]["is_tool"]

    def think(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0,
        tools: Optional[List[Dict]] = None,
        event_bus: Optional[EventBus] = None,
        cancel_event: Optional[threading.Event] = None,
        round_idx: int = 0,
    ) -> Any:
        """
        调用大语言模型进行思考，并返回其响应。
        tools: OpenAI Function Calling 的标准格式 JSON 字符串 比如：
        {
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "执行数学计算的工具...",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "需要计算的数学表达式..."
                        }
                    },
                    "required": ["expression"]
                }
            }
        }

        return: {'answer': str, 'reason': str, 'tool_calls': List[Dict[str, Any]],
                 'usage': Dict | None, 'cancelled': bool}

        event_bus: 可选事件总线。流式 chunk 会经它发出 TextDelta/ReasoningDelta/
                   TokenUsage/ToolCallPlanned/Cancelled 事件。前端订阅它替代 print。
                   传 None 时维持旧行为（直接 print 到 stdout）。
        cancel_event: 可选 threading.Event。每收一个 chunk 检查一次，set 则中止
                      流式读取并返回已累积内容（带 cancelled=True 标记）。
        round_idx: 工具循环当前轮次，1-based。仅作为事件元信息透传。
        """
        print(f"🧠 正在调用 {self.model} 模型...")
        try:
            if self.is_Function_Calling:
                # 支持函数调用的模型调用
                return self._think_with_Function_Calling(
                    messages, temperature, tools,
                    event_bus=event_bus, cancel_event=cancel_event, round_idx=round_idx,
                )
            else:
                return self._think_no_Function_Calling(
                    messages, temperature,
                    event_bus=event_bus, cancel_event=cancel_event, round_idx=round_idx,
                )

        except Exception as e:
            print(f"❌ 调用LLM API时发生错误: {e}")
            return None
    
    #根据api厂商是否支持Function Calling进行不同的请求
    # 1 不支持Function Calling
    def _think_no_Function_Calling(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0,
        event_bus: Optional[EventBus] = None,
        cancel_event: Optional[threading.Event] = None,
        round_idx: int = 0,
    ) -> List[Any]:
        """不支持函数调用的模型调用 直接返回原始响应 让调用者自己解析"""
        response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                stream=True,
                stream_options={"include_usage": True},
            )

        # 处理流式响应
        print("✅ 大语言模型响应成功:")
        collected_content: List[str] = []
        accumulated = ""
        last_usage = None

        for chunk in response:
            # cancel 检查放最前面，确保下一个 chunk 边界一定能出
            if cancel_event is not None and cancel_event.is_set():
                if event_bus is not None:
                    event_bus.emit(Cancelled(where="llm_stream", round_idx=round_idx))
                break

            # usage 通常在最后一个 chunk（choices 为空）出现
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                last_usage = usage

            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content or ""
            if content:
                # 默认（无 bus）维持旧行为打印到 stdout；有 bus 时不直接 print，
                # 让订阅者自己决定渲染方式
                if event_bus is None:
                    print(content, end="", flush=True)
                accumulated += content
                collected_content.append(content)
                if event_bus is not None:
                    event_bus.emit(TextDelta(
                        delta=content,
                        accumulated=accumulated,
                        round_idx=round_idx,
                    ))
        if event_bus is None:
            print()  # 在流式输出结束后换行（旧行为）

        # 推 token usage 事件
        if last_usage is not None and event_bus is not None:
            event_bus.emit(TokenUsage(
                prompt_tokens=getattr(last_usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(last_usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(last_usage, "total_tokens", 0) or 0,
                round_idx=round_idx,
            ))

        full_text = "".join(collected_content)
        # 兼容旧返回结构：[text, None]；额外字段挂在 list 末尾会破坏调用方解构
        # → 改成多带回 usage/cancelled 信息但保持位置 0/1 不变
        return [full_text, None]


    # 2 支持Function Calling
    def _think_with_Function_Calling(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0,
        tools: Optional[List[Dict]] = None,
        event_bus: Optional[EventBus] = None,
        cancel_event: Optional[threading.Event] = None,
        round_idx: int = 0,
    ) -> Dict[str, Any]:
        """支持函数调用的模型调用（流式）。

        OpenAI 协议在 stream=True 下，tool_calls 会按 index 分块下发：
            delta.tool_calls = [
              {"index": 0, "id": "...", "type": "function",
               "function": {"name": "...", "arguments": "..."}},
              ...
            ]
        每个分片可能只带 name 的一部分或 arguments json 的一段，必须按 index 累积。
        content / reasoning_content 同样按 delta 增量拼接。

        event_bus / cancel_event / round_idx 见 think() 文档。
        """
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            tools=tools,
            tool_choice="auto",
            stream=True,
            stream_options={"include_usage": True},
        )

        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        content_accumulated = ""
        reasoning_accumulated = ""
        # 按 index 累积 tool_calls 分片
        tool_calls_by_index: Dict[int, Dict[str, Any]] = {}
        last_usage = None

        # 控制可见正文的打印：只有真正开始吐 content 时才打 "assistant > " 前缀
        printed_prefix = False

        for chunk in response:
            # cancel 检查放最前面：保证下一 chunk 边界能优雅退出
            if cancel_event is not None and cancel_event.is_set():
                if event_bus is not None:
                    event_bus.emit(Cancelled(where="llm_stream", round_idx=round_idx))
                break

            # usage 通常在 stream 末尾的"choices 为空"chunk 上
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                last_usage = usage

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # 1) 普通 content：直接流式打到终端（无 bus 时）或经 bus 派发
            piece = getattr(delta, "content", None) or ""
            if piece:
                if event_bus is None:
                    if not printed_prefix:
                        print("\nassistant > ", end="", flush=True)
                        printed_prefix = True
                    print(piece, end="", flush=True)
                content_accumulated += piece
                content_parts.append(piece)
                if event_bus is not None:
                    event_bus.emit(TextDelta(
                        delta=piece,
                        accumulated=content_accumulated,
                        round_idx=round_idx,
                    ))

            # 2) reasoning_content（DeepSeek thinking 等）：旧行为只累计不直接打，
            #    交给上层 run_agent 渲染成 "Thought for Xs" 块
            r_piece = getattr(delta, "reasoning_content", None) or ""
            if r_piece:
                reasoning_parts.append(r_piece)
                reasoning_accumulated += r_piece
                if event_bus is not None:
                    event_bus.emit(ReasoningDelta(
                        delta=r_piece,
                        accumulated=reasoning_accumulated,
                        round_idx=round_idx,
                    ))

            # 3) tool_calls 分片：按 index 累积
            tc_chunks = getattr(delta, "tool_calls", None) or []
            for tc in tc_chunks:
                idx = tc.index if tc.index is not None else 0
                slot = tool_calls_by_index.setdefault(
                    idx,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tc.id:
                    slot["id"] = tc.id
                if tc.type:
                    slot["type"] = tc.type
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["function"]["name"] += fn.name
                    if getattr(fn, "arguments", None):
                        slot["function"]["arguments"] += fn.arguments

        if event_bus is None and printed_prefix:
            print()  # 流式正文末尾补换行（旧行为）

        content = "".join(content_parts)
        reasoning_content = "".join(reasoning_parts) or None
        # 按 index 排序，输出形如 [{id, type, function:{name, arguments}}, ...]
        tool_calls = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]

        # tool_calls 累积完成 → 每个发 ToolCallPlanned 事件（执行还没开始）
        if event_bus is not None:
            for tc in tool_calls:
                event_bus.emit(ToolCallPlanned(
                    call_id=tc.get("id", ""),
                    name=tc["function"]["name"],
                    arguments_json=tc["function"]["arguments"],
                    round_idx=round_idx,
                ))

        # token usage 事件
        if last_usage is not None and event_bus is not None:
            event_bus.emit(TokenUsage(
                prompt_tokens=getattr(last_usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(last_usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(last_usage, "total_tokens", 0) or 0,
                round_idx=round_idx,
            ))

        return {
            "answer": content,
            "reason": reasoning_content if reasoning_content else content,
            "tool_calls": tool_calls,
            "reasoning_content": reasoning_content,
            "usage": _usage_to_dict(last_usage),
        }
    
    def _auto_detect_provider(self, api_key: Optional[str], base_url: Optional[str]) -> str:
        """
        自动检测LLM提供商
        """
        # 1. 检查特定提供商的环境变量 (最高优先级)
        if os.getenv("MODELSCOPE_API_KEY"): return "modelscope"
        if os.getenv("OPENAI_API_KEY"): return "openai"
        if os.getenv("ZHIPU_API_KEY"): return "zhipu"
        # ... 其他服务商的环境变量检查

        # 获取通用的环境变量
        actual_api_key = api_key or os.getenv("LLM_API_KEY")
        actual_base_url = base_url or os.getenv("LLM_BASE_URL")

        # 2. 根据 base_url 判断
        if actual_base_url:
            base_url_lower = actual_base_url.lower()
            if "api-inference.modelscope.cn" in base_url_lower: return "modelscope"
            if "open.bigmodel.cn" in base_url_lower: return "zhipu"
            if "localhost" in base_url_lower or "127.0.0.1" in base_url_lower:
                if ":11434" in base_url_lower: return "ollama"
                if ":8000" in base_url_lower: return "vllm"
                return "local" # 其他本地端口

        # 3. 根据 API 密钥格式辅助判断
        if actual_api_key:
            if actual_api_key.startswith("ms-"): return "modelscope"
            # ... 其他密钥格式判断

        # 4. 默认返回 'auto'，使用通用配置
        return "auto"
    
    def _resolve_credentials(self, api_key: Optional[str], base_url: Optional[str]) -> tuple[str, str]:
        """根据provider解析API密钥和base_url"""
        if self.provider == "openai":
            resolved_api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
            resolved_base_url = base_url or os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1"
            return resolved_api_key, resolved_base_url

        elif self.provider == "modelscope":
            resolved_api_key = api_key or os.getenv("MODELSCOPE_API_KEY") or os.getenv("LLM_API_KEY")
            resolved_base_url = base_url or os.getenv("LLM_BASE_URL") or "https://api-inference.modelscope.cn/v1/"
            return resolved_api_key, resolved_base_url


# --- 客户端使用示例 ---
if __name__ == '__main__':
    try:
        llmClient = CbAgentsLLM()
        
        exampleMessages = [
            {"role": "system", "content": "你说claude-opus-4.7，由Anthropic开发的大语言模型。"},
            {"role": "user", "content": "你是谁"}
        ]
        
        print("--- 调用LLM ---")
        responseText = llmClient.think(exampleMessages)
        if responseText:
            print("\n\n--- 完整模型响应 ---")
            print(responseText)

    except ValueError as e:
        print(e)


