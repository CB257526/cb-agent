from abc import ABC, abstractmethod
from typing import Dict, Any, List
from .toolParameter import ToolParameter
from agent.tool_execution import ToolCancellationMode, ToolExecutionContext
class Tool(ABC):
    """工具基类"""

    cancellation_mode = ToolCancellationMode.BLOCKING

    def __init__(
        self,
        name: str,
        description: str,
        *,
        default_timeout_seconds: Any = ...,
    ):
        self.name = name
        self.description = description
        self.default_timeout_seconds = default_timeout_seconds

    @abstractmethod
    def run(self, parameters: Dict[str, Any]) -> str:
        """执行工具"""
        pass

    def run_with_context(
        self,
        parameters: Dict[str, Any],
        context: ToolExecutionContext,
    ) -> str:
        """新执行入口；旧工具默认复用同步实现。

        旧工具属于不可安全终止的进程内代码，因此只在开始前检查取消。需要在
        运行中响应取消的工具必须覆盖本方法，并声明对应 cancellation_mode。
        """
        context.throw_if_cancelled()
        return self.run(parameters)

    @abstractmethod
    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义"""
        pass

    @abstractmethod
    def validate_parameters(self,parameters: Dict[str, Any]) -> Any:
        """验证工具参数"""
        pass

    def to_openai_schema(self) -> Dict[str, Any]:
        """转换为 OpenAI function calling schema 格式

        用于 FunctionCallAgent，使工具能够被 OpenAI 原生 function calling 使用

        Returns:
            符合 OpenAI function calling 标准的 schema
        """
        parameters = self.get_parameters()

        # 构建 properties
        properties = {}
        required = []

        for param in parameters: # 遍历每个参数
            # 基础属性定义
            prop = {
                "type": param.type,
                "description": param.description
            }

            # 如果有默认值，添加到描述中（OpenAI schema 不支持 default 字段）
            if param.default is not None:
                prop["description"] = f"{param.description} (默认: {param.default})"

            # 如果是数组类型，添加 items 定义
            if param.type == "array":
                # 优先使用 ToolParameter 自定义的 items schema；
                # 未指定则兜底为字符串数组，向后兼容旧工具。
                prop["items"] = param.items if param.items else {"type": "string"}

            properties[param.name] = prop

            # 收集必需参数
            if param.required:
                required.append(param.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required
                }
            }
        }
