"""Skill 工具

让 LLM 通过 function calling 调用 Skill。
LLM 在系统提示词中看到可用的 Skill 列表，判断用户请求匹配哪个 Skill 后调用此工具。
"""

from typing import Dict, Any, List

from tools.tool import Tool, ToolParameter
from skills.skill_manager import SkillManager


class SkillTool(Tool):
    """Skill 工具

    LLM 通过 function calling 调用此工具来加载并执行 Skill。
    内部通过 SkillManager 查找 Skill，渲染内容后返回给 LLM。
    """

    def __init__(self, skill_manager: SkillManager):
        super().__init__(
            name="skill",
            description=(
                "调用 Skill 执行特定领域任务。"
                "当用户请求匹配某个 Skill 的使用场景时调用。"
                "也可由用户通过 /skill-name 直接触发。"
                "传入 skill 名称获取该 Skill 的完整指令和参考文档。"
            )
        )
        self.skill_manager = skill_manager

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """验证工具参数"""
        skill = parameters.get("skill")
        if not skill or not isinstance(skill, str):
            return False
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        """执行工具

        Args:
            parameters: 必须包含 skill(名称)，可选 args(参数)、document(文档名)

        Returns:
            Skill 正文或指定的参考文档内容
        """
        if not self.validate_parameters(parameters):
            return "[ERROR] 参数验证失败：缺少 skill 名称"

        skill_name = parameters["skill"].strip()
        args = parameters.get("args", "")
        document = parameters.get("document", "").strip()

        # 查找 Skill
        skill = self.skill_manager.get_skill(skill_name)
        if not skill:
            available = [s.name for s in self.skill_manager.list_skills()]
            return f"[ERROR] 未找到名为 '{skill_name}' 的 Skill。可用的 Skill: {', '.join(available)}"

        # 检查是否禁止模型调用
        if skill.disable_model_invocation:
            return f"[ERROR] Skill '{skill_name}' 已禁用模型自动调用，请用户通过 /{skill_name} 手动触发"

        # 记录使用
        self.skill_manager.record_usage(skill_name)

        # 如果指定了 document，加载对应的参考文档
        if document:
            return self.skill_manager.load_skill_reference(skill_name, document)

        # 否则加载 SKILL.md 正文
        return self.skill_manager.load_skill_content(skill_name, args)

    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义"""
        return [
            ToolParameter(
                name="skill",
                type="string",
                description="Skill 名称（kebab-case），如 'pdf'、'skill-creator'",
                required=True
            ),
            ToolParameter(
                name="args",
                type="string",
                description="传给 Skill 的参数字符串，可选",
                required=False,
                default=""
            ),
            ToolParameter(
                name="document",
                type="string",
                description="要加载的参考文档名称（不含 .md 扩展名），如 'forms'、'reference'。省略则加载 SKILL.md 正文",
                required=False,
                default=""
            ),
        ]
