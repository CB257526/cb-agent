# OTUI 前端：会话切换与浮层弹窗修复技术报告

## 背景

cb-agent 的 TUI 已从 Ink/React/Node 迁移到 OpenTUI + Solid.js + Bun（新目录 `ui-otui/`，旧 `ui-tui/` 暂留以备回退）。迁移完成后的多轮验收中，浮层 Select 弹窗（`/sessions` `/tools` `/mcp` 命令打开的居中小窗）暴露出三个递进的问题：

1. 会话列表弹窗只显示标题，选项列表空白。
2. 切换会话后（无论走弹窗还是 `/switch <id>`），对话区一直空白，不恢复该会话的历史聊天记录。
3. 在弹窗里选中会话回车，没有任何切换反应。

本报告记录这三个问题的根因与修复，重点在第 3 个——它是一个 Solid.js 响应式 props 与命令式副作用执行顺序耦合导致的隐蔽 bug。

## 问题一：弹窗列表高度塌缩

### 现象

弹窗弹出后只看到标题行，下方选项列表整片空白。

### 根因

OpenTUI 的 `SelectRenderable` 可见项数由 `maxVisibleItems = floor(height / linesPerItem)` 决定。最初实现把 `<select flexGrow={1}>` 放进 flex 列容器，期望它自动撑高。但在该布局下 flexGrow 算不出确定的 `height`，select 拿到的高度近似 0，`maxVisibleItems` 退化为 0，于是一条选项都画不出来，只剩标题。

### 修复

不依赖 flex 自动撑高，按选项数显式计算高度。`showDescription` 时每项占 2 行，最多展示 10 项（再多由 select 自身滚动）：

```ts
const LINES_PER_ITEM = 2;
const MAX_VISIBLE = 10;
const listHeight = createMemo(() => {
  const n = Math.min(options().length, MAX_VISIBLE);
  return Math.max(1, n) * LINES_PER_ITEM;
});
```

`<select height={listHeight()} ... />`，高度确定后列表正常渲染。

## 问题二：切换会话后不恢复历史

### 现象

`/sessions` 弹窗选中、`/switch <id>`、`/new` 都只在对话流打印一行“已切换到会话 …”提示，但对话区保持空白，不显示目标会话的历史消息。

### 根因

后端 `session.switch` / `session.create` 的 RPC 响应里带的是完整 `SessionPayload`：

```
{ session: SessionSummary | null, history: RestoredHistoryMessage[], context_window?: ContextWindow | null }
```

但前端三条命令的 handler 拿到 payload 后从没用 `history` 重绘对话流，只 `appendSystem` 了一行提示。历史数据到了前端却被丢弃。

### 修复

在 `SessionProvider` 内新增统一的 `applySessionPayload`，复用 gateway_ready 时已有的 `restoredHistoryToItems` 把后端 history 转成 ChatItem，并同步 session 摘要、上下文窗口，同时清掉上一会话残留的流式态（busy/round/todos/activeQuestionId），避免切换后还顶着旧动效：

```ts
const applySessionPayload = (payload: SessionPayload) => {
  setState("items", restoredHistoryToItems(payload.history ?? []));
  setState("session", payload.session ?? null);
  if (payload.context_window !== undefined)
    setState("contextWindow", payload.context_window ?? null);
  setState("busy", false);
  setState("round", 0);
  setState("todos", []);
  setState("activeQuestionId", null);
};
```

该函数通过 `CommandCtx` 注入，`/switch`、`/new`、`/sessions` 弹窗的 onSelect 全部改为调用它。

## 问题三：选中回车不切换（核心）

### 现象

弹窗里上下键能正常移动高亮，但在某个会话上回车，没有任何切换发生。

### 定位过程

仅凭读代码无法确定断点在哪一环（键没进来？取值错？回调没跑？后端报错？），所以加临时文件日志逐环打点。关键日志：

```
key: name=down ...
key: name=down ...
key: name=return raw="\r"
commit: index=2 picked={...value:"session_2026..._4a1bba68"} hasOnSelect=true
```

有 `commit` 行、`picked.value` 正确、`hasOnSelect=true`，但**始终没有 onSelect 内部那行 `[onSelect]` 日志**——证明 `onSelect` 回调根本没被调用。前端键盘链路（键进来、取值、判定）全部正常，问题就卡在“调用回调”这一步。

### 根因

`commit()` 当时的写法是先关弹窗、再调回调：

```ts
const commit = () => {
  const picked = props.spec.options[index()];
  closeDialog();                                    // 把 state.dialog 置为 null
  if (picked && typeof picked.value === "string")
    props.spec.onSelect?.(picked.value);            // 此刻 props.spec 已是 null
};
```

弹窗在 app.tsx 里以 `<SelectDialog spec={state.dialog!} />` 挂载。Solid.js 的组件 props 是**惰性 getter**，每次访问 `props.spec` 都会重新读取当前的 `state.dialog`，而不是组件创建时的快照。

`closeDialog()` 把 `state.dialog` 置为 null 后，紧接着的 `props.spec` 立刻求值为 null，`props.spec.onSelect?.(...)` 因此变成 `null?.()`——可选链遇到 null 静默短路，回调从不执行。日志里 `hasOnSelect=true` 之所以成立，是因为那行探针打在 `closeDialog()` 之前读取的，反而印证了“关窗前能读到、关窗后读不到”这一时序。

这是 Solid 响应式 props 与命令式副作用顺序耦合的典型陷阱：把响应式来源（`state.dialog`）当成了稳定引用，在销毁它之后还继续读它的字段。

### 修复

在销毁响应式来源之前，先把要用的值抓进局部变量（局部变量是普通快照，不随 `state.dialog` 变化），再关弹窗、再调回调：

```ts
const commit = () => {
  // 必须在 closeDialog() 之前抓出 onSelect 和 value：
  // props.spec 是 Solid 响应式 getter，读的是 state.dialog；
  // closeDialog() 置 null 后再读就成了 null?.()，回调被静默跳过。
  const picked = props.spec.options[index()];
  const onSelect = props.spec.onSelect;
  closeDialog();
  if (picked && typeof picked.value === "string") onSelect?.(picked.value);
};
```

## 附带改进：弹窗自管键盘，不依赖原生 select 焦点

定位问题三的过程中，把 SelectDialog 的键盘处理从“依赖原生 `<select>` 的 itemSelected 事件”改为“自己用 `useKeyboard` 接管 ↑/↓/回车/Esc，`<select>` 设 `focused={false}` 纯做受控展示”。

原因：OpenTUI 的 `SelectRenderable` 只有在被焦点系统聚焦时才会走 `handleKeyPress` 并 emit `itemSelected`。弹窗弹出时 Prompt 的 `<input>` 失焦后，焦点未必自动转交给 select，导致回车被吞、`itemSelected` 永不触发。仿 opencode 的做法由弹窗自身接管键盘，高亮项由我们维护的 `selectedIndex` 信号驱动，彻底绕开焦点转移的不确定性。

`useKeyboard` 底层挂在 `renderer.keyInput` 的 `"keypress"` 事件上（EventEmitter，多监听器都会收到），所以弹窗的监听器与 app 壳层的全局监听器可以共存，不互相吞键。

为什么不直接搬 opencode 的 `DialogSelect`：它 719 行，深度耦合 opencode 自有的 dialog 栈、keymap 引擎、config、scroll 工具、remeda/fuzzysort 等约 1200+ 行基础设施。cb-agent 只需要“居中小窗 + 上下选 + 回车确认”，自实现的轻量组件约 110 行已满足，且外观（边框窗/标题/描述/滚动条/底部提示）已对齐。这与迁移计划“只复刻 JSX 呈现、不整段照搬”的原则一致。

## 影响文件

- `ui-otui/src/components/SelectDialog.tsx`：高度显式计算、自管键盘、commit 先抓值后关窗。
- `ui-otui/src/context/session.tsx`：新增 `applySessionPayload`，导入 `SessionPayload`，注入 CommandCtx。
- `ui-otui/src/commands.ts`：`/switch` `/new` `/sessions` 改用 `applySessionPayload` 恢复历史；CommandCtx 增加 `applySessionPayload` 字段。

## 验证

- `tsc --noEmit` 通过。
- 实测：`/sessions` 打开弹窗，列表正常显示；↑/↓ 移动高亮；回车切换到目标会话，对话区恢复该会话历史聊天记录。
