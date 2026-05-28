"""Skill 数据类

表示一个从 SKILL.md 解析出来的 Skill，包含元数据、正文和资源访问方法。
"""

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Skill:
    """Skill 数据类

    对应一个 SKILL.md 文件的完整解析结果。
    """
    name: str                          # kebab-case 标识符
    description: str                   # 一行描述
    body: str                          # SKILL.md 正文（支持变量替换）
    skill_dir: Path                    # skill 目录绝对路径
    when_to_use: str = ""              # 详细触发条件
    allowed_tools: Optional[list] = None   # 工具权限白名单
    arguments: Optional[list] = None       # 声明的参数名
    argument_hint: Optional[str] = None    # 参数提示
    model: Optional[str] = None            # 模型覆盖
    user_invocable: bool = True            # 用户可否通过 /name 调用
    disable_model_invocation: bool = False # 禁止 AI 调用
    license: Optional[str] = None
    metadata: Optional[dict] = None
    compatibility: Optional[str] = None
    paths: Optional[list] = None           # glob 模式，条件激活
    aliases: Optional[list] = None         # 别名
    version: Optional[str] = None          # 版本号

    def to_metadata_string(self, detail: str = "full") -> str:
        """L1 表示：用于系统提示词中的 Skill 列表

        detail 级别:
        - "full": name + description + when_to_use
        - "compact": name + 截断的 description
        - "name_only": 仅名称
        """
        if detail == "name_only":
            return f"- {self.name}"

        if detail == "compact":
            desc = self.description[:80] + "..." if len(self.description) > 80 else self.description
            return f"- {self.name}: {desc}"

        # full
        parts = [f"- {self.name}: {self.description}"]
        if self.when_to_use:
            parts.append(f" — {self.when_to_use}")
        return "".join(parts)

    def matches_paths(self, file_paths: list) -> bool:
        """检查给定文件路径是否匹配此 Skill 的激活模式

        如果 paths 为 None（未声明），Skill 始终激活（向后兼容）。
        如果 paths 已声明，至少一个 file_path 需匹配至少一个 pattern。
        """
        if self.paths is None:
            return True
        for pattern in self.paths:
            for fpath in file_paths:
                if fnmatch.fnmatch(fpath, pattern) or fnmatch.fnmatch(Path(fpath).name, pattern):
                    return True
        return False

    def render(self, args: str = "") -> str:
        """渲染最终提示词，执行变量替换

        Args:
            args: 用户传入的参数字符串

        Returns:
            替换后的正文内容
        """
        result = self.body

        # 替换 ${SKILL_DIR}
        result = result.replace("${SKILL_DIR}", str(self.skill_dir))

        # 替换 $ARGUMENTS
        result = result.replace("$ARGUMENTS", args)

        # 替换 $arg_name（按 arguments 声明匹配）
        if self.arguments and args:
            parsed = self._parse_args(args)
            for arg_name in self.arguments:
                placeholder = f"${arg_name}"
                if placeholder in result:
                    value = parsed.get(arg_name, "")
                    result = result.replace(placeholder, value)

        return result

    def get_references(self) -> dict:
        """扫描 skill_dir 下除 SKILL.md 外的 *.md 文件

        Returns:
            {文件名(不含扩展名): 文件内容} 的字典
        """
        refs = {}
        if not self.skill_dir.is_dir():
            return refs

        for md_file in self.skill_dir.glob("*.md"):
            if md_file.name.upper() == "SKILL.md":
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                key = md_file.stem  # 文件名不含扩展名
                refs[key] = content
            except (OSError, UnicodeDecodeError):
                continue
        return refs

    def get_scripts(self) -> dict:
        """扫描 scripts/ 子目录

        Returns:
            {脚本名(不含扩展名): 脚本路径} 的字典
        """
        scripts = {}
        scripts_dir = self.skill_dir / "scripts"
        if not scripts_dir.is_dir():
            return scripts

        for py_file in scripts_dir.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            key = py_file.stem
            scripts[key] = py_file
        return scripts

    def get_agents(self) -> dict:
        """扫描 agents/ 子目录的 *.md 文件

        Returns:
            {文件名(不含扩展名): 文件内容} 的字典
        """
        agents = {}
        agents_dir = self.skill_dir / "agents"
        if not agents_dir.is_dir():
            return agents

        for md_file in agents_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                key = md_file.stem
                agents[key] = content
            except (OSError, UnicodeDecodeError):
                continue
        return agents

    @staticmethod
    def _parse_args(args: str) -> dict:
        """解析参数字符串

        支持两种格式：
        1. --key=value 格式
        2. 按位置匹配（第一个参数匹配第一个 argument）
        """
        result = {}

        # 尝试 --key=value 格式
        kv_pattern = re.compile(r'--(\w[\w-]*)=(?:"([^"]*)"|\'([^\']*)\'|(\S+))')
        for match in kv_pattern.finditer(args):
            key = match.group(1).replace("-", "_")
            value = match.group(2) or match.group(3) or match.group(4) or ""
            result[key] = value

        # 如果没有找到 key=value 格式，整个 args 作为第一个参数
        if not result:
            result["__raw__"] = args.strip()

        return result
