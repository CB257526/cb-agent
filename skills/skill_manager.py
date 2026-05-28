"""Skill 管理器

负责发现、解析、加载和匹配 Skill。
从 .cbagent/skills/ 目录扫描 SKILL.md 文件，解析 frontmatter 和正文。
"""

import re
from pathlib import Path
from typing import Optional

from .skill import Skill


# 已知的 frontmatter 字段
KNOWN_FIELDS = {
    'name', 'description', 'when_to_use', 'allowed-tools',
    'arguments', 'argument-hint', 'model', 'user-invocable',
    'disable-model-invocation', 'license', 'metadata', 'compatibility'
}


class SkillManager:
    """Skill 管理器

    扫描指定目录下的 SKILL.md 文件，解析并管理所有 Skill。
    """

    def __init__(self, skills_dir: Path = None):
        """初始化管理器

        Args:
            skills_dir: Skill 目录路径，默认为 .cbagent/skills/
        """
        if skills_dir is None:
            # 默认使用项目根目录下的 .cbagent/skills/
            skills_dir = Path(__file__).parent.parent / ".cbagent" / "skills"

        self._skills_dir = Path(skills_dir)
        self._skills: dict[str, Skill] = {}

        # 启动时自动扫描发现
        self._discover_skills()

    def _discover_skills(self):
        """扫描 skills_dir，发现并解析所有 SKILL.md"""
        if not self._skills_dir.is_dir():
            return

        for skill_dir in self._skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue

            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue

            try:
                skill = self._parse_skill_file(skill_md)
                if skill:
                    self._skills[skill.name] = skill
            except Exception as e:
                # 解析失败跳过，不影响其他 skill
                print(f"⚠️ 解析 Skill 失败 ({skill_md}): {e}")

    def _parse_skill_file(self, skill_md: Path) -> Optional[Skill]:
        """解析单个 SKILL.md 文件

        Args:
            skill_md: SKILL.md 文件路径

        Returns:
            解析后的 Skill 对象，或 None
        """
        content = skill_md.read_text(encoding="utf-8")

        # 提取 frontmatter
        frontmatter, body = self._split_frontmatter(content)
        if frontmatter is None:
            return None

        # 解析 frontmatter 字段
        fields = self._parse_frontmatter(frontmatter)

        # name 和 description 是必需的
        name = fields.get("name", "").strip()
        description = fields.get("description", "").strip()
        if not name or not description:
            return None

        # 解析列表类型字段
        allowed_tools = self._parse_list_field(fields.get("allowed-tools"))
        arguments = self._parse_list_field(fields.get("arguments"))

        # 解析布尔字段
        user_invocable = self._parse_bool(fields.get("user-invocable"), True)
        disable_model_invocation = self._parse_bool(fields.get("disable-model-invocation"), False)

        return Skill(
            name=name,
            description=description,
            body=body.strip(),
            skill_dir=skill_md.parent,
            when_to_use=fields.get("when_to_use", "").strip(),
            allowed_tools=allowed_tools,
            arguments=arguments,
            argument_hint=fields.get("argument-hint", "").strip() or None,
            model=fields.get("model", "").strip() or None,
            user_invocable=user_invocable,
            disable_model_invocation=disable_model_invocation,
            license=fields.get("license", "").strip() or None,
            metadata=None,  # metadata 暂不解析复杂结构
            compatibility=fields.get("compatibility", "").strip() or None,
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple:
        """分离 YAML frontmatter 和 Markdown 正文

        Returns:
            (frontmatter_str, body_str) 或 (None, content) 如果没有 frontmatter
        """
        # 匹配 --- 开头和结尾之间的内容
        match = re.match(r'^---\s*\n([\s\S]*?)---\s*\n?([\s\S]*)$', content)
        if not match:
            return None, content

        return match.group(1), match.group(2)

    @staticmethod
    def _parse_frontmatter(frontmatter: str) -> dict:
        """解析 frontmatter 字段

        不使用 PyYAML，逐行解析简单的 key: value 格式。
        支持多行列表值（以 - 开头的行）。
        """
        fields = {}
        current_key = None
        current_list = None

        for line in frontmatter.split('\n'):
            stripped = line.strip()
            if not stripped:
                continue

            # 检查是否是列表项
            if stripped.startswith('- ') and current_key:
                if current_list is None:
                    current_list = []
                current_list.append(stripped[2:].strip())
                fields[current_key] = current_list
                continue

            # 检查是否是 key: value 行
            match = re.match(r'^([\w-]+)\s*:\s*(.*)$', stripped)
            if match:
                # 如果之前有列表，重置
                current_key = match.group(1).strip()
                value = match.group(2).strip()

                # 去除引号
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]

                if value:
                    fields[current_key] = value
                    current_list = None
                else:
                    # 值为空，可能是多行列表的开始
                    current_list = None
            else:
                # 续行（长文本 description 的换行情况）
                if current_key and current_key in fields:
                    fields[current_key] += " " + stripped

        return fields

    @staticmethod
    def _parse_list_field(value) -> Optional[list]:
        """解析列表类型字段"""
        if value is None:
            return None
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            # 逗号分隔的字符串也视为列表
            return [item.strip() for item in value.split(",") if item.strip()]
        return None

    @staticmethod
    def _parse_bool(value, default: bool = False) -> bool:
        """解析布尔类型字段"""
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1")
        return default

    # ==================== 公共 API ====================

    def list_skills(self) -> list:
        """返回所有已发现的 Skill 列表"""
        return list(self._skills.values())

    def get_skill(self, name: str) -> Optional[Skill]:
        """按名称获取 Skill"""
        return self._skills.get(name)

    def build_skills_overview(self) -> str:
        """构建 L1 提示词片段，用于系统提示词注入

        Returns:
            格式化的 Skill 列表字符串
        """
        if not self._skills:
            return ""

        lines = ["<available-skills>", "以下 Skill 可通过 Skill 工具调用：", ""]
        for skill in self._skills.values():
            lines.append(skill.to_metadata_string())
        lines.append("")
        lines.append("当用户请求匹配某个 Skill 的使用场景时，使用 Skill 工具调用对应的 Skill。")
        lines.append("</available-skills>")

        return "\n".join(lines)

    def load_skill_content(self, name: str, args: str = "") -> str:
        """加载 Skill 的 L2 内容（仅 SKILL.md 正文）

        参考文档不在此处加载。SKILL.md 正文中通常会指引 LLM
        按需读取特定参考文档（如"如需高级功能请参阅 REFERENCE.md"），
        LLM 判断需要时通过 load_skill_reference() 单独加载。

        Args:
            name: Skill 名称
            args: 用户传入的参数

        Returns:
            Skill 正文内容字符串（不含参考文档）
        """
        skill = self.get_skill(name)
        if not skill:
            return f"未找到名为 '{name}' 的 Skill"

        # 渲染正文（变量替换）
        body = skill.render(args)

        # 列出可用的参考文档名称，提示 LLM 可按需加载
        refs = skill.get_references()
        parts = [f"## Skill: {skill.name}", "", body]

        if refs:
            ref_names = ", ".join(refs.keys())
            parts.append("")
            parts.append(f"[可用参考文档: {ref_names} — 如需查看，调用 load_skill_reference 加载]")

        return "\n".join(parts)

    def load_skill_reference(self, name: str, reference_name: str) -> str:
        """加载 Skill 的单个参考文档

        Args:
            name: Skill 名称
            reference_name: 参考文档名称（不含 .md 扩展名）

        Returns:
            参考文档内容，或错误信息
        """
        skill = self.get_skill(name)
        if not skill:
            return f"未找到名为 '{name}' 的 Skill"

        refs = skill.get_references()
        if reference_name not in refs:
            available = list(refs.keys())
            return f"Skill '{name}' 中未找到参考文档 '{reference_name}'。可用文档: {', '.join(available)}"

        return refs[reference_name]

    def match_skill(self, user_message: str) -> Optional[str]:
        """关键词匹配 Skill（降级方案，用于无 function-calling 的场景）

        Args:
            user_message: 用户消息

        Returns:
            匹配的 Skill 名称，或 None
        """
        if not self._skills:
            return None

        user_lower = user_message.lower()

        # 优先匹配 when_to_use，其次匹配 description
        best_match = None
        best_score = 0

        for skill in self._skills.values():
            score = 0
            # 检查 when_to_use 中的关键词
            if skill.when_to_use:
                keywords = self._extract_keywords(skill.when_to_use)
                for kw in keywords:
                    if kw in user_lower:
                        score += 2

            # 检查 description 中的关键词
            desc_keywords = self._extract_keywords(skill.description)
            for kw in desc_keywords:
                if kw in user_lower:
                    score += 1

            # 检查 name 是否被提及
            if skill.name in user_lower:
                score += 5

            if score > best_score:
                best_score = score
                best_match = skill.name

        return best_match if best_score > 0 else None

    @staticmethod
    def _extract_keywords(text: str) -> list:
        """从文本中提取关键词（简单实现：按空格分词，过滤短词）"""
        # 移除标点，按空格分词
        cleaned = re.sub(r'[^\w\s]', ' ', text.lower())
        words = cleaned.split()
        # 过滤长度小于 3 的词和常见停用词
        stop_words = {"the", "a", "an", "is", "are", "was", "were", "be",
                      "been", "being", "have", "has", "had", "do", "does",
                      "did", "will", "would", "could", "should", "may",
                      "might", "can", "shall", "to", "of", "in", "for",
                      "on", "with", "at", "by", "from", "as", "into",
                      "through", "during", "before", "after", "and", "but",
                      "or", "not", "no", "if", "then", "else", "when",
                      "this", "that", "these", "those", "it", "its",
                      "you", "your", "i", "my", "we", "our", "he", "she",
                      "they", "them", "their", "any", "all", "each",
                      "every", "both", "few", "more", "most", "other",
                      "some", "such", "than", "too", "very", "just"}
        return [w for w in words if len(w) >= 3 and w not in stop_words]
