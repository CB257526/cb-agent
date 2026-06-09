"""QQ 平台专用工具子模块。

这里刻意只导出轻量注册表，不在包初始化阶段导入执行层。
执行层会加载 QQ action bridge、文件交付等运行时依赖；权限检查和测试只需要
读取子功能元数据，避免无意间把完整 QQ adapter / AgentSession 依赖拉进来。
"""

from .registry import QQFunctionSpec, get_qq_function_spec, list_qq_function_specs

__all__ = [
    "QQFunctionSpec",
    "get_qq_function_spec",
    "list_qq_function_specs",
]
