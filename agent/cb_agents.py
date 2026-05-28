import os
from openai import OpenAI
from dotenv import load_dotenv
from typing import List, Dict, Optional, Any
import json
from constant.llm.constant_llm import ConstantLLM
# 加载 .env 文件中的环境变量
load_dotenv()

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

    def think(self, messages: List[Dict[str, str]], temperature: float = 0, tools: Optional[List[Dict]] = None) -> Any:
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

        return: {'answer': str, 'reason': str, 'tool_calls': List[Dict[str, Any]]}
        """
        print(f"🧠 正在调用 {self.model} 模型...")
        try:
            if self.is_Function_Calling:
                # 支持函数调用的模型调用
                return self._think_with_Function_Calling(messages, temperature, tools)
            else:
                return self._think_no_Function_Calling(messages, temperature)

        except Exception as e:
            print(f"❌ 调用LLM API时发生错误: {e}")
            return None
    
    #根据api厂商是否支持Function Calling进行不同的请求
    # 1 不支持Function Calling
    def _think_no_Function_Calling(self, messages: List[Dict[str, str]], temperature: float = 0) -> str:
        """不支持函数调用的模型调用 直接返回原始响应 让调用者自己解析"""
        response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                stream=True,
            )
            
        # 处理流式响应
        print("✅ 大语言模型响应成功:")
        collected_content = []
        for chunk in response:
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content or ""
            print(content, end="", flush=True)
            collected_content.append(content)
        print()  # 在流式输出结束后换行
        return ["".join(collected_content),None]  # 返回完整响应文本，tool_calls_info为None


    # 2 支持Function Calling
    def _think_with_Function_Calling(self, messages: List[Dict[str, str]], temperature: float = 0, tools: Optional[List[Dict]] = None) -> List[Any]:
        """支持函数调用的模型调用 自动处理函数调用逻辑
        """
        #TODO 实现支持函数调用的模型调用逻辑
        # 第一步：调用模型（非流式，因为需要解析 tool_calls）
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            tools=tools,
            tool_choice="auto",  # 模型自主决定是否调用工具
            #extra_body={"thinking": {"type": "disabled"}}
        )
        #print(f"原始响应: {response}")
        message = response.choices[0].message
        #message=ChatCompletionMessage(content='我是Claude，由Anthropic开发的大语言模型。是的，版本是Opus-4.7。有什么可以帮你的吗？', 
        # refusal=None, role='assistant', annotations=None, audio=None, 
        # function_call=None, tool_calls=None, 
        # reasoning_content='嗯，用户问了一个简单的自我介绍问题“你是谁”。这是一个非常基础的身份询问。
        # \n\n我需要直接、清晰地说明自己的身份和来源。我是由Anthropic开发的大语言模型，名字是Claude，版本是Opus-4.7。
        # \n\n想到了可以用简洁的句式回答，先确认用户提到的版本信息，再说明我的创建者。不需要展开其他功能或细节，避免信息冗余。')


        content = message.content if message.content else ""
        tool_calls = message.tool_calls if message.tool_calls else []
        tool_calls = [tool.model_dump() for tool in tool_calls] # 将每个tool对象转换为字典格式
        """
        "tool_calls": [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": "{\"city\": \"Beijing\"}"
                }
            }
        ]
        """
        # 提取思考字段如果有，没有的话就使用content代替
        if message.reasoning_content:
            reason = message.reasoning_content
        else:
            reason = content

        
        # 情况1：模型直接回复文本，没有调用工具
        return {'answer': content, 'reason': reason, 'tool_calls': tool_calls} # 直接回复文本，没有调用工具
    
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


