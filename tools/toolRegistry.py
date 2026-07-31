"""工具注册表 - HelloAgents原生工具系统"""

import copy
import inspect
import logging
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional

from agent.tool_execution import ToolCancellationMode, ToolExecutionContext
from .tool import Tool


logger = logging.getLogger(__name__)

class ToolRegistry:
    """
    工具注册表

    提供工具的注册、管理和执行功能。
    支持两种工具注册方式：
    1. Tool对象注册（推荐）
    2. 函数直接注册（简便）
    """


    def __init__(self):
        # MCP 工具改为后台连接后，注册动作可能和主对话线程读取 tool schema
        # 同时发生。这里用 RLock 保护字典读写，并在读取列表时先做快照，避免
        # “遍历过程中字典变化”的竞态。
        self._lock = threading.RLock()
        self._tools: dict[str, Tool] = {}
        self._functions: dict[str, dict[str, Any]] = {}

    def register_tool(self, tool: Tool):
        """
        注册Tool对象

        Args:
            tool: Tool实例
        """
        with self._lock:
            if tool.name in self._tools:
                print(f"警告：工具 '{tool.name}' 已存在，将被覆盖。")
            self._tools[tool.name] = tool
        print(f"工具 '{tool.name}' 已注册。")

    def register_function(self, name: str, description: str, func: Callable[[str], str]):
        """
        直接注册函数作为工具（简便方式）

        Args:
            name: 工具名称
            description: 工具描述
            func: 工具函数，接受字符串参数，返回字符串结果
        """
        with self._lock:
            if name in self._functions:
                print(f"警告：工具 '{name}' 已存在，将被覆盖。")
            self._functions[name] = {
                "description": description,
                "func": func
            }
        print(f"工具 '{name}' 已注册。")

    def unregister(self, name: str):
        """注销工具"""
        with self._lock:
            if name in self._tools:
                del self._tools[name]
                print(f"工具 '{name}' 已注销。")
            elif name in self._functions:
                del self._functions[name]
                print(f"工具 '{name}' 已注销。")
            else:
                print(f"警告：工具 '{name}' 不存在。")

    def get_tool(self, name: str) -> Optional[Tool]:
        """获取Tool对象"""
        with self._lock:
            return self._tools.get(name)

    def get_function(self, name: str) -> Optional[Callable]:
        """获取工具函数"""
        with self._lock:
            func_info = self._functions.get(name)
        return func_info["func"] if func_info else None

    def execute_tool(
        self,
        name: str,
        input_dict: dict[str, Any],
        context: Optional[ToolExecutionContext] = None,
    ) -> Any:
        """
        执行工具

        Args:
            name: 工具名称
            input_dict: 输入参数

        Returns:
            工具执行结果
        """
        with self._lock:
            tool = self._tools.get(name)
            func_info = self._functions.get(name)

        if tool is not None:
            # 新主路径始终使用带上下文的入口。异常不能在注册表中转成普通文本，
            # 否则执行器无法区分失败、用户取消和超时。
            if context is not None:
                return tool.run_with_context(input_dict, context)
            return tool.run(input_dict)

        elif func_info is not None:
            func = func_info["func"]
            if context is not None:
                # 函数工具只有明确声明第二个参数时才传入上下文。通过签名判断可以
                # 避免把工具内部真正抛出的 TypeError 误判为旧接口。
                try:
                    signature = inspect.signature(func)
                    parameters = signature.parameters.values()
                    accepts_context = any(
                        item.kind in (item.VAR_POSITIONAL, item.VAR_KEYWORD)
                        for item in parameters
                    ) or len(signature.parameters) >= 2
                except (TypeError, ValueError):
                    # 部分 C 扩展可调用对象没有 Python 签名，只能沿用旧接口。
                    accepts_context = False
                if accepts_context:
                    return func(input_dict, context)
            return func(input_dict)

        else:
            raise KeyError(f"未找到名为 '{name}' 的工具")

    def get_execution_profile(self, name: str) -> tuple[ToolCancellationMode, Any]:
        """返回工具的取消模式和实例级默认超时。"""

        with self._lock:
            tool = self._tools.get(name)
        if tool is None:
            return ToolCancellationMode.BLOCKING, ...
        return (
            getattr(tool, "cancellation_mode", ToolCancellationMode.BLOCKING),
            getattr(tool, "default_timeout_seconds", ...),
        )

    def get_tools_description(self) -> str:
        """
        获取所有可用工具的格式化描述字符串。

        工具按名称字母序排序输出。这是为了:
        - 确定性: 相同工具集合 → 相同排列顺序 → system message 字节不变
        - 缓存友好: system message 是 messages[0],其字节序列必须稳定
          provider 端才能命中 prompt cache。如果每次随机排列,缓存前缀就变了。

        Returns:
            工具描述字符串,用于构建提示词
        """
        descriptions = []

        with self._lock:
            tools = sorted(self._tools.values(), key=lambda t: t.name)
            functions = sorted(self._functions.items(), key=lambda item: item[0])

        # Tool对象描述
        for tool in tools:
            descriptions.append(f"- {tool.name}: {tool.description}")

        # 函数工具描述
        for name, info in functions:
            descriptions.append(f"- {name}: {info['description']}")

        return "\n".join(descriptions) if descriptions else "暂无可用工具"
    

    def get_tools_description_openai_schema(self) -> Optional[List[Dict]]:
        """
        获取所有可用工具的 OpenAI function calling 格式列表。

        工具按名称字母序排序,输出前再次按 function.name 排序:
        - 内部 sorted() 保证 Tool 对象和函数工具各自的确定性顺序
        - 外层 descriptions.sort() 保证合并后的最终列表也是确定性的
        - 两层排序缺一不可: Tool.name 和 function.name 的字母序可能不一致

        Returns:
            工具列表,用于模型调用 Function Calling
        """
        descriptions = []

        with self._lock:
            tools = sorted(self._tools.values(), key=lambda t: t.name)
            functions = sorted(self._functions.items(), key=lambda item: item[0])

        # Tool对象描述
        for tool in tools:
            descriptions.append(tool.to_openai_schema())

        # 函数工具描述
        # TODO: 处理函数参数的类型和必填性
        for name, info in functions:
            descriptions.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": info["description"],
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    },
                }
            })
        descriptions.sort(
            key=lambda item: str(((item.get("function") or {}).get("name")) or "")
        )
        return descriptions if descriptions else []

    def list_tools(self) -> list[str]:
        """列出所有工具名称,按字母序排列(保证确定性,支持 prompt cache)。"""
        with self._lock:
            return sorted(list(self._tools.keys()) + list(self._functions.keys()))

    def get_all_tools(self) -> list[Tool]:
        """获取所有 Tool 对象,按名称字母序排列(保证确定性)。"""
        with self._lock:
            return sorted(self._tools.values(), key=lambda t: t.name)

    def clone_filtered(
        self,
        *,
        allow_names: Optional[Iterable[str]] = None,
        deny_names: Optional[Iterable[str]] = None,
        event_bus: Any = None,
        bash_session: Any = None,
        bash_output_dir: Any = None,
    ) -> "ToolRegistry":
        """创建一个只包含指定工具的快照注册表。

        按 allow/deny 两组规则筛选工具，同时处理特殊工具（todo 需要 event_bus、
        list_tools 需要引用自身）的依赖注入。普通 Tool 优先调用专用克隆接口，
        其次深拷贝或重新构造；无法安全克隆的工具会跳过，不把有状态实例直接共享
        给多个子代理线程。
        """
        allow = set(allow_names) if allow_names is not None else None
        deny = set(deny_names or [])
        cloned = ToolRegistry()

        with self._lock:
            tools = sorted(self._tools.values(), key=lambda t: t.name)
            functions = sorted(self._functions.items(), key=lambda item: item[0])

        selected_tool_names = []
        for tool in tools:
            if allow is not None and tool.name not in allow:
                continue
            if tool.name in deny:
                continue
            selected_tool_names.append(tool.name)
            if tool.name == "list_tools":
                continue
            if tool.name == "todo":
                try:
                    from tools.tools.todo_tool import TodoTool
                    cloned.register_tool(TodoTool(event_bus=event_bus))
                    continue
                except Exception:
                    pass
            if tool.name == "bash":
                try:
                    from tools.tools.bash_tool import BashTool

                    cloned.register_tool(BashTool(
                        session=bash_session,
                        permission=getattr(tool, "_permission", None),
                        is_subagent=True,
                        skill_observer=getattr(tool, "_skill_observer", None),
                        dangerously_skip_permissions_provider=getattr(
                            tool,
                            "_dangerously_skip_permissions_provider",
                            None,
                        ),
                        dangerously_skip_permissions=bool(getattr(
                            tool,
                            "_dangerously_skip_permissions",
                            False,
                        )),
                        output_dir=bash_output_dir,
                    ))
                    continue
                except Exception:
                    pass
            cloned_tool = None
            clone_for_subagent = getattr(tool, "clone_for_subagent", None)
            if callable(clone_for_subagent):
                try:
                    cloned_tool = clone_for_subagent(event_bus=event_bus)
                except TypeError:
                    try:
                        cloned_tool = clone_for_subagent()
                    except Exception:
                        logger.exception("工具专用子代理克隆失败: %s", tool.name)
                except Exception:
                    logger.exception("工具专用子代理克隆失败: %s", tool.name)
            if cloned_tool is None:
                try:
                    # 深拷贝优先保留自定义工具的实例化名称、描述和配置，同时隔离
                    # 可变状态；含锁或网络句柄的工具若无法深拷贝，再尝试重新构造。
                    cloned_tool = copy.deepcopy(tool)
                except Exception:
                    try:
                        cloned_tool = type(tool)()
                    except Exception:
                        logger.warning("跳过无法安全克隆的子代理工具: %s", tool.name)
                        continue
            # 重新构造可能回到类的默认名称；子注册表必须保持父注册表的公开契约，
            # 否则角色 allowlist 中的名字会与实际 schema/执行入口不一致。
            cloned_tool.name = tool.name
            cloned_tool.description = tool.description
            cloned.register_tool(cloned_tool)

        if "list_tools" in selected_tool_names:
            try:
                from tools.tools.list_tools_tool import ListToolsTool
                cloned.register_tool(ListToolsTool(cloned))
            except Exception:
                pass

        for name, info in functions:
            if allow is not None and name not in allow:
                continue
            if name in deny:
                continue
            # 裸函数没有统一的状态克隆协议。只有显式声明线程安全的函数才允许进入
            # 子代理，防止闭包捕获主会话状态后被多个线程并发修改。
            if not bool(getattr(info["func"], "subagent_thread_safe", False)):
                logger.warning("跳过未声明线程安全的子代理函数工具: %s", name)
                continue
            cloned.register_function(name, info["description"], info["func"])
        return cloned

    def clear(self):
        """清空所有工具"""
        with self._lock:
            self._tools.clear()
            self._functions.clear()
        print("所有工具已清空。")

# 全局工具注册表
global_registry = ToolRegistry()
