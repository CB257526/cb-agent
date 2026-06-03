# TUI 消息窗口渲染防抖动 — 技术报告

## 背景

聊天消息积累到 50+ 条后（多轮工具调用、多条 thought chunk、assistant 回答），TUI 界面开始持续抖动。即使之前的自适应节流已经降低了 flush 频率，每次 `setItems` 仍然触发 React 对全量消息的调和 → Ink 重写全部 ANSI → 画面不稳。

## 根因

`EventStream` 的 `items.map()` 对每条 ChatItem 都创建 React 元素。当 items 数组增长，React 调和量线性增长：

```
items.length  | React 调和元素数 | flush 一次耗时
-------------|----------------|--------------
30           | 30             | ~2ms
80           | 80             | ~8ms
150          | 150            | ~15ms → 开始掉帧
```

每次流式 `text_delta` → `scheduleFlush` → `flushDelta` → `setItems` 都会重新映射全部消息。虽然 `React.memo` 让子组件跳过实际 DOM 更新，但父级的 `items.map()` 仍创建全量虚拟 DOM 元素。

## 修复方案

### 窗口渲染

`EventStream` 只渲染最近 `MAX_VISIBLE = 50` 条消息，超出部分折叠：

```typescript
const { visibleItems, hiddenCount } = useMemo(() => {
  if (items.length <= MAX_VISIBLE) return { visibleItems: items, hiddenCount: 0 };
  return {
    visibleItems: items.slice(items.length - MAX_VISIBLE),
    hiddenCount: items.length - MAX_VISIBLE,
  };
}, [items]);
```

React 调和量被**硬上界**为 50 条消息，与历史消息总量解耦。

### 交互切换（Ctrl+E）

- **默认**：折叠模式，最近 50 条可见
- **busy 期间**：强制折叠（无视用户选择），防止流式抖动
- **Ctrl+E**：空闲时切换展开/折叠全部消息
- **折叠提示行**：`... 以上 N 条消息已折叠（Ctrl+E 展开 / 再按折叠）`

### App.tsx 改动

- 新增 `showAllMessages` 状态（默认 `false`）
- 新增 `Ctrl+E` 键盘绑定
- 传 `showAll={showAllMessages}` 给 EventStream

### EventStream.tsx 改动

- 新增 `showAll?: boolean` prop
- `effectiveShowAll = !busy && showAll` — busy 期间强制折叠
- `useMemo` 依赖 `[items, effectiveShowAll]` 控制切片

## 文件改动

- `ui-tui/src/App.tsx`：+6 行（状态 + 键盘绑定 + prop 传递）
- `ui-tui/src/components/EventStream.tsx`：+53/-17 行（窗口渲染 + showAll 支持）
