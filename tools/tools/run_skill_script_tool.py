"""Skill 脚本执行工具

让 LLM 通过 function calling 执行 Skill 捆绑的 Python 脚本。
当 Skill 的指令中要求执行特定脚本时，LLM 调用此工具。
"""

from typing import Dict, Any, List

from tools.tool import Tool, ToolParameter
from skills.skill_manager import SkillManager
from skills.skill_executor import SkillExecutor


class RunSkillScriptTool(Tool):
    """Skill 脚本执行工具

    LLM 通过 function calling 调用此工具来执行 Skill 捆绑的 Python 脚本。
    内部通过 SkillManager 查找脚本路径，通过 SkillExecutor 执行。
    """

    def __init__(self, skill_manager: SkillManager, executor: SkillExecutor = None):
        super().__init__(
            name="run_skill_script",
            description=(
                "执行 Skill 捆绑的 Python 脚本。"
                "当 Skill 的指令中要求运行特定脚本时调用此工具。"
                "需要提供 Skill 名称和脚本名称（不含 .py 扩展名）。"
            )
        )
        self.skill_manager = skill_manager
        self.executor = executor or SkillExecutor()

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """验证工具参数"""
        skill_name = parameters.get("skill_name")
        script_name = parameters.get("script_name")
        if not skill_name or not isinstance(skill_name, str):
            return False
        if not script_name or not isinstance(script_name, str):
            return False
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        """执行工具

        Args:
            parameters: 必须包含 skill_name 和 script_name，可选 args

        Returns:
            脚本执行的 stdout 输出
        """
        if not self.validate_parameters(parameters):
            return "[ERROR] 参数验证失败：需要 skill_name 和 script_name"

        skill_name = parameters["skill_name"].strip()
        script_name = parameters["script_name"].strip()
        args = parameters.get("args", [])
        stdin_data = parameters.get("stdin_data")

        # 查找 Skill
        skill = self.skill_manager.get_skill(skill_name)
        if not skill:
            available = [s.name for s in self.skill_manager.list_skills()]
            return f"[ERROR] 未找到名为 '{skill_name}' 的 Skill。可用的 Skill: {', '.join(available)}"

        # 查找脚本
        scripts = skill.get_scripts()
        script_path = scripts.get(script_name)
        if not script_path:
            available_scripts = list(scripts.keys())
            return f"[ERROR] Skill '{skill_name}' 中未找到脚本 '{script_name}'。可用脚本: {', '.join(available_scripts)}"

        # 执行脚本
        if stdin_data:
            return self.executor.run_script(
                script_path, args=args, stdin_data=stdin_data
            )
        else:
            return self.executor.run_script(script_path, args=args)

    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义"""
        return [
            ToolParameter(
                name="skill_name",
                type="string",
                description="Skill 名称（kebab-case），如 'pdf'",
                required=True
            ),
            ToolParameter(
                name="script_name",
                type="string",
                description="脚本名称（不含 .py 扩展名），如 'check_fillable_fields'",
                required=True
            ),
            ToolParameter(
                name="args",
                type="array",
                description="命令行参数列表，可选",
                required=False
            ),
            ToolParameter(
                name="stdin_data",
                type="string",
                description="通过 stdin 传入脚本的数据，可选",
                required=False
            ),
        ]
