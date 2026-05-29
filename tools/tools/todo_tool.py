"""待办工具

为Agent提供任务管理能力，用于分解复杂任务、跟踪进度、在长对话中保持专注。
状态保存在内存中，每个Agent实例一份。
"""

import json
from typing import Dict, Any, List, Optional

from tools.tool import Tool, ToolParameter

# 有效的任务状态
VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


class TodoStore:
    """内存中的待办列表存储

    每个Agent实例对应一个TodoStore。
    任务按列表顺序排列，位置即优先级。
    每个任务包含: id, content, status
    """

    def __init__(self):
        self._items: List[Dict[str, str]] = []

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        """写入待办任务，返回写入后的完整列表

        Args:
            todos: 任务列表，每项为 {id, content, status}
            merge: False=替换整个列表, True=按id更新已有项并追加新项
        """
        if not merge:
            self._items = [self._validate(t) for t in self._dedupe_by_id(todos)]
        else:
            existing = {item["id"]: item for item in self._items}
            for t in self._dedupe_by_id(todos):
                item_id = str(t.get("id", "")).strip()
                if not item_id:
                    continue
                if item_id in existing:
                    if "content" in t and t["content"]:
                        existing[item_id]["content"] = str(t["content"]).strip()
                    if "status" in t and t["status"]:
                        status = str(t["status"]).strip().lower()
                        if status in VALID_STATUSES:
                            existing[item_id]["status"] = status
                else:
                    validated = self._validate(t)
                    existing[validated["id"]] = validated
                    self._items.append(validated)
            # 重建列表，保持原有顺序
            seen = set()
            rebuilt = []
            for item in self._items:
                current = existing.get(item["id"], item)
                if current["id"] not in seen:
                    rebuilt.append(current)
                    seen.add(current["id"])
            self._items = rebuilt
        return self.read()

    def read(self) -> List[Dict[str, str]]:
        """返回当前列表的副本"""
        return [item.copy() for item in self._items]

    def has_items(self) -> bool:
        """检查是否有任务"""
        return bool(self._items)

    def format_for_injection(self) -> Optional[str]:
        """渲染待办列表，用于上下文压缩后注入

        只注入 pending/in_progress 的任务，避免压缩后重复已完成的工作。
        """
        if not self._items:
            return None

        markers = {
            "completed": "[x]",
            "in_progress": "[>]",
            "pending": "[ ]",
            "cancelled": "[~]",
        }

        active_items = [
            item for item in self._items
            if item["status"] in ("pending", "in_progress")
        ]
        if not active_items:
            return None

        lines = ["[你的活跃任务列表已在上下文压缩中保留]"]
        for item in active_items:
            marker = markers.get(item["status"], "[?]")
            lines.append(f"- {marker} {item['id']}. {item['content']} ({item['status']})")

        return "\n".join(lines)

    @staticmethod
    def _validate(item: Dict[str, Any]) -> Dict[str, str]:
        """验证并规范化一个待办任务"""
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            item_id = "?"

        content = str(item.get("content", "")).strip()
        if not content:
            content = "(无描述)"

        status = str(item.get("status", "pending")).strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"

        return {"id": item_id, "content": content, "status": status}

    @staticmethod
    def _dedupe_by_id(todos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按id去重，保留最后一次出现的位置"""
        last_index: Dict[str, int] = {}
        for i, item in enumerate(todos):
            item_id = str(item.get("id", "")).strip() or "?"
            last_index[item_id] = i
        return [todos[i] for i in sorted(last_index.values())]


class TodoTool(Tool):
    """待办工具

    为Agent提供任务管理功能：
    - 创建/更新任务列表
    - 读取当前任务
    - 按id合并更新
    """

    def __init__(self):
        super().__init__(
            name="todo",
            description=(
                "任务管理工具 - 用于分解复杂任务、跟踪执行进度。"
                "适用于3步以上的复杂任务或用户一次给出多个任务的场景。"
            )
        )
        self.store = TodoStore()

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """验证工具参数"""
        # 无参数时为读取操作，合法
        if not parameters:
            return True

        # 验证 todos 参数
        todos = parameters.get("todos")
        if todos is not None:
            if not isinstance(todos, list):
                return False
            for item in todos:
                if not isinstance(item, dict):
                    return False
                # id 和 content 至少有一个
                if not item.get("id") and not item.get("content"):
                    return False
                # 如果提供了 status，必须合法
                status = item.get("status")
                if status is not None and str(status).strip().lower() not in VALID_STATUSES:
                    return False

        # 验证 merge 参数
        merge = parameters.get("merge")
        if merge is not None and not isinstance(merge, bool):
            return False

        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        """执行工具

        无参数或todos为空 -> 读取当前列表
        有todos参数 -> 写入任务列表
        """
        if not self.validate_parameters(parameters):
            return "❌ 参数验证失败：参数格式不正确"

        todos = parameters.get("todos")
        merge = parameters.get("merge", False)

        if todos is not None:
            items = self.store.write(todos, merge)
        else:
            items = self.store.read()

        # 统计各状态数量
        pending = sum(1 for i in items if i["status"] == "pending")
        in_progress = sum(1 for i in items if i["status"] == "in_progress")
        completed = sum(1 for i in items if i["status"] == "completed")
        cancelled = sum(1 for i in items if i["status"] == "cancelled")

        return json.dumps({
            "todos": items,
            "summary": {
                "total": len(items),
                "pending": pending,
                "in_progress": in_progress,
                "completed": completed,
                "cancelled": cancelled,
            },
        }, ensure_ascii=False)

    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义"""
        return [
            ToolParameter(
                name="todos",
                type="array",
                description=(
                    "待办任务列表。每项是对象，包含 id(唯一标识), content(任务描述), "
                    "status(pending/in_progress/completed/cancelled)。"
                    "省略则读取当前列表。"
                ),
                required=False,
                # 显式声明 items 为对象，避免被 to_openai_schema 兜底成字符串数组
                items={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "任务唯一标识，如 '1'、'task-a'",
                        },
                        "content": {
                            "type": "string",
                            "description": "任务描述",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(VALID_STATUSES),
                            "description": "任务状态，默认 pending",
                        },
                    },
                    "required": ["id", "content"],
                },
            ),
            ToolParameter(
                name="merge",
                type="boolean",
                description="true: 按id更新已有任务并追加新任务; false(默认): 替换整个列表",
                required=False,
                default=False
            ),
        ]

    def get_context_for_injection(self) -> Optional[str]:
        """获取用于上下文注入的待办列表文本

        可被Agent在上下文压缩后调用，将活跃任务重新注入对话。
        """
        return self.store.format_for_injection()
