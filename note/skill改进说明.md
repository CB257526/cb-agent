# 参考 Codex 实现 Skill 系统:详细设计文档

---

## 一、定位与目标

在动手之前,先明确要解决的问题域。一个 skill 系统的核心目标有四个:

1. **让 agent 知道有哪些 skill 可用** —— 必须在 system prompt 里给出"目录"
2. **让 agent 知道怎么用某个 skill** —— 必须能读到完整操作手册
3. **让用户和 agent 都能触发 skill** —— 既要显式触发(用户说要用),也要隐式触发(任务自然契合)
4. **控制上下文成本** —— skill 描述要轻量,正文按需加载

Codex 的设计哲学是"**指令式 + 本地优先 + 渐进披露**"。它把 SKILL.md 当成一份**给 agent 看的 markdown 操作手册**,而不是一个结构化函数。这是最重要的认知前提。

---

## 二、文件结构与命名约定

每个 skill 必须是一个**目录**,根下放一个固定文件 `SKILL.md`。这样设计的几个理由:

- **可扩展性**:同一目录下可以放脚本、模板、参考文档,SKILL.md 通过相对路径引用它们
- **可发现性**:目录名天然就是 skill 的默认名,不需要额外配置
- **可移植性**:整个目录可以打包、版本化、git 子模块化

推荐的目录布局:

- `SKILL.md` —— 必需,YAML frontmatter + markdown 正文
- `scripts/` —— 可选,供 agent 调用的脚本
- `references/` —— 可选,详细参考文档,SKILL.md 用相对路径引用
- `assets/` —— 可选,图标、模板

`SKILL.md` 这个文件名是**强约束**,不是 `skill.md` 也不是别的。这是发现阶段唯一识别的锚点。

---

## 三、SKILL.md 的两段式结构

SKILL.md 由两段组成:**YAML frontmatter** 和 **markdown 正文**。这两段承担完全不同的职责。

### 3.1 Frontmatter 段

只放**索引型元数据**。这些字段会在 system prompt 启动时全量渲染,必须满足:

- **可单行表达** —— `description` 必须能用一句话说清,这是 agent 决定是否触发 skill 的关键
- **机器可读** —— YAML 格式,严格校验
- **内容稳定** —— 不要写动态信息

只保留三个字段就够用:

- `name` —— skill 的标识符,kebab-case,带 namespace
- `description` —— 一句话描述,触发判断的核心
- `metadata.short-description` —— 极短描述,用于更紧凑的渲染场景

**故意不放**的字段:不暴露 `allowed_tools`、`arguments`、`model`、`version`、`license`、`paths`、`aliases`、`user_invocable` 等。这些字段要么让 prompt 膨胀,要么和 agent 行为耦合过紧,要么可以通过其他机制实现。

### 3.2 正文段

正文是**操作手册**,给 agent 读的。它的特点:

- **只在 skill 被触发后才加载** —— 不进启动 prompt
- **内容自由** —— 可以写代码块、引用相对路径、列步骤
- **应该自包含** —— agent 读到正文就应该能完成整个工作流
- **鼓励引用相对路径** —— `scripts/x.py`、`references/y.md`,这样整个 skill 目录可移植

正文里推荐用清晰的章节划分:**触发条件**、**前置依赖**、**核心步骤**、**失败处理**。agent 实际使用时会按章节执行,而不是全文通读。

### 3.3 YAML 容错

第三方 skill 经常写出 `description: Build for AWS: ECS` 这种带冒号的非法 YAML 字符串。Codex 内置了一层**frontmatter 修复器**:行级扫描,遇到键值后还有冒号就自动加引号。**强烈建议在仿写时也保留这层容错**,因为 skill 生态越开放,数据越脏。

---

---

## 五、Skill 发现:多层级优先级

Skill 应该被发现的地方很多,需要明确优先级。Codex 用了四层 + 插件注入,优先级从高到低:

2. **安装目录级** —— cbagent的安装目录的skill，这个层级通常与用户级是一个等级，所有项目都会进行加载`.cbagent/skills/`
3. **仓库级** —— 跟随项目走,通常在 `.agents/skills/`,只对当前项目生效,可以纳入版本控制
4. **用户级** —— 用户的全局 skill,在 `$HOME/.agents/skills/` ,跨项目生效

实现时要处理的关键问题:

- **路径解析**:不同层用不同的文件系统抽象(本地、远程、沙箱),统一用 URI 抽象
- **扫描深度限制**:递归遍历子目录,但要限制最大深度(Codex 用 6)和最大目录数(2000),防止误把整个硬盘扫了
- **符号链接策略**:用户/仓库/管理员层跟随目录符号链接,系统层忽略(防止恶意替换内置 skill)
- **同名冲突**:优先级高的覆盖低的。Codex 用的是 scope 排序:system → admin → repo → user,用户层最后但**实际优先级最低**(内部排序)

**项目根判定**:仓库层需要从 cwd 向上回溯找到项目根(用 `.git`、`pyproject.toml` 等 marker),从根到 cwd 的每一层都可能是 skill 所在。这样做是为了让多 workspace 项目能在每个子目录放独立 skill。

---

## 六、Skill 在 prompt 里的渲染:预算管理

Skill 元数据注入 system prompt 时,**绝对不能**把每个 skill 的完整 SKILL.md 灌进去。三个理由:

1. 启动 prompt 会随 skill 数量线性增长,污染所有对话
2. 大部分 skill 在 90% 的对话里都用不到
3. 用户感知不到这部分开销,但它在消耗 context window

Codex 的策略是**2% 预算**。具体实现逻辑:

1. **算预算** —— 拿到当前模型的 context window 大小,2% 作为 token 上限(回退到 8000 字符)
2. **第一遍渲染** —— 用绝对路径 + description 渲染所有 skill 的"目录行",格式是 `- {name}: {description} (file: {path})`
3. **超预算则压缩 description** —— 把每个 skill 的 description 按字符级贪心分配,长 description 被截断成 `...`
4. **还超预算则省略 description** —— 只显示 `- {name}: (file: {path})`
5. **再超预算则绝对路径改 alias** —— 提取公共根,用 `r0`、`r1` 别名代替,节省路径字符
6. **还超则完全省略** —— 只显示 skill 总数和警告

这套**五级降级**是核心,任何一层都不能省。降级时要给出"已被裁剪"的提示,让 agent 知道还有更多 skill 存在。

**关键约束**:渲染时只输出 `name + description + path`,**绝对不输出 SKILL.md 正文**。正文是另一阶段的事。

---

## 七、Skill 触发机制

Skill 触发分两种:**显式**和**隐式**。

### 7.1 显式触发

用户在消息中**点名**某个 skill,比如"用 $imagegen 帮我生成一张图"或者"`[imagegen]`"。提取规则:

- 识别 `$` 后跟标识符的语法
- 标识符允许字母、数字、下划线、连字符、冒号
- 还要识别 markdown 链接形式 `[$name](path)`
- 显式提及的 skill **强制加载**,无视任何"是否应该触发"的判断

显式触发的匹配策略:**先按路径匹配,再按 plain name 匹配**。同名 skill 多个时 plain name 必须唯一才能匹配。这避免歧义。

### 7.2 隐式触发

Agent **没有显式提到** skill,但任务语义匹配某个 skill 的 description。判定完全交给 LLM 自己:

- 在 system prompt 的"使用说明"里告诉 LLM:"如果任务明显匹配某个 skill 的 description,必须使用该 skill"
- LLM 自行决定
- 实现上只需要"暴露 skill 列表",判定逻辑在模型里

**坑**:隐式触发依赖 description 写得是否清楚。这是 skill 作者的责任,不是系统的责任。

### 7.3 隐式调用的事后识别(关键!)

有一种特殊"隐式触发":agent 读 SKILL.md 后,**自己用 bash 工具**运行了 skill 目录下的脚本。比如 `python /path/to/skill/scripts/x.py`。这种"调用"在 prompt 层面 agent 没说任何东西,但**实际上确实用了**。

Codex 的做法是**事后识别**:

1. 加载阶段,维护一个 `scripts_dir → skill` 索引
2. 每次 bash 工具执行,解析命令,识别"是否是 `python/bash/node/...` + 已知扩展名脚本"
3. 解析出脚本绝对路径,走祖先链查 `scripts_dir` 索引
4. 匹配到对应 skill 后,**只做记录**(遥测/分析),不重复触发

**为什么不在事前提供专用工具?** 三个理由:

1. **工具数量膨胀** —— 模型上下文里每个工具都消耗注意力预算
2. **执行能力无差异** —— skill 脚本本质就是普通脚本,加一层封装没有安全收益
3. **沙箱复用** —— 直接走通用 shell 工具

**这对仿写者的启示**:不要做 `run_skill_script` 这种专用工具。让 agent 用通用 shell,**把识别和记录做好**就行。

---

## 八、Skill 正文注入:渐进披露

Skill 触发后,需要把 SKILL.md 正文交给 agent。Codex 的设计:

- 正文被读出来后,包成 `<skill>...</skill>` 标签的 user message 片段
- 这个片段作为**用户消息的一部分**送入对话,而不是塞进 system prompt
- 这样符合"system 不变,user 提供上下文"的常规模式
- 缓存友好:正文是 user message 的一部分,前序对话的 system 部分保持稳定,减少 cache miss

**正文不包含 frontmatter**。frontmatter 已经在 system prompt 里出现过一次,正文只保留 markdown 主体,避免重复消耗 token。

### 8.1 引用解析

SKILL.md 正文里经常写 `scripts/x.py` 或 `references/y.md` 这类相对路径。Agent 读到时**自然能根据上下文推断根目录**(就是 `path_to_skills_md` 的父目录)。系统在 prompt 的"使用说明"里要明确告诉 agent 这个解析规则,不然 agent 容易把相对路径跑错。

### 8.2 嵌套引用

如果 SKILL.md 里说"读 `references/y.md`",agent 应该:

- 先读 SKILL.md 整体,理解工作流
- 再按需读 references 里的具体文件
- 读完后**自己**用读到的内容继续工作,**不要**把读到的内容丢给子 agent

最后一条很关键:**不要把 skill 指令的解读委托给子 agent**。子 agent 可以执行 task work,但不能"理解 skill",因为 skill 是给主 agent 看的操作手册。

---



---

## 十、缓存与变更感知

Skill 加载是 IO 密集型操作(遍历目录、读文件、解析 YAML)。要做缓存:

- 缓存键是 `cwd + config_layer_stack 的指纹 + plugin_skill_roots`
- 配置变化时才失效,普通对话轮次不重载
- 文件系统监听:某些环境需要响应 skill 目录变化(用户安装新 skill 后希望立即可用)。Codex 提供了 `skills_watcher` 模块做这件事

但要小心:**缓存的一致性责任在调用方**。如果允许运行时动态新增 skill,要么禁用缓存,要么做主动失效。

---

## 十一、可观测性

Skill 系统必须有遥测/分析,关键指标:

- **每个 skill 的注入次数**(显式/隐式分开)
- **被裁剪的 skill 数**(反映预算压力)
- **加载错误数**(YAML 错误、文件读取失败)
- **隐式调用命中数**(agent 跑了脚本但没显式触发)

这些指标有两个用途:**产品决策**(哪些 skill 有用)和**用户调试**(为什么我的 skill 没生效)。

---

---

## 十三、给仿写者的关键取舍建议

### 13.1 字段选择

不要照搬你最初列出的 17 个字段。**只实现必要的**:`name`、`description`、可选的 `short_description`、可选的扩展元数据 interface/dependencies/policy。其他字段(your list 里的 14 个)在本仓库**根本没实现**,不要凭空加。

### 13.2 文件夹还是单文件

**强制目录**。即使 skill 只有一个 `SKILL.md`,也要放在目录里。这是为了向后兼容(将来可以加 scripts/、references/、assets/ 而不破坏现有 skill 的发现逻辑)。

### 13.3 用 markdown 而非结构化指令

SKILL.md 的正文是 markdown,**不是 YAML/TOML/JSON**。原因是 agent 在读操作手册时,人类可读的 markdown 比结构化数据更自然,而且天然支持代码块、相对路径、表格、列表等丰富表达。

### 13.4 不要做专用执行工具

agent 用通用 bash 跑 skill 脚本就够了。做专用工具反而引入新的安全策略、新的工具注册、新的沙箱代码。这是反模式。

### 13.5 显式优先于智能

不要让系统做"是否应该触发 skill"的判断。Codex 的策略是:**只要 description 写得清楚,LLM 自己会判断**。判断责任在 skill 作者和 LLM,不在调度系统。

### 13.6 元数据预算宁严勿松

2% 的预算看似很小,但**充足**。一个 200k 上下文有 4000 token 装 skill 列表,够装几十个。如果发现不够,首先应该**优化 description 长度**,而不是扩大预算。

### 13.7 容错优先于严格

第三方 skill 内容质量参差。YAML 修复、失败 fail-open、缺字段用默认值 —— 这些**比严格校验更重要**。一个能容忍 80% 第三方内容的系统,远比一个只能加载 100% 严格内容的有用。

---

## 十四、最小可实现版本(MVP)

如果你想一个周末就搭起来,最小集是:

1. 一个 `Skill` 数据类,只有 name、description、path
2. `discover(cwd, codex_home) -> list[Skill]`,从固定几个目录找 `**/SKILL.md`
3. `parse_frontmatter(text) -> (meta, body)`,一个 30 行的 YAML 解析
4. `render(skills, budget_chars) -> str`,一个列表渲染 + 字符级裁剪
5. `collect_mentions(user_text, skills) -> list[Skill]`,正则提取 `$name`
6. `read_skill_body(skill) -> str`,简单的文件读

总代码量 200-300 行 Python 就能跑。然后再迭代加**预算降级、扩展元数据、隐式识别、缓存、遥测**。

---

## 十五、扩展方向

按 ROI 排序,后续可以加的功能:

1. **进度条/UI 集成** —— 展示当前可用的 skill 列表
2. **skill 安装/卸载工具** —— CLI 命令管理用户级 skill
3. **plugin 集成** —— 把 skill 作为 plugin 的一部分发布
4. **多产品适配** —— `policy.products` 已经预留接口
5. **远程 skill 源** —— 从 marketplace 下载 skill,通过 plugin 协议集成
6. **skill 验证工具** —— 静态检查 SKILL.md 是否符合规范、description 是否清晰
7. **MCP 依赖预解析** —— 在加载时验证依赖的 MCP server 是否可达,失败时给出明确错误

---

## 十六、总结:Codex Skill 系统的设计精髓

把整个调研压缩成一句话:**Codex 的 skill 系统是一个"分层文件结构 + 严格核心契约 + 宽松扩展元数据 + 预算化目录渲染 + 通用工具执行 + 事后遥测识别"的复合体**。

它没有花哨的工具框架,没有复杂的权限模型,没有专门的运行时。它靠的是**文件约定 + prompt 工程 + 现有工具复用**这三件套。仿写时记住这点,不要被自己最初想象的"完整 skill 字段集"误导 —— 那些字段很多根本不存在,存在的也很简单。

最关键的两个取舍:**正文按需加载(而不是全量预加载)** 和 **复用通用 shell 工具(而不是新做执行工具)**。这两点决定了整个系统的简洁性。