# cb-agent Skills 系统设计与使用指南

## 目录

1. [设计理念](#1-设计理念)
2. [架构概览](#2-架构概览)
3. [核心概念](#3-核心概念)
4. [SKILL.md 文件格式](#4-skillmd-文件格式)
5. [目录结构规范](#5-目录结构规范)
6. [三级加载机制](#6-三级加载机制)
7. [变量替换系统](#7-变量替换系统)
8. [API 参考](#8-api-参考)
9. [工具集成](#9-工具集成)
10. [端到端使用示例](#10-端到端使用示例)
11. [创建自定义 Skill](#11-创建自定义-skill)
12. [设计决策与权衡](#12-设计决策与权衡)

---

## 1. 设计理念

### Prompt as Capability（提示词即能力）

cb-agent 的 Skills 系统采用 **"提示词即能力"** 的设计理念：

- **Skill 不是 Tool**：Tool 是一个原子化的函数调用（如搜索、读文件）；Skill 是一个完整的工作流，由提示词、参考文档和可执行脚本组合而成。
- **声明式定义**：Skill 用 Markdown + YAML frontmatter 声明，不需要写代码即可创建。
- **上下文注入**：Skill 的核心机制是将 Markdown 提示词内容注入到对话上下文中，让 LLM 按照 Skill 定义的流程执行任务。
- **渐进式加载**：采用三级加载策略，只在需要时加载完整内容，节省上下文窗口。

### 与 Tool 的区别

| 维度 | Tool | Skill |
|------|------|-------|
| 本质 | 函数调用 | 提示词指令 |
| 定义方式 | Python 代码继承 Tool ABC | Markdown + YAML frontmatter |
| 调用方式 | LLM 通过 function calling 执行 | LLM 读取指令后使用 Tool 执行 |
| 返回值 | 执行结果字符串 | 指令文本（LLM 据此行动） |
| 适用场景 | 原子操作（搜索、存储、计算） | 复杂工作流（PDF处理、代码审查） |

---

## 2. 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                     Skills 系统架构                          │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              Skill 来源层                            │    │
│  │  .cbagent/skills/                                   │    │
│  │  ├── pdf/SKILL.md                                   │    │
│  │  ├── skill-creator/SKILL.md                         │    │
│  │  └── my-custom-skill/SKILL.md                       │    │
│  └─────────────────────┬───────────────────────────────┘    │
│                        │                                    │
│                        ▼                                    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              SkillManager                           │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────────┐    │    │
│  │  │ 发现     │ │ 解析     │ │ 内容加载          │    │    │
│  │  │ Discovery│ │ Parsing  │ │ Content Loading   │    │    │
│  │  └──────────┘ └──────────┘ └──────────────────┘    │    │
│  └─────────────────────┬───────────────────────────────┘    │
│                        │                                    │
│       ┌────────────────┼────────────────┐                   │
│       ▼                ▼                ▼                   │
│  ┌──────────┐   ┌──────────────┐  ┌──────────────┐         │
│  │ L1 概览  │   │ L2 内容      │  │ L3 资源      │         │
│  │ 系统提示词│   │ SkillTool    │  │ 脚本/文档    │         │
│  │ 始终注入  │   │ 按需加载     │  │ 按需加载     │         │
│  └──────────┘   └──────────────┘  └──────────────┘         │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              工具层                                  │    │
│  │  SkillTool          RunSkillScriptTool              │    │
│  │  (调用 Skill)        (执行 Skill 脚本)               │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. 核心概念

### 3.1 Skill 的组成

一个 Skill 由以下部分组成：

```
my-skill/
├── SKILL.md          # 主文件（必需）：YAML frontmatter + Markdown 正文
├── reference.md      # 参考文档（可选）：补充说明
├── forms.md          # 参考文档（可选）：特定主题指南
├── scripts/          # 可执行脚本目录（可选）
│   ├── helper.py
│   └── processor.py
├── agents/           # 子 Agent 指令目录（可选）
│   └── reviewer.md
└── assets/           # 资源文件目录（可选）
    └── template.html
```

### 3.2 三级加载模型

| 级别 | 内容 | 加载时机 | 注入位置 |
|------|------|----------|----------|
| L1 | name + description + when_to_use | 每次请求 | 系统提示词 |
| L2 | SKILL.md 正文 + 参考文档 | LLM 调用 SkillTool | 工具返回值 |
| L3 | scripts/、agents/、assets/ | LLM 按需调用 | 工具执行 |

### 3.3 执行流程

```
用户: "帮我填写这个PDF表单 /tmp/app.pdf"
         │
         ▼
系统提示词中包含 L1 概览 (LLM 看到可用的 Skill 列表)
         │
         ▼
LLM 判断: 用户请求匹配 "pdf" Skill
         │
         ▼
LLM 调用: skill(name="pdf")
         │
         ▼
SkillTool 返回 SKILL.md 正文 (7595 字符)
  正文写着: "If you need to fill out a PDF form, read FORMS.md"
         │
         ▼
LLM 判断: 用户要填写表单，需要查看 FORMS.md
         │
         ▼
LLM 调用: skill(name="pdf", document="forms")
         │
         ▼
SkillTool 返回 forms.md 内容 (11854 字符)
         │
         ▼
LLM 按 forms.md 的指令执行:
  1. 调用 run_skill_script 检查 PDF 是否有可填写字段
  2. 分析表单结构
  3. 使用 pypdf/pdf-lib 填写表单
```

---

## 4. SKILL.md 文件格式

### 4.1 基本结构

```markdown
---
name: my-skill
description: 一行描述这个 Skill 做什么
when_to_use: 当用户要求做 X、Y、Z 时使用此 Skill
---

# Skill 标题

这里是 Skill 的正文内容，LLM 读取后会按照这些指令执行任务。

## 步骤

1. 首先分析用户需求
2. 然后执行操作
3. 最后验证结果
```

### 4.2 Frontmatter 字段详解

#### 必需字段

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `name` | string | Skill 标识符，kebab-case，最长 64 字符 | `pdf`, `code-review` |
| `description` | string | 一行描述，最长 1024 字符，用于 L1 列表展示 | `处理PDF文件的各类操作` |

#### 可选字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `when_to_use` | string | 空 | 详细的触发条件描述，帮助 LLM 判断何时调用 |
| `allowed-tools` | list | null | 工具权限白名单，如 `["Read", "Write", "Bash"]` |
| `arguments` | list | null | 声明的参数名，用于 `$arg` 变量替换 |
| `argument-hint` | string | null | 参数提示文本，如 `"[file path]"` |
| `model` | string | null | 模型覆盖，如 `"haiku"`, `"sonnet"` |
| `user-invocable` | bool | true | 用户是否可通过 `/name` 调用 |
| `disable-model-invocation` | bool | false | 禁止 LLM 自动调用 |
| `license` | string | null | 许可证信息 |
| `compatibility` | string | null | 依赖或兼容性说明 |

#### 字段格式示例

```yaml
---
name: pdf
description: 处理PDF文件的各类操作
when_to_use: >
  当用户提到PDF文件、需要提取PDF文本、合并/拆分PDF、
  填写PDF表单、或进行PDF OCR时使用此Skill
allowed-tools:
  - Read
  - Write
  - Bash
arguments:
  - filename
  - format
argument-hint: "[filename] [--format=csv|json]"
model: sonnet
user-invocable: true
disable-model-invocation: false
license: MIT
---
```

### 4.3 Frontmatter 解析规则

1. **分隔符**：以 `---` 开头和结尾
2. **键值对**：`key: value` 格式，冒号后需有空格
3. **列表**：以 `- ` 开头的行，归属最近的 key
4. **长文本**：续行自动拼接到前一个 key 的值
5. **引号**：值可以用 `"` 或 `'` 包裹，会被自动去除

```yaml
# 键值对
name: pdf

# 列表
arguments:
  - filename
  - format

# 长文本（续行拼接）
description: This is a very long description that
  spans multiple lines and will be joined together
```

---

## 5. 目录结构规范

### 5.1 最简 Skill

```
.cbagent/skills/
└── my-skill/
    └── SKILL.md
```

### 5.2 带参考文档

```
.cbagent/skills/
└── pdf/
    ├── SKILL.md
    ├── forms.md          # PDF 表单填写指南
    └── reference.md      # 高级 PDF 处理参考
```

### 5.3 带可执行脚本

```
.cbagent/skills/
└── pdf/
    ├── SKILL.md
    ├── reference.md
    └── scripts/
        ├── check_fillable_fields.py
        ├── fill_pdf_form.py
        └── extract_tables.py
```

### 5.4 完整 Skill

```
.cbagent/skills/
└── skill-creator/
    ├── SKILL.md
    ├── agents/
    │   ├── grader.md
    │   └── comparator.md
    ├── scripts/
    │   ├── run_eval.py
    │   ├── improve_description.py
    │   └── package_skill.py
    ├── references/
    │   └── schemas.md
    └── assets/
        └── eval_review.html
```

---

## 6. 三级加载机制

### 6.1 L1: 概览（始终注入）

L1 内容在每次请求时注入系统提示词，让 LLM 知道有哪些 Skill 可用。

**生成方式**：`SkillManager.build_skills_overview()`

**输出格式**：

```
<available-skills>
以下 Skill 可通过 Skill 工具调用：

- pdf: Use this skill whenever the user wants to do anything with PDF files...
- skill-creator: Create new skills, modify and improve existing skills...

当用户请求匹配某个 Skill 的使用场景时，使用 Skill 工具调用对应的 Skill。
</available-skills>
```

**注入位置**：系统提示词的末尾

### 6.2 L2: 正文（按需加载）

当 LLM 调用 `SkillTool` 时，**只返回 SKILL.md 正文**（经过变量替换），不包含参考文档。

**生成方式**：`SkillManager.load_skill_content(name, args)`

**设计理由**：SKILL.md 正文中通常会指引 LLM 按需读取特定参考文档（如 "如需高级功能请参阅 REFERENCE.md"、"如需填写表单请阅读 FORMS.md"）。LLM 阅读正文后自行判断是否需要加载参考文档，避免一次性加载全部内容浪费 token。

**返回格式**：

```
## Skill: pdf

# PDF Processing Guide

## Overview
This guide covers essential PDF processing operations...
For advanced features, see REFERENCE.md. If you need to fill out a PDF form, read FORMS.md.

[可用参考文档: forms, reference — 如需查看，调用 Skill 工具并指定 document 参数]
```

### 6.3 参考文档（按需加载）

当 LLM 判断需要查看某个参考文档时，通过 SkillTool 的 `document` 参数加载。

**调用方式**：`skill(name="pdf", document="forms")`

**生成方式**：`SkillManager.load_skill_reference(name, reference_name)`

**返回内容**：指定的单个参考文档的完整内容。

### 6.4 L3: 资源（按需加载）

| 资源类型 | 访问方式 | 说明 |
|----------|----------|------|
| scripts/*.py | `RunSkillScriptTool` | 通过子进程执行 |
| agents/*.md | `Skill.get_agents()` | 子 Agent 指令 |
| assets/* | `Skill.skill_dir / "assets"` | 文件路径引用 |

### 6.5 加载流程示例

```
用户: "帮我填写这个PDF表单 /tmp/app.pdf"
  │
  ▼
LLM 看到 L1 概览，判断匹配 "pdf" Skill
  │
  ▼
LLM 调用: skill(name="pdf")
  │         返回 SKILL.md 正文 (7595 字符)
  │         正文写着: "If you need to fill out a PDF form, read FORMS.md"
  │
  ▼
LLM 判断需要表单填写指南
  │
  ▼
LLM 调用: skill(name="pdf", document="forms")
  │         返回 forms.md (11854 字符)
  │
  ▼
LLM 按 forms.md 的指令，调用 run_skill_script 执行脚本
```

**Token 对比**：
- 旧方案（一次性加载）：44156 字符
- 新方案（按需加载）：7595 + 11854 = 19449 字符（节省 56%）

---

## 7. 变量替换系统

### 7.1 支持的变量

| 变量 | 替换为 | 示例 |
|------|--------|------|
| `$arg_name` | 声明的参数值 | `$filename` -> `test.pdf` |
| `$ARGUMENTS` | 所有参数的原始字符串 | `$ARGUMENTS` -> `--filename=test.pdf --format=json` |
| `${SKILL_DIR}` | Skill 目录的绝对路径 | `${SKILL_DIR}` -> `/path/to/.cbagent/skills/pdf` |

### 7.2 参数解析

参数字符串支持两种格式：

#### key=value 格式

```python
args = '--filename="test.pdf" --format="json"'
# 解析结果: {"filename": "test.pdf", "format": "json"}
```

支持的格式：
- `--key=value`
- `--key="value with spaces"`
- `--key='value with spaces'`
- `--key=value_without_quotes`

#### 原始字符串

如果参数中没有 `--key=value` 格式，整个字符串作为 `$ARGUMENTS` 的值。

### 7.3 SKILL.md 中使用变量

```markdown
---
name: process-file
description: 处理指定文件
arguments:
  - filename
  - format
---

请处理文件: $filename

输出格式: $format

所有参数: $ARGUMENTS

脚本位于: ${SKILL_DIR}/scripts/process.py
```

调用时：

```python
skill.render(args='--filename="data.csv" --format="json"')
# 输出:
# 请处理文件: data.csv
# 输出格式: json
# 所有参数: --filename="data.csv" --format="json"
# 脚本位于: /path/to/skills/process-file/scripts/process.py
```

---

## 8. API 参考

### 8.1 Skill 类

```python
from skills import Skill

# 属性
skill.name                    # str: Skill 名称
skill.description             # str: 一行描述
skill.when_to_use             # str: 详细触发条件
skill.body                    # str: SKILL.md 正文
skill.skill_dir               # Path: Skill 目录路径
skill.allowed_tools           # list|None: 工具白名单
skill.arguments               # list|None: 声明的参数
skill.model                   # str|None: 模型覆盖
skill.user_invocable          # bool: 用户可否调用
skill.disable_model_invocation # bool: 禁止 LLM 调用

# 方法
skill.to_metadata_string()    # -> str: L1 表示
skill.render(args="")         # -> str: 渲染正文（变量替换）
skill.get_references()        # -> dict[str, str]: 参考文档
skill.get_scripts()           # -> dict[str, Path]: 脚本路径
skill.get_agents()            # -> dict[str, str]: 子 Agent 指令
```

### 8.2 SkillManager 类

```python
from skills import SkillManager
from pathlib import Path

# 初始化（默认扫描 .cbagent/skills/）
manager = SkillManager()

# 指定目录
manager = SkillManager(skills_dir=Path("/path/to/skills"))

# 发现与查询
manager.list_skills()                     # -> list[Skill]: 所有 Skill
manager.get_skill("pdf")                  # -> Skill|None: 按名称获取

# 内容生成
manager.build_skills_overview()           # -> str: L1 概览
manager.load_skill_content("pdf")         # -> str: L2 正文（不含参考文档）
manager.load_skill_content("pdf", args="file.pdf")  # -> str: 带参数的 L2 正文

# 参考文档加载
manager.load_skill_reference("pdf", "forms")       # -> str: 单个参考文档内容
manager.load_skill_reference("pdf", "reference")   # -> str: 单个参考文档内容

# 匹配（降级方案）
manager.match_skill("帮我处理PDF")        # -> str|None: 匹配的 Skill 名称
```

### 8.3 SkillExecutor 类

```python
from skills import SkillExecutor
from pathlib import Path

# 初始化
executor = SkillExecutor(timeout=60)  # 超时时间（秒）

# 执行脚本
executor.run_script(
    script_path=Path("scripts/process.py"),
    args=["--input=file.pdf"],
    stdin_data=None
)

# 带上下文执行（JSON 通过 stdin 传入）
executor.run_script_with_context(
    script_path=Path("scripts/process.py"),
    context={"filename": "test.pdf", "format": "json"},
    args=["--verbose"]
)
```

---

## 9. 工具集成

### 9.1 SkillTool

让 LLM 通过 function calling 调用 Skill。

```python
from tools.tools.skill_tool import SkillTool
from skills import SkillManager

manager = SkillManager()
tool = SkillTool(manager)

# 注册到 ToolRegistry
registry.register_tool(tool)
```

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `skill` | string | 是 | Skill 名称 |
| `args` | string | 否 | 传给 Skill 的参数 |
| `document` | string | 否 | 要加载的参考文档名称（不含 .md）。省略则加载 SKILL.md 正文 |

**调用示例**：

```python
# 加载 SKILL.md 正文
tool.run({"skill": "pdf"})

# 加载指定参考文档
tool.run({"skill": "pdf", "document": "forms"})

# 带参数加载
tool.run({"skill": "pdf", "args": "--filename=report.pdf"})
```

**返回值**：SKILL.md 正文（不含参考文档），或指定的参考文档内容

### 9.2 RunSkillScriptTool

让 LLM 执行 Skill 捆绑的 Python 脚本。

```python
from tools.tools.run_skill_script_tool import RunSkillScriptTool
from skills import SkillManager, SkillExecutor

manager = SkillManager()
executor = SkillExecutor()
tool = RunSkillScriptTool(manager, executor)

# 注册到 ToolRegistry
registry.register_tool(tool)
```

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `skill_name` | string | 是 | Skill 名称 |
| `script_name` | string | 是 | 脚本名称（不含 .py） |
| `args` | array | 否 | 命令行参数 |
| `stdin_data` | string | 否 | stdin 数据 |

---

## 10. 端到端使用示例

### 10.1 基本使用

```python
from skills import SkillManager, SkillExecutor
from tools.tools.skill_tool import SkillTool
from tools.tools.run_skill_script_tool import RunSkillScriptTool
from tools.toolRegistry import ToolRegistry

# 1. 初始化
manager = SkillManager()
executor = SkillExecutor()

# 2. 注册工具
registry = ToolRegistry()
registry.register_tool(SkillTool(manager))
registry.register_tool(RunSkillScriptTool(manager, executor))

# 3. 构建系统提示词
base_prompt = "你是一个有用的助手。"
system_prompt = base_prompt + "\n\n" + manager.build_skills_overview()

# 4. 调用 LLM
from agent.cb_agents import CbAgentsLLM
llm = CbAgentsLLM()

messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": "帮我处理这个PDF文件 /tmp/app.pdf"}
]

[msg, tool_calls] = llm.think(
    messages,
    tools=registry.get_tools_description_openai_schema()
)
```

### 10.2 完整 Agent 循环

```python
from skills import SkillManager, SkillExecutor
from tools.tools.skill_tool import SkillTool
from tools.tools.run_skill_script_tool import RunSkillScriptTool
from tools.tools.search import SearchTool
from tools.toolRegistry import ToolRegistry
from agent.cb_agents import CbAgentsLLM

# 初始化
manager = SkillManager()
executor = SkillExecutor()
registry = ToolRegistry()
registry.register_tool(SearchTool())
registry.register_tool(SkillTool(manager))
registry.register_tool(RunSkillScriptTool(manager, executor))
llm = CbAgentsLLM()

# 构建系统提示词
system_prompt = "你是一个有用的助手。\n\n" + manager.build_skills_overview()

# Agent 循环
messages = [{"role": "system", "content": system_prompt}]

while True:
    user_input = input("用户: ")
    if user_input.lower() in ("exit", "quit"):
        break

    messages.append({"role": "user", "content": user_input})

    # 调用 LLM
    [msg, tool_calls] = llm.think(
        messages,
        tools=registry.get_tools_description_openai_schema()
    )

    # 处理工具调用
    if tool_calls:
        for tc in tool_calls:
            result = registry.execute_tool(tc["name"], tc["arguments"])
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result
            })

        # LLM 处理工具结果
        [msg, tool_calls] = llm.think(
            messages,
            tools=registry.get_tools_description_openai_schema()
        )

    # 输出回复
    print(f"助手: {msg}")
    messages.append({"role": "assistant", "content": msg})
```

### 10.3 手动加载 Skill

```python
from skills import SkillManager

manager = SkillManager()

# 获取 pdf skill 的完整内容
content = manager.load_skill_content("pdf")
print(content)

# 带参数加载
content = manager.load_skill_content("pdf", args="--filename=report.pdf")
print(content)

# 获取 Skill 对象
skill = manager.get_skill("pdf")

# 访问资源
refs = skill.get_references()     # {"forms": "...", "reference": "..."}
scripts = skill.get_scripts()     # {"check_fillable_fields": Path(...)}
agents = skill.get_agents()       # {"grader": "..."}
```

---

## 11. 创建自定义 Skill

### 11.1 最简示例

创建文件 `.cbagent/skills/hello/SKILL.md`：

```markdown
---
name: hello
description: 向用户打招呼并提供帮助
when_to_use: 当用户说"你好"、"hello"或初次见面时
---

# 打招呼

请用中文向用户问好，语气友好热情。

然后询问有什么可以帮助的，并简要介绍你的能力。
```

### 11.2 带参数的 Skill

创建文件 `.cbagent/skills/code-review/SKILL.md`：

```markdown
---
name: code-review
description: 代码审查工具
when_to_use: 当用户要求审查代码、进行 code review 时
arguments:
  - target
  - focus
argument-hint: "[file or directory] [--focus=security|performance|style]"
allowed-tools:
  - Read
  - Glob
  - Grep
---

# 代码审查

请对以下目标进行代码审查: $target

审查重点: $focus

## 审查步骤

1. 使用 Glob 工具找到相关文件
2. 使用 Read 工具读取代码内容
3. 使用 Grep 工具搜索潜在问题
4. 按以下维度进行审查:
   - 代码风格一致性
   - 潜在的 bug
   - 性能问题
   - 安全漏洞
5. 输出审查报告，包含:
   - 问题列表（按严重程度排序）
   - 修复建议
   - 总体评价

如需执行静态分析脚本，使用 run_skill_script 工具:
- skill_name: code-review
- script_name: static_analysis
```

### 11.3 带脚本的 Skill

创建目录结构：

```
.cbagent/skills/pdf-processor/
├── SKILL.md
├── reference.md
└── scripts/
    ├── extract_text.py
    └── merge_pdfs.py
```

SKILL.md:

```markdown
---
name: pdf-processor
description: PDF文件处理工具
when_to_use: 当用户需要处理PDF文件时
arguments:
  - filename
---

# PDF 处理器

请处理文件: $filename

## 可用脚本

使用 run_skill_script 工具执行以下脚本:

1. **extract_text** - 提取PDF文本
   ```
   skill_name: pdf-processor
   script_name: extract_text
   args: [$filename]
   ```

2. **merge_pdfs** - 合并多个PDF
   ```
   skill_name: pdf-processor
   script_name: merge_pdfs
   args: [file1.pdf, file2.pdf, --output=merged.pdf]
   ```

详细用法参考 ${SKILL_DIR}/reference.md
```

scripts/extract_text.py:

```python
"""提取 PDF 文本"""
import sys
from pypdf import PdfReader

def main():
    if len(sys.argv) < 2:
        print("用法: python extract_text.py <pdf_file>")
        sys.exit(1)

    filename = sys.argv[1]
    reader = PdfReader(filename)

    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        print(f"=== Page {i+1} ===")
        print(text)
        print()

if __name__ == "__main__":
    main()
```

### 11.4 Skill 创建清单

- [ ] 创建目录 `.cbagent/skills/<skill-name>/`
- [ ] 编写 `SKILL.md`，包含必需的 `name` 和 `description`
- [ ] 添加 `when_to_use` 以提高触发准确率
- [ ] 如果需要参数，声明 `arguments` 并在正文中使用 `$arg_name`
- [ ] 如果需要执行脚本，创建 `scripts/` 目录
- [ ] 如果需要补充文档，创建参考 *.md 文件
- [ ] 运行测试验证 Skill 能被正确发现和加载

---

## 12. 设计决策与权衡

### 12.1 为什么用 SkillTool 而不是被动注入？

**方案对比**：

| 方案 | 优点 | 缺点 |
|------|------|------|
| 关键词匹配 + 被动注入 | 无额外 API 调用 | 匹配不准确，浪费上下文 |
| LLM 匹配 + 被动注入 | 匹配准确 | 多一次 API 调用 |
| **SkillTool（采用）** | LLM 自主判断，准确 | 无额外缺点 |

**结论**：SkillTool 让 LLM 根据 L1 概览自主判断是否需要调用某个 Skill，比任何自动匹配算法都更准确，且与 function-calling 架构天然契合。

### 12.2 为什么不引入 PyYAML？

Frontmatter 格式只有 6 个已知字段，且都是简单的 key-value 或列表。逐行解析足以满足需求，避免引入额外依赖。

### 12.3 三级加载的必要性

- **L1 必须始终注入**：否则 LLM 不知道有哪些 Skill 可用
- **L2 按需加载**：一个 Skill 的完整内容可能有数百行，全部注入会浪费上下文
- **L3 资源独立**：脚本通过工具执行，文档按需读取

### 12.4 变量替换 vs 模板引擎

使用简单的字符串替换而非 Jinja2 等模板引擎：

- Skill 正文是给 LLM 看的，不需要复杂逻辑
- 减少依赖
- 简单可预测

### 12.5 降级匹配保留

`match_skill()` 方法保留作为无 function-calling 场景的降级方案：

- 某些模型不支持 function calling
- 某些场景需要预判 Skill 以优化提示词构建
- 关键词匹配零成本，可作为快速预筛选
