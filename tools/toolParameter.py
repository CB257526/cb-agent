from pydantic import BaseModel
from typing import Any, List, Dict, Optional

class ToolParameter(BaseModel):
    """工具参数定义(函数中的其中一个参数)"""
    name: str  # 参数名称
    type: str  # 参数类型
    description: str  # 参数描述
    required: bool = True  # 是否必填
    default: Any = None  # 默认值
    # 当 type=="array" 时可指定 items 的 JSON Schema；为 None 时按字符串数组兜底。
    # 例：items={"type": "object", "properties": {...}, "required": [...]}
    items: Optional[Dict[str, Any]] = None


# 示例
"""
    @abstractmethod
    def get_parameters(self) -> List[ToolParameter]:
        获取工具参数定义
        pass
    这是我们定义一个工具时需要实现的返回工具参数定义的方法

    假如我们有一个搜索函数，它的参数是query，类型是string，描述是搜索查询内容，必填，没有默认值
    实现如下：
    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="query",  # 参数名称
                type="string",  # 参数类型
                description="搜索查询内容",  # 参数描述
                required=True,  # 是否必填
                default=None  # 默认值
            )
            ...
            ToolParameter(
                name="param2",  # 参数名称
                type="string",  # 参数类型
                description="参数2描述",  # 参数描述
                required=False,  # 是否必填
                default=None  # 默认值
            )
        ]
"""
