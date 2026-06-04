"""Skill 管理器

负责发现、解析、加载和匹配 Skill。
从 .cbagent/skills/ 目录扫描 SKILL.md 文件，解析 frontmatter 和正文。

优化特性（v2）:
- 条件激活: paths frontmatter 声明 glob 模式，仅操作匹配文件时激活 Skill
- 预算控制: L1 概览限制在上下文窗口的 1%，三级降级（完整→紧凑→仅名称）
- 使用追踪: 记录调用频率，7天半衰期指数衰减排序
- 别名支持: aliases frontmatter 注册备用名称
- 热重载: 基于 mtime 的文件变更检测
"""

import math
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional, Sequence

from .skill import Skill


# 已知的 frontmatter 字段
KNOWN_FIELDS = {
    'name', 'description', 'when_to_use', 'allowed-tools',
    'arguments', 'argument-hint', 'model', 'user-invocable',
    'disable-model-invocation', 'license', 'metadata', 'compatibility',
    'paths', 'aliases', 'version'
}


class SkillManager:
    """Skill 管理器

    扫描指定目录下的 SKILL.md 文件，解析并管理所有 Skill。
    """

    def __init__(self, skills_dir: Path | Sequence[Path] = None):
        """初始化管理器

        Args:
            skills_dir: Skill 目录路径；默认为用户级 ~/.agents/skills + 项目级 .cbagent/skills/。
        """
        if skills_dir is None:
            # 默认同时扫描用户级和项目级 Skill。用户级跨项目共享；项目级靠后扫描，
            # 同名 Skill 可覆盖用户级版本。
            project_skills_dir = Path(__file__).resolve().parent.parent / ".cbagent" / "skills"
            user_skills_dir = Path.home() / ".agents" / "skills"
            skills_dirs = [user_skills_dir, project_skills_dir]
        elif isinstance(skills_dir, (str, Path)):
            skills_dirs = [Path(skills_dir)]
        else:
            skills_dirs = [Path(item) for item in skills_dir]

        self._skills_dirs = [Path(item).resolve() for item in skills_dirs]
        self._skills: dict[str, Skill] = {}

        # 使用频率追踪: name -> 时间戳列表
        self._usage: dict[str, list[float]] = defaultdict(list)
        # 防抖: name -> 上次记录时间
        self._last_record_time: dict[str, float] = {}

        # 热重载: SKILL.md 路径 -> 上次 mtime
        self._last_mtime_cache: dict[str, float] = {}

        # 启动时自动扫描发现
        self._discover_skills()

    def _discover_skills(self):
        """扫描所有 skills_dir，发现并解析所有 SKILL.md"""
        for skills_dir in self._skills_dirs:
            self._discover_skills_in_dir(skills_dir)

    def _discover_skills_in_dir(self, skills_dir: Path):
        """扫描单个 skills_dir。"""
        if not skills_dir.is_dir():
            return

        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue

            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue

            try:
                # 记录 mtime 用于热重载检测
                self._last_mtime_cache[str(skill_md)] = skill_md.stat().st_mtime

                skill = self._parse_skill_file(skill_md)
                if skill:
                    old_skill = self._skills.get(skill.name)
                    if old_skill is not None and old_skill is not skill:
                        self._skills = {
                            key: value
                            for key, value in self._skills.items()
                            if value is not old_skill
                        }
                    self._skills[skill.name] = skill
                    # 注册别名（共享同一 Skill 对象引用）
                    if skill.aliases:
                        for alias in skill.aliases:
                            if alias not in self._skills:
                                self._skills[alias] = skill
            except Exception as e:
                # 解析失败跳过，不影响其他 skill
                print(f"警告：解析 Skill 失败 ({skill_md}): {e}")

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
        paths = self._parse_list_field(fields.get("paths"))
        aliases = self._parse_list_field(fields.get("aliases"))

        # 解析布尔字段（收紧规则：只接受 true / "true"）
        user_invocable = self._parse_bool(fields.get("user-invocable"), True)
        disable_model_invocation = self._parse_bool(fields.get("disable-model-invocation"), False)

        # 版本号
        version = fields.get("version", "").strip() or None

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
            paths=paths,
            aliases=aliases,
            version=version,
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
        支持多行列表值（以 - 开头的行）和 YAML > 折叠标量。
        """
        fields = {}
        current_key = None
        current_list = None
        in_folded = False       # > 折叠标量中
        folded_lines = []       # 收集折叠标量的行

        for line in frontmatter.split('\n'):
            stripped = line.strip()
            if not stripped:
                if in_folded:
                    folded_lines.append("")  # 保留空行以便段分隔
                continue

            # 检查是否是列表项
            if stripped.startswith('- ') and current_key and not in_folded:
                if current_list is None:
                    current_list = []
                current_list.append(stripped[2:].strip())
                fields[current_key] = current_list
                continue

            # 检查是否是 key: value 行
            match = re.match(r'^([\w-]+)\s*:\s*(.*)$', stripped)
            if match:
                # 结束之前的折叠标量
                if in_folded and current_key:
                    # 用空格连接非空行（YAML 折叠标量语义）
                    fields[current_key] = " ".join(
                        l.strip() for l in folded_lines if l.strip()
                    )
                    in_folded = False
                    folded_lines = []

                current_key = match.group(1).strip()
                value = match.group(2).strip()

                # 去除引号
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]

                if value == ">":
                    # YAML 折叠标量：后续行用空格拼接
                    in_folded = True
                    folded_lines = []
                    current_list = None
                elif value:
                    fields[current_key] = value
                    current_list = None
                else:
                    # 值为空，可能是多行列表的开始
                    current_list = None
            else:
                if in_folded:
                    folded_lines.append(stripped)
                elif current_key and current_key in fields:
                    fields[current_key] += " " + stripped

        # 结束任何残留的折叠标量
        if in_folded and current_key:
            fields[current_key] = " ".join(
                l.strip() for l in folded_lines if l.strip()
            )

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
        """解析布尔类型字段

        收紧规则：只接受 true / "true"（对齐 Claude Code）。
        不再接受 yes/1/on 等非标准值。
        """
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() == "true"
        return default

    # ==================== 使用频率追踪 ====================

    def record_usage(self, name: str):
        """记录 Skill 调用，60s 防抖

        Args:
            name: Skill 名称
        """
        now = time.time()
        last = self._last_record_time.get(name, 0)
        if now - last < 60:
            return  # 防抖
        self._last_record_time[name] = now
        self._usage[name].append(now)
        # 清理 30 天前的记录
        cutoff = now - 30 * 86400
        self._usage[name] = [t for t in self._usage[name] if t > cutoff]

    def get_usage_score(self, name: str) -> float:
        """计算使用分数（7天半衰期指数衰减）

        最近使用权重大，7天前半衰。最低分 0.1（不完全归零）。

        Args:
            name: Skill 名称

        Returns:
            衰减后的使用分数
        """
        if name not in self._usage:
            return 0.0
        now = time.time()
        half_life = 7 * 86400  # 7 天
        decay = 0.693 / half_life  # ln(2) / half_life
        score = 0.0
        for ts in self._usage[name]:
            age = now - ts
            score += math.exp(-decay * age)
        return score

    # ==================== 公共 API ====================

    def list_skills(self) -> list:
        """返回所有已发现的 Skill 列表"""
        self.check_for_changes()
        seen = set()
        result = []
        for skill in self._skills.values():
            if skill.name in seen:
                continue
            seen.add(skill.name)
            result.append(skill)
        return result

    def format_skill_list(self) -> str:
        """格式化当前 Skill 列表，供 CLI/TUI 展示。"""
        skills = self.list_skills()
        if not skills:
            return "当前没有发现任何 Skill。"
        lines = [f"已发现 {len(skills)} 个 Skill："]
        for skill in skills:
            desc = (skill.description or "")[:120]
            lines.append(f"  - {skill.name}: {desc}")
        return "\n".join(lines)

    def get_skill(self, name: str) -> Optional[Skill]:
        """按名称获取 Skill（支持别名查找）"""
        self.check_for_changes()
        return self._skills.get(name)

    @staticmethod
    def extract_file_paths(messages: list) -> list:
        """从对话消息中提取文件路径

        用于 paths 条件激活过滤。

        Args:
            messages: 对话消息列表

        Returns:
            提取到的文件路径列表
        """
        paths = []
        # 匹配常见文件路径模式
        path_pattern = re.compile(
            r'''(?:^|[\s"'(\[<])((?:[A-Za-z]:\\|/)?[\w\-./\\]+\.\w{1,10})(?:$|[\s"'.,;)\]>])'''
        )
        for msg in messages[-5:]:  # 仅最近 5 条消息
            content = msg.get("content", "")
            if isinstance(content, str):
                for match in path_pattern.finditer(content):
                    paths.append(match.group(1))
        return paths

    def build_skills_overview(
        self,
        file_paths: Optional[list] = None,
        max_chars: int = 2000,
    ) -> str:
        """构建 L1 提示词片段，用于系统提示词注入

        支持条件激活过滤和预算控制。三级降级：
        1. full -- 完整 description + when_to_use
        2. compact -- name + 截断 description
        3. name_only -- 仅名称

        Args:
            file_paths: 当前操作的文件路径，用于条件激活过滤
            max_chars: 最大字符数预算（约上下文窗口 1% 对应约 500 tokens）

        Returns:
            格式化的 Skill 列表字符串
        """
        self.check_for_changes()
        if not self._skills:
            return ""

        # 按使用分数降序排列
        skills_by_score = sorted(
            self._skills.values(),
            key=lambda s: self.get_usage_score(s.name),
            reverse=True,
        )

        # 去重（别名共享同一对象引用）
        seen = set()
        candidates = []
        for skill in skills_by_score:
            if skill.name in seen:
                continue
            seen.add(skill.name)
            # 条件激活过滤
            if file_paths and not skill.matches_paths(file_paths):
                continue
            candidates.append(skill)

        if not candidates:
            return ""

        # 尝试 full 级别
        for detail in ("full", "compact", "name_only"):
            result = SkillManager._format_overview(candidates, detail)
            if len(result) <= max_chars:
                return result

        # 极限预算：只列名称
        return SkillManager._format_overview(candidates, "name_only")

    @staticmethod
    def _format_overview(candidates: list, detail: str) -> str:
        """格式化 Skill 列表

        Args:
            candidates: 候选 Skill 列表
            detail: 详细级别 (full/compact/name_only)
        """
        lines = ["<available-skills>", "以下 Skill 可通过 Skill 工具调用：", ""]
        for skill in candidates:
            lines.append(skill.to_metadata_string(detail))
        lines.append("")
        lines.append("当用户请求匹配某个 Skill 的使用场景时，使用 Skill 工具调用对应的 Skill。")
        lines.append("</available-skills>")
        return "\n".join(lines)

    def build_skills_overview_for_model(
        self,
        model_context_window: int = 200_000,
        file_paths: Optional[list] = None,
    ) -> str:
        """根据模型上下文窗口计算预算并构建 L1 概览

        Args:
            model_context_window: 模型的上下文窗口大小（token 数）
            file_paths: 当前操作的文件路径

        Returns:
            格式化的 Skill 列表字符串
        """
        # 约 4 字符/token，预算 = 上下文窗口的 1%
        max_chars = int(model_context_window * 0.01 * 4)
        return self.build_skills_overview(file_paths=file_paths, max_chars=max_chars)

    def load_skill_content(self, name: str, args: str = "") -> str:
        """加载 Skill 的 L2 内容（仅 SKILL.md 正文）

        参考文档不在此处加载。SKILL.md 正文中通常会指引 LLM
        按需读取特定参考文档（如"如需高级功能请参阅 REFERENCE.md"），
        LLM 判断需要时可再次调用 skill 工具，并设置 document 参数单独加载。

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
            parts.append(
                f"[可用参考文档: {ref_names} -- 如需查看，请再次调用 skill 工具并设置 document 参数]"
            )

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

        # 按使用分数排序，优先推荐常用 Skill
        candidates = sorted(
            self._skills.values(),
            key=lambda s: self.get_usage_score(s.name),
            reverse=True,
        )

        # 去重（别名共享对象）
        seen = set()
        best_match = None
        best_score = 0

        for skill in candidates:
            if skill.name in seen:
                continue
            seen.add(skill.name)

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

            # 检查别名
            if skill.aliases:
                for alias in skill.aliases:
                    if alias in user_lower:
                        score += 5

            if score > best_score:
                best_score = score
                best_match = skill.name

        return best_match if best_score > 0 else None

    # ==================== 热重载 ====================

    def check_for_changes(self) -> bool:
        """检查 SKILL.md 文件是否被修改，如有变更则自动重载

        基于 mtime 的轮询检测，无额外依赖。
        可在每次对话轮次前调用。

        Returns:
            True 如果检测到变更并已重载
        """
        if not any(skills_dir.is_dir() for skills_dir in self._skills_dirs):
            return False

        changed = False
        current_files = set()
        for skills_dir in self._skills_dirs:
            if not skills_dir.is_dir():
                continue
            for skill_dir in skills_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.is_file():
                    continue

                try:
                    key = str(skill_md)
                    current_files.add(key)
                    mtime = skill_md.stat().st_mtime
                    cached = self._last_mtime_cache.get(key, 0)
                    if cached == 0 or mtime > cached:
                        self._last_mtime_cache[key] = mtime
                        changed = True
                except OSError:
                    continue

        if set(self._last_mtime_cache.keys()) != current_files:
            self._last_mtime_cache = {
                key: value
                for key, value in self._last_mtime_cache.items()
                if key in current_files
            }
            changed = True

        if changed:
            self._skills.clear()
            self._discover_skills()

        return changed

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
