"""Skill 发现、索引渲染和轻量遥测。

这个模块刻意不再实现“执行 Skill”的运行时。Skill 只是一个目录里的
SKILL.md 操作手册：启动时只把 name / description / file path 注入 prompt；
需要正文时由用户显式触发，或由模型自己用 file_read 读取 SKILL.md；脚本执行
统一复用 bash，并在 bash 之后做命中记录。
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import yaml

from .skill import Skill

logger = logging.getLogger(__name__)


# 递归扫描的硬限制。Skill 目录是用户可扩展入口，必须防止误扫整个硬盘、
# node_modules、虚拟环境等大目录。6 层基本覆盖 workspace/.agents/skills/foo。
MAX_SCAN_DEPTH = 6
MAX_SCAN_DIRS = 2000

# bash 事后识别时，只把这些扩展名当作“可能是 Skill 脚本”的文件。
# 这里故意保守：命中记录是遥测，不应该误把普通数据文件记成 Skill 调用。
SCRIPT_EXTENSIONS = {
    ".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts",
    ".rb", ".pl", ".php",
}
SCRIPT_RUNNERS = {
    "bash", "sh", "zsh", "node", "deno", "ruby", "perl", "php",
}

# 显式触发语法：
# - 普通形式：$skill-name / $namespace:skill-name
# - Markdown 链接形式：[$skill](path/to/SKILL.md)
# 路径形式优先，解决同名 Skill 或 namespace 简写的歧义。
MENTION_RE = re.compile(r"(?<![\w$])\$([A-Za-z0-9_:-]+)")
MARKDOWN_MENTION_RE = re.compile(
    r"\[\$([A-Za-z0-9_:-]+)\]\(([^)]+)\)"
)


@dataclass(frozen=True)
class SkillScriptHit:
    """一次 bash 命令命中 Skill scripts/ 下脚本的记录。"""

    skill_name: str
    skill_file: str
    script_path: str

    def to_dict(self) -> dict[str, str]:
        return {
            "skill_name": self.skill_name,
            "skill_file": self.skill_file,
            "script_path": self.script_path,
        }


class SkillManager:
    """发现 SKILL.md 文件，并为模型生成预算内 Skill 目录。"""

    def __init__(self, skills_dir: Path | str | Sequence[Path | str] | None = None):
        # _skills_dirs 按优先级从低到高扫描；后扫描的同名 Skill 覆盖前者。
        self._skills_dirs = self._resolve_skill_roots(skills_dir)

        # name -> Skill。这里只注册正式 name，不再注册 aliases、paths 等旧字段。
        self._skills: dict[str, Skill] = {}

        # plain name 索引用于 $demo 匹配 namespace:demo；如果候选不唯一则不匹配。
        self._plain_name_index: dict[str, list[Skill]] = {}

        # scripts/ 目录反查 Skill，供 bash 工具执行后识别“这条命令用了哪个 Skill”。
        self._script_dirs: dict[Path, Skill] = {}

        # SKILL.md mtime 快照。调用方不用手动 reload，每次 public API 都会轻量检查。
        self._last_mtime_cache: dict[str, float] = {}

        # 轻量使用记录：目前只在进程内保留，用于调试和未来遥测扩展。
        self._usage: dict[str, list[float]] = defaultdict(list)
        self._last_record_time: dict[tuple[str, str], float] = {}
        self._script_hits: list[dict[str, Any]] = []
        self._discover_skills()

    # ------------------------------------------------------------------
    # Discovery / 发现阶段

    @staticmethod
    def _resolve_skill_roots(
        skills_dir: Path | str | Sequence[Path | str] | None,
    ) -> list[Path]:
        """计算要扫描的 Skill 根目录。

        测试或嵌入场景传入 skills_dir 时完全尊重调用方顺序；默认运行时使用三层：
        用户级 ~/.agents/skills、仓库级 .agents/skills、安装/项目级 .cbagent/skills。
        """

        if skills_dir is not None:
            if isinstance(skills_dir, (str, Path)):
                raw_roots: Iterable[Path | str] = [skills_dir]
            else:
                raw_roots = skills_dir
            return [Path(item).expanduser().resolve() for item in raw_roots]

        # package_root 是 cb-agent 包根；内置示例 Skill 当前放在 .cbagent/skills。
        project_root = SkillManager._find_project_root(Path.cwd())
        package_root = Path(__file__).resolve().parent.parent
        roots = [
            Path.home() / ".agents" / "skills",
            project_root / ".agents" / "skills",
            package_root / ".cbagent" / "skills",
        ]
        result: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            resolved = root.expanduser().resolve()
            # 去重保持顺序，避免 cwd 恰好等于 package_root 时重复扫描同一层。
            if resolved in seen:
                continue
            seen.add(resolved)
            result.append(resolved)
        return result

    @staticmethod
    def _find_project_root(start: Path) -> Path:
        """从 cwd 向上找项目根；找不到 marker 就退回 cwd。"""

        markers = {".git", "pyproject.toml", "setup.py", "package.json"}
        current = start.expanduser().resolve()
        for candidate in (current, *current.parents):
            if any((candidate / marker).exists() for marker in markers):
                return candidate
        return current

    def _discover_skills(self) -> None:
        """全量重建索引。

        这里选择“清空后重建”，而不是增量更新。Skill 数量通常很小，全量重建
        更容易保证覆盖顺序、plain name 索引和 scripts_dir 索引始终一致。
        """

        self._skills.clear()
        self._plain_name_index.clear()
        self._script_dirs.clear()
        self._last_mtime_cache.clear()

        for root in self._skills_dirs:
            for skill_file in self._iter_skill_files(root):
                try:
                    skill = self._parse_skill_file(skill_file)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("parse skill failed: %s: %s", skill_file, exc)
                    continue
                if skill is None:
                    continue
                # 记录 mtime 用于后续 check_for_changes 的快速比较。
                self._last_mtime_cache[str(skill_file)] = skill_file.stat().st_mtime
                self._register_skill(skill)

    def _iter_skill_files(self, root: Path) -> Iterable[Path]:
        """受限递归遍历 root 下的 SKILL.md。

        发现一个目录本身就是 Skill 后，不再继续深入该目录。这样 scripts/、
        references/ 里的 Markdown 不会被误当成嵌套 Skill。
        """

        if not root.is_dir():
            return

        # 用显式 stack 而不是 Path.rglob，方便限制深度和目录数量。
        visited: set[Path] = set()
        dirs_seen = 0
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                real = current.resolve()
            except OSError:
                continue
            # resolve 后去重可以避免符号链接形成扫描环。
            if real in visited:
                continue
            visited.add(real)
            dirs_seen += 1
            if dirs_seen > MAX_SCAN_DIRS:
                logger.warning("skill scan stopped at directory limit: %s", root)
                return

            skill_file = current / "SKILL.md"
            if skill_file.is_file():
                # Skill 的强约定锚点是大写 SKILL.md。
                yield skill_file.resolve()
                continue

            if depth >= MAX_SCAN_DEPTH:
                continue
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name)
            except OSError:
                continue
            for child in reversed(children):
                if child.is_dir():
                    stack.append((child, depth + 1))

    def _register_skill(self, skill: Skill) -> None:
        """注册一个 Skill，并同步脚本目录和 plain-name 索引。"""

        # 同名覆盖发生在这里；因为扫描顺序是低优先级到高优先级，后者自然胜出。
        old_skill = self._skills.get(skill.name)
        if old_skill is not None:
            # 覆盖 Skill 时必须同步清理脚本反查索引，否则旧目录里的脚本仍会被
            # record_script_hits 误记为已安装 Skill 的一次命中。
            for registered_dir, registered_skill in list(self._script_dirs.items()):
                if registered_skill is old_skill:
                    self._script_dirs.pop(registered_dir, None)
        self._skills[skill.name] = skill
        scripts_dir = skill.scripts_dir
        if scripts_dir.is_dir():
            self._script_dirs[scripts_dir.resolve()] = skill
        self._rebuild_plain_name_index()

    def _rebuild_plain_name_index(self) -> None:
        """重建 `$plain-name` 查询表。

        name 带 namespace 时（例如 plugin:foo），plain name 是最后一个冒号后
        的部分。只有 plain name 唯一时才允许 $foo 匹配，避免静默选错 Skill。
        """

        index: dict[str, list[Skill]] = defaultdict(list)
        for skill in self._skills.values():
            index[skill.name].append(skill)
            plain = skill.name.rsplit(":", 1)[-1]
            if plain != skill.name:
                index[plain].append(skill)
        self._plain_name_index = dict(index)

    # ------------------------------------------------------------------
    # Parsing / SKILL.md 解析

    def _parse_skill_file(self, skill_md: Path) -> Optional[Skill]:
        """读取并解析单个 SKILL.md。

        这里只消费索引型元数据：name、description、metadata.short-description。
        allowed_tools、arguments、aliases、paths 等旧字段即使存在也会被忽略，
        避免 Skill frontmatter 再次变成一套复杂运行时配置。
        """

        content = skill_md.read_text(encoding="utf-8")
        frontmatter, body = self._split_frontmatter(content)
        if frontmatter is None:
            return None
        metadata = self._parse_frontmatter(frontmatter)

        # name 缺失时用目录名兜底；description 缺失时保留空串。
        # 这样第三方 Skill 不完全规范时仍可被发现和显式读取。
        name = str(metadata.get("name") or skill_md.parent.name).strip()
        description = str(metadata.get("description") or "").strip()
        short_description = self._short_description(metadata)
        if not name:
            return None

        return Skill(
            name=name,
            description=description,
            body=body.strip(),
            skill_dir=skill_md.parent.resolve(),
            short_description=short_description,
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[Optional[str], str]:
        """拆分 frontmatter 和 Markdown 正文。

        返回的 body 不含 frontmatter；frontmatter 已在 overview 阶段承担索引作用，
        正文按需加载时不重复注入，减少上下文消耗。
        """

        match = re.match(r"^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*\r?\n?([\s\S]*)$", content)
        if not match:
            return None, content
        return match.group(1), match.group(2)

    @staticmethod
    def _parse_frontmatter(frontmatter: str) -> dict[str, Any]:
        """解析 YAML frontmatter，带一次容错修复重试。

        常见脏数据是 `description: Build for AWS: ECS` 这种未加引号的冒号
        字符串。第一遍原样解析失败后，第二遍只做行级加引号修复；两遍失败
        返回空 dict，让坏 Skill 不影响其他 Skill。
        """

        for candidate in (frontmatter, SkillManager._repair_frontmatter(frontmatter)):
            try:
                parsed = yaml.safe_load(candidate) or {}
            except yaml.YAMLError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    @staticmethod
    def _repair_frontmatter(frontmatter: str) -> str:
        """保守修复一类 YAML 冒号字符串错误。

        注意这不是通用 YAML formatter：它只处理 `key: value: more` 这种值里
        含 `: ` 的单行标量。列表、对象、折叠标量、已经加引号的值都原样保留。
        """

        repaired: list[str] = []
        key_value = re.compile(r"^(\s*[\w.-]+\s*:\s*)(.+?)\s*$")
        for line in frontmatter.splitlines():
            match = key_value.match(line)
            if not match:
                repaired.append(line)
                continue
            prefix, value = match.group(1), match.group(2)
            stripped = value.strip()
            if (
                ": " not in stripped
                or stripped.startswith(('"', "'", "[", "{", "|", ">"))
            ):
                repaired.append(line)
                continue
            escaped = stripped.replace("\\", "\\\\").replace('"', '\\"')
            repaired.append(f'{prefix}"{escaped}"')
        return "\n".join(repaired)

    @staticmethod
    def _short_description(metadata: dict[str, Any]) -> Optional[str]:
        """读取紧凑渲染用的短描述。

        规范字段是 metadata.short-description；顶层 short-description 作为旧写法
        兼容，避免早期手写 Skill 失效。
        """

        meta = metadata.get("metadata")
        if isinstance(meta, dict):
            value = meta.get("short-description")
            if value:
                return str(value).strip()
        value = metadata.get("short-description")
        if value:
            return str(value).strip()
        return None

    # ------------------------------------------------------------------
    # Public API / 对外接口

    def check_for_changes(self) -> bool:
        """检查 SKILL.md 文件集合或 mtime 是否变化。

        一旦发现新增、删除或修改，就全量重建索引。调用方不需要理解缓存一致性，
        只要走 list/get/load/build 这些 public API 就能看到最新 Skill。
        """

        current: dict[str, float] = {}
        for root in self._skills_dirs:
            for skill_file in self._iter_skill_files(root) or []:
                try:
                    current[str(skill_file)] = skill_file.stat().st_mtime
                except OSError:
                    continue
        if current == self._last_mtime_cache:
            return False
        self._discover_skills()
        return True

    def list_skills(self) -> list[Skill]:
        """返回当前发现的唯一 Skill 列表。"""

        self.check_for_changes()
        return list(self._skills.values())

    def get_skill(self, name: str) -> Optional[Skill]:
        """按正式 name 精确获取 Skill。

        这里不做 plain-name 兜底，避免 /foo 这类前端命令在有多个 namespace
        Skill 时误命中。需要显式触发解析时使用 resolve_mention。
        """

        self.check_for_changes()
        return self._skills.get(name)

    def format_skill_list(self) -> str:
        """供 OTUI 展示的中文 Skill 列表，不直接进入模型上下文。"""

        skills = self.list_skills()
        if not skills:
            return "当前没有发现任何 Skill。"
        lines = [f"已发现 {len(skills)} 个 Skill："]
        for skill in skills:
            desc = skill.short_description or skill.description
            lines.append(f"  - {skill.name}: {desc[:120]}")
        return "\n".join(lines)

    def build_skills_overview(
        self,
        file_paths: Optional[list[str]] = None,
        max_chars: int = 8000,
    ) -> str:
        """构建注入 prompt 的轻量 Skill 目录。

        只渲染 name + description + SKILL.md 路径，不渲染正文。隐式触发由模型
        根据 description 自行判断，系统不再做关键词或路径激活判断。
        """

        # 旧版 paths 条件激活已经移除；保留参数是为了不打断旧调用点。
        del file_paths
        self.check_for_changes()
        skills = self.list_skills()
        if not skills:
            return ""

        # 五级降级：
        # 1. 完整 description + 绝对路径
        # 2. 截断 description
        # 3. 省略 description
        # 4. 路径公共根别名
        # 5. 只提示数量和裁剪警告
        renderers = (
            lambda: self._format_overview(skills, mode="full"),
            lambda: self._format_overview(skills, mode="truncated"),
            lambda: self._format_overview(skills, mode="no_description"),
            lambda: self._format_overview(skills, mode="alias_paths"),
            lambda: self._format_summary_only(len(skills)),
        )
        for render in renderers:
            output = render()
            if len(output) <= max_chars:
                return output
        return self._format_summary_only(len(skills))

    def build_skills_overview_for_model(
        self,
        model_context_window: int = 200_000,
        file_paths: Optional[list[str]] = None,
    ) -> str:
        """按模型上下文窗口计算 2% 预算后渲染 overview。"""

        max_chars = int(model_context_window * 0.02 * 4)
        return self.build_skills_overview(file_paths=file_paths, max_chars=max_chars)

    def load_skill_content(self, name: str, args: str = "") -> str:
        """显式加载 Skill 正文并包成 <skill> 片段。

        /skill、session.load_skill 和 AgentSession 中的 `$skill` 显式提及都走这里。
        返回值不含 frontmatter，但会带 source / references / scripts 清单，方便模型
        正确解析相对路径。
        """

        skill = self.resolve_mention(name)
        if skill is None:
            return f"未找到名为 '{name}' 的 Skill"
        self.record_usage(skill.name, reason="explicit")
        return self._wrap_skill_body(skill, args=args)

    def load_skill_reference(self, name: str, reference_name: str) -> str:
        """加载某个参考文档。

        新架构下模型一般直接用 file_read 读取 references；这个方法保留给 UI、
        测试和旧内置 Skill 的显式入口。
        """

        skill = self.resolve_mention(name)
        if skill is None:
            return f"未找到名为 '{name}' 的 Skill"

        paths = skill.get_reference_paths()
        path = paths.get(reference_name)
        if path is None:
            available = ", ".join(paths) if paths else "无"
            return f"Skill '{skill.name}' 中未找到参考文档 '{reference_name}'。可用文档: {available}"
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"读取 Skill 参考文档失败: {exc}"

        return "\n".join([
            f"<skill-reference name=\"{skill.name}\" document=\"{reference_name}\">",
            f"source: file:{path}",
            "",
            body,
            "</skill-reference>",
        ])

    def collect_explicit_mentions(self, text: str) -> list[Skill]:
        """从用户文本中提取显式 Skill 触发。

        先解析 Markdown 链接形式，再解析 `$name`。如果同一个 Skill 被多次提到，
        只返回一次，避免同一正文重复注入当前轮。
        """

        self.check_for_changes()
        if not text:
            return []

        matches: list[Skill] = []
        seen: set[str] = set()
        markdown_spans: list[tuple[int, int]] = []

        for match in MARKDOWN_MENTION_RE.finditer(text):
            markdown_spans.append(match.span())
            name = match.group(1)
            target = match.group(2)
            # 路径优先：[$foo](/path/to/bar/SKILL.md) 应按路径命中 bar，
            # 链接文字只作为路径解析失败后的备用名称。
            skill = self.resolve_path(target) or self.resolve_mention(name)
            if skill and skill.name not in seen:
                seen.add(skill.name)
                matches.append(skill)

        for match in MENTION_RE.finditer(text):
            # Markdown link 已按“路径优先”处理；这里跳过 link 文本里的 $name，
            # 避免 `[$a](/path/to/b/SKILL.md)` 同时注入 a 和 b。
            if any(start <= match.start() < end for start, end in markdown_spans):
                continue
            name = match.group(1)
            skill = self.resolve_mention(name)
            if skill and skill.name not in seen:
                seen.add(skill.name)
                matches.append(skill)

        return matches

    def resolve_mention(self, name: str) -> Optional[Skill]:
        """解析 `$name` 显式提及。

        匹配顺序是：正式 name 完全匹配 -> plain name 唯一匹配。plain name 有多个
        候选时返回 None，让用户或模型消歧，不静默选择其中一个。
        """

        self.check_for_changes()
        raw = (name or "").strip().strip("`")
        if not raw:
            return None
        if raw.startswith("$"):
            raw = raw[1:]
        exact = self._skills.get(raw)
        if exact is not None:
            return exact
        candidates = self._plain_name_index.get(raw, [])
        unique = {skill.name: skill for skill in candidates}
        if len(unique) == 1:
            return next(iter(unique.values()))
        return None

    def resolve_path(self, path_text: str) -> Optional[Skill]:
        """通过文件路径反查 Skill。

        支持 file:/abs/path/SKILL.md、Skill 目录路径，或 Skill 目录下任意资源路径。
        这用于 Markdown 显式触发，也方便未来 UI 点击文件定位时找回所属 Skill。
        """

        self.check_for_changes()
        path_text = (path_text or "").strip().strip("<>").strip()
        if path_text.startswith("file:"):
            path_text = path_text[5:]
        if not path_text:
            return None
        try:
            path = Path(path_text).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
            path = path.resolve()
        except OSError:
            return None

        for skill in self._skills.values():
            if path == skill.skill_file or path == skill.skill_dir:
                return skill
            try:
                path.relative_to(skill.skill_dir)
                return skill
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------
    # Bash script hit observer / bash 脚本命中识别

    def identify_script_hits(self, command: str, cwd: str | Path | None = None) -> list[SkillScriptHit]:
        """识别一条 bash 命令是否运行了某个 Skill scripts/ 下的脚本。

        这一步只做“事后识别”，不改变命令是否执行，也不提供额外权限。设计上
        让 agent 继续使用通用 bash，SkillManager 只负责记录这次 bash 和 Skill
        的关联，供调试/遥测使用。
        """

        self.check_for_changes()
        base_cwd = Path(cwd or Path.cwd()).expanduser()
        hits: list[SkillScriptHit] = []
        seen_paths: set[str] = set()
        for argv in self._parse_command_segments(command):
            # parse_pipeline 会把 `python a.py && bash b.sh` 拆成多个 argv。
            script = self._script_arg_from_argv(argv)
            if not script:
                continue
            path = self._resolve_script_arg(script, base_cwd)
            if path is None:
                continue
            skill = self._skill_for_script_path(path)
            if skill is None:
                continue
            key = str(path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            hits.append(SkillScriptHit(
                skill_name=skill.name,
                skill_file=str(skill.skill_file),
                script_path=str(path),
            ))
        return hits

    def record_script_hits(self, command: str, cwd: str | Path | None = None) -> list[dict[str, str]]:
        """记录 bash 脚本命中并返回可序列化 payload。

        BashTool 会把这个 payload 放进 JSON 结果的 skill_script_hits 字段。字段存在
        只是为了透明调试；模型不需要因此再触发或加载 Skill。
        """

        hits = self.identify_script_hits(command, cwd=cwd)
        now = time.time()
        payloads = [hit.to_dict() for hit in hits]
        for payload in payloads:
            self.record_usage(payload["skill_name"], reason="script")
            self._script_hits.append({
                **payload,
                "command": command,
                "cwd": str(cwd or ""),
                "timestamp": now,
            })
        return payloads

    def script_hits(self) -> list[dict[str, Any]]:
        """返回进程内累计的脚本命中记录，主要给测试和调试使用。"""

        return list(self._script_hits)

    @staticmethod
    def _parse_command_segments(command: str) -> list[list[str]]:
        """把 shell 命令拆成简单 argv 列表。

        优先复用 bash_security.parse_pipeline，因为它已经理解管道、&&、; 等
        shell 控制符；解析失败时退回 shlex，保证简单命令仍可识别。
        """

        try:
            from tools.tools.bash_security import parse_pipeline

            return parse_pipeline(command)
        except Exception:
            try:
                return [shlex.split(command)]
            except ValueError:
                return []

    @staticmethod
    def _script_arg_from_argv(argv: list[str]) -> Optional[str]:
        """从 argv 中找出“解释器 + 脚本文件”的脚本参数。

        只识别 python/bash/node 等常见运行器。`python -m`、`python -c` 不是文件
        脚本执行，直接返回 None。
        """

        if not argv:
            return None
        runner = Path(argv[0]).name.lower()
        is_python = runner.startswith("python")
        if not is_python and runner not in SCRIPT_RUNNERS:
            return None

        iterator = iter(argv[1:])
        for token in iterator:
            if token in {"-m", "-c"}:
                return None
            if token.startswith("-"):
                # 这些选项后面跟的是选项值，不是脚本路径。
                if token in {"-I", "--input-type", "--loader", "--require", "-r"}:
                    next(iterator, None)
                continue
            suffix = Path(token).suffix.lower()
            if suffix in SCRIPT_EXTENSIONS:
                return token
            return None
        return None

    @staticmethod
    def _resolve_script_arg(script: str, cwd: Path) -> Optional[Path]:
        """把脚本参数解析成绝对路径。相对路径按 bash 本次 cwd 解析。"""

        try:
            path = Path(script).expanduser()
            if not path.is_absolute():
                path = cwd / path
            return path.resolve()
        except OSError:
            return None

    def _skill_for_script_path(self, script_path: Path) -> Optional[Skill]:
        """判断绝对脚本路径是否位于某个已知 scripts/ 目录下。"""

        suffix = script_path.suffix.lower()
        if suffix not in SCRIPT_EXTENSIONS:
            return None
        for scripts_dir, skill in self._script_dirs.items():
            try:
                script_path.relative_to(scripts_dir)
                return skill
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------
    # Formatting and telemetry / prompt 渲染与轻量遥测

    def record_usage(self, name: str, reason: str = "explicit") -> None:
        """记录 Skill 使用。

        60 秒防抖，避免同一轮显式加载正文、再运行脚本时把计数刷爆。目前只在
        进程内保存，后续要接日志/指标时可以从这里延伸。
        """

        now = time.time()
        key = (name, reason)
        last = self._last_record_time.get(key, 0.0)
        if now - last < 60:
            return
        self._last_record_time[key] = now
        self._usage[name].append(now)

    def _wrap_skill_body(self, skill: Skill, args: str = "") -> str:
        """把正文包装成给模型读的 <skill> 片段。

        source 块明确告诉模型 skill_dir、参考文档和脚本路径，减少模型自己用
        bash/grep 到处搜索的概率。正文仍然只包含 Markdown body，不含 YAML。
        """

        lines = [
            f"<skill name=\"{skill.name}\">",
            "<skill-source>",
            f"file: {skill.skill_file}",
            f"skill_dir: {skill.skill_dir}",
            "relative_paths: Resolve paths mentioned by this skill relative to skill_dir.",
            "scripts: Run bundled scripts with the bash tool when the instructions require it.",
        ]
        refs = skill.get_reference_paths()
        if refs:
            lines.append("references:")
            for name, path in refs.items():
                lines.append(f"- {name}: file:{path}")
        scripts = skill.get_script_paths()
        if scripts:
            lines.append("script_files:")
            for name, path in scripts.items():
                lines.append(f"- {name}: file:{path}")
        lines.extend([
            "</skill-source>",
            "",
            skill.render(args),
            "</skill>",
        ])
        return "\n".join(lines)

    def _format_overview(self, skills: list[Skill], mode: str) -> str:
        """按指定降级模式渲染 Skill 目录。"""

        path_aliases: dict[str, str] = {}
        common_root = ""
        if mode == "alias_paths":
            # 只有到了第四级降级才把绝对路径压成 r0/...，平时保留完整路径更直观。
            common_root = self._common_skill_root(skills)
            if common_root:
                path_aliases["r0"] = common_root

        lines = [
            "<available-skills>",
            "Skills are local markdown operating manuals. Use one when the user explicitly names it or the task clearly matches its description.",
            "To use a skill, read the listed SKILL.md with file_read. There is no skill or run_skill_script tool.",
            "Resolve relative paths from that SKILL.md's directory. Run bundled scripts with bash when instructed.",
        ]
        if mode != "full":
            lines.append("Note: this skill index was compressed to fit the prompt budget.")
        if path_aliases:
            aliases = ", ".join(f"{key}={value}" for key, value in path_aliases.items())
            lines.append(f"Path aliases: {aliases}")
        lines.append("")

        if mode == "truncated":
            # 所有 Skill 使用同一个截断预算，简单可预测；不是 token 级精算。
            max_desc = self._description_budget(skills, lines)
        else:
            max_desc = None

        for skill in skills:
            path = str(skill.skill_file)
            if common_root:
                try:
                    rel = skill.skill_file.relative_to(common_root)
                    path = f"r0/{rel}"
                except ValueError:
                    pass

            if mode == "no_description" or mode == "alias_paths":
                lines.append(f"- {skill.name}: (file: {path})")
                continue

            desc = skill.description
            if mode == "truncated" and max_desc is not None:
                desc = self._clip_description(desc, max_desc)
            lines.append(f"- {skill.name}: {desc} (file: {path})")

        lines.append("</available-skills>")
        return "\n".join(lines)

    @staticmethod
    def _format_summary_only(count: int) -> str:
        """最终降级：连目录行也放不下时，只告诉模型有 Skill 被省略。"""

        return "\n".join([
            "<available-skills>",
            f"{count} skills are available, but the list was omitted because it exceeds the prompt budget.",
            "Use /skills or session.load_skill to inspect installed skills explicitly.",
            "</available-skills>",
        ])

    @staticmethod
    def _description_budget(skills: list[Skill], header_lines: list[str]) -> int:
        """给 description 一个粗略字符预算。

        这里不用 token 估算，避免引入 tiktoken 下载/缓存等不稳定因素。overview
        外层还有 max_chars 兜底，因此字符级预算足够。
        """

        del header_lines
        if not skills:
            return 0
        return max(24, min(120, 240 // max(1, len(skills)) + 40))

    @staticmethod
    def _clip_description(description: str, max_chars: int) -> str:
        """字符级截断 description，并用省略号标记。"""

        if len(description) <= max_chars:
            return description
        return description[: max(0, max_chars - 3)].rstrip() + "..."

    @staticmethod
    def _common_skill_root(skills: list[Skill]) -> str:
        """计算一组 SKILL.md 路径的公共根，用于 r0 路径别名降级。"""

        if not skills:
            return ""
        try:
            common = os.path.commonpath([str(skill.skill_file) for skill in skills])
        except ValueError:
            return ""
        path = Path(common)
        if path.is_file() or path.name == "SKILL.md":
            path = path.parent
        return str(path)
