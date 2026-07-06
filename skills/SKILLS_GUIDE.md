# cb-agent Skills 系统指南

本文描述重构后的 Skill 系统。新的 Skill 不再是模型可调用工具，也不再有专门的脚本执行链；它是一份可发现、可按需读取的 Markdown 操作手册。

## 设计目标

- 启动 prompt 只放轻量目录：`name`、`description`、`SKILL.md` 路径。
- Skill 正文按需读取：显式 `$skill` 只注入当前轮，隐式使用由模型根据目录自行 `file_read`。
- 脚本统一走 `bash`：SkillManager 只在 bash 执行后记录脚本命中，绝不替 agent 执行脚本。
- 元数据保持克制：只消费 `name`、`description`、`metadata.short-description`。
- 兼容旧资源布局：继续发现根目录 `*.md` 参考文档和 `scripts/` 脚本。

## 目录结构

推荐结构：

```text
my-skill/
├── SKILL.md
├── references/
│   └── guide.md
├── scripts/
│   └── helper.py
└── assets/
    └── template.json
```

兼容结构：

```text
legacy-skill/
├── SKILL.md
├── forms.md
└── scripts/
    └── inspect.py
```

`references/*.md` 是新推荐位置；Skill 根目录下除 `SKILL.md` 外的 `*.md` 仍会作为参考文档暴露，方便旧内置 Skill 不迁移也可用。

## SKILL.md 格式

最小示例：

```markdown
---
name: pdf
description: Handle PDF inspection, extraction, form filling, and conversion tasks.
metadata:
  short-description: PDF workflows
---

# PDF Skill

When the user asks to inspect or modify a PDF, follow these steps...
```

当前只消费这些字段：

| 字段 | 用途 |
|---|---|
| `name` | Skill 正式名称。缺失时使用目录名兜底。 |
| `description` | overview 中给模型匹配任务的描述。 |
| `metadata.short-description` | `/skills` 等紧凑展示优先使用的短描述。 |

旧字段会被忽略，包括 `allowed_tools`、`arguments`、`aliases`、`paths`、`when_to_use`、`user_invocable`、`disable_model_invocation` 等。它们不会报错，但也不会影响发现、匹配或权限。

frontmatter 使用 `pyyaml` 解析。若遇到 `description: Build for AWS: ECS` 这类未加引号的冒号值，解析失败后会自动做一次保守加引号修复再重试。

## 发现顺序

默认扫描顺序从低优先级到高优先级：

1. 用户级：`$HOME/.agents/skills`
2. 仓库级：`.agents/skills`
3. 安装/项目级：`.cbagent/skills`

同名 Skill 后扫描者覆盖先扫描者。显式传入 `SkillManager(skills_dir=[...])` 时，完全按传入顺序覆盖。

扫描规则：

- 递归查找大写 `SKILL.md`。
- 最大深度为 6。
- 单个根最多扫描 2000 个目录。
- 找到一个 `SKILL.md` 后不再深入该 Skill 目录，避免把 `references/` 或 `scripts/` 误当嵌套 Skill。

## Prompt Overview

`SkillManager.build_skills_overview()` 生成注入 system prompt 的轻量目录，格式类似：

```text
<available-skills>
Skills are local markdown operating manuals...
- pdf: Handle PDF tasks (file: /path/to/.cbagent/skills/pdf/SKILL.md)
- skill-creator: Create or update skills (file: /path/to/.cbagent/skills/skill-creator/SKILL.md)
</available-skills>
```

overview 会告诉模型：

- 看到匹配 description 的任务时，读取列出的 `SKILL.md`。
- 相对路径从该 Skill 目录解析。
- bundled scripts 使用 `bash` 执行。
- 不存在 `skill` 或 `run_skill_script` 工具。

预算按上下文窗口约 2% 计算，并按五级降级：

1. 完整描述
2. 截断描述
3. 省略描述
4. 路径别名
5. 只提示数量和裁剪警告

## 显式触发

显式触发会把对应 `SKILL.md` 正文包装成 `<skill>...</skill>`，只追加到当前轮 LLM user content；历史里仍保存用户原文，避免长期膨胀。

支持形式：

```text
请用 $pdf 处理这个文件
请参考 [$pdf](/path/to/.cbagent/skills/pdf/SKILL.md)
```

规则：

- `$name` 先按正式名称精确匹配。
- 若正式名称不存在，再按 plain name 匹配 `namespace:name` 的最后一段。
- plain name 必须唯一，否则不匹配。
- Markdown 链接形式路径优先；路径解析失败后才用链接文字里的名称兜底。

用户入口仍保留：

- `/skills`
- `/skill NAME [args]`
- `/NAME [args]`
- `session.load_skill`

这些入口不再检查 `user_invocable`，因为该字段已经不参与新系统。

## 正文渲染

`Skill.render(args)` 只保留两个兼容替换：

| 占位符 | 替换值 |
|---|---|
| `$ARGUMENTS` | 显式加载时传入的参数字符串 |
| `${SKILL_DIR}` | Skill 目录绝对路径 |

正文返回值不包含 YAML frontmatter。`load_skill_content()` 会额外加上 source、references、script_files 清单，帮助模型正确定位资源。

## 参考文档

`Skill.get_reference_paths()` 会发现：

- `references/*.md`
- Skill 根目录下除 `SKILL.md` 外的 `*.md`

如果两处有同名 stem，后扫描的 `references/*.md` 会覆盖根目录同名文档。模型通常应直接用 `file_read` 读取 overview 或 `<skill-source>` 中列出的文件。

## 脚本执行

Skill 脚本必须通过 `bash` 工具执行，例如：

```text
python ${SKILL_DIR}/scripts/inspect.py /tmp/input.pdf
bash ${SKILL_DIR}/scripts/prepare.sh
node ${SKILL_DIR}/scripts/report.js
```

`BashTool` 可注入 `skill_observer=SkillManager`。bash 执行命令时，observer 会识别 `python`、`bash`、`sh`、`node` 等运行器后面的脚本路径；如果路径位于某个已安装 Skill 的 `scripts/` 目录下，就记录一次轻量命中。

记录行为：

- 不改变 bash stdout/stderr/exit_code 语义。
- 不触发二次加载 Skill。
- JSON 结果中可附带 `skill_script_hits`，用于测试和调试。
- subagent registry clone 出来的 `bash` 会继承同一个 observer。

## 主要 API

`Skill` 只保留轻量字段：

```python
Skill(
    name="pdf",
    description="...",
    body="...",
    skill_dir=Path("..."),
    short_description="PDF workflows",
)
```

常用方法：

| API | 说明 |
|---|---|
| `skill.render(args="")` | 渲染正文并替换 `$ARGUMENTS`、`${SKILL_DIR}`。 |
| `skill.get_reference_paths()` | 返回参考文档路径。 |
| `skill.get_script_paths()` | 返回 `scripts/` 下脚本路径。 |
| `manager.list_skills()` | 返回当前唯一 Skill 列表。 |
| `manager.get_skill(name)` | 按正式名称精确获取 Skill。 |
| `manager.resolve_mention(name)` | 解析 `$name`，支持唯一 plain name。 |
| `manager.resolve_path(path)` | 通过文件路径反查所属 Skill。 |
| `manager.collect_explicit_mentions(text)` | 从用户文本提取显式 Skill。 |
| `manager.build_skills_overview(max_chars=N)` | 构建预算内 prompt 目录。 |
| `manager.load_skill_content(name, args="")` | 加载并包装 Skill 正文。 |
| `manager.load_skill_reference(name, reference_name)` | 兼容入口，读取参考文档。 |
| `manager.record_script_hits(command, cwd=...)` | 识别并记录 bash 脚本命中。 |

## 已移除接口

这些接口在新架构中不存在：

- `skills.skill_executor.SkillExecutor`
- `tools.tools.skill_tool.SkillTool`
- `tools.tools.run_skill_script_tool.RunSkillScriptTool`
- 模型侧 `skill` 工具
- 模型侧 `run_skill_script` 工具
- `SkillManager.match_skill()`
- 基于 `paths`、`aliases`、`allowed_tools`、`user_invocable` 的旧匹配和权限行为

如果某个旧 Skill 正文里仍写着“调用 run_skill_script”，应改成“使用 bash 执行 scripts/ 下的脚本”。

## 创建新 Skill 检查表

- 创建目录 `.agents/skills/<name>/` 或 `.cbagent/skills/<name>/`。
- 创建 `SKILL.md`，只依赖 `name`、`description`、可选 `metadata.short-description`。
- 把长参考资料放到 `references/*.md`。
- 把可执行辅助脚本放到 `scripts/`。
- 在正文里明确说明脚本如何通过 `bash` 调用。
- 避免把大量正文塞进 `description`；description 用于发现，正文用于操作。
- 通过 `/skills` 或 `session.load_skill` 确认可发现。

## 测试建议

重点覆盖：

- frontmatter 冒号修复。
- 旧字段被忽略。
- 多根目录覆盖顺序。
- overview 预算降级。
- `$name` 和 `[$name](path)` 显式触发。
- Markdown 链接路径优先。
- 正文不含 frontmatter。
- `references/` 与根目录 `*.md` 兼容发现。
- 覆盖同名 Skill 后旧 `scripts/` 目录不再命中。
- bash `skill_script_hits` 只作为轻量调试 payload。
