import { describe, expect, test } from "bun:test";
import { EventEmitter } from "node:events";
import { TextBufferRenderable, type BaseRenderable } from "@opentui/core";
import { createComponent } from "solid-js";
import type { Transport } from "../transport.js";
import type { ChatItem, PermissionMode, PlanMode } from "../types.js";
import { theme } from "../theme.js";

class FakeTransport extends EventEmitter {
  readonly stderrLogFile = "/tmp/cb-agent-test.log";
  private promptSequence = 0;

  close() {}
  cancel() {}
  quit() {}

  sendPrompt(): string {
    this.promptSequence += 1;
    return `prompt-${this.promptSequence}`;
  }

  answerQuestion() {}

  setMode(mode: PlanMode) {
    return Promise.resolve({ plan_state: { mode, status: "idle" } });
  }

  setPermissionMode(permissionMode: PermissionMode) {
    return Promise.resolve({ permission_mode: permissionMode });
  }

  listTools() {
    return Promise.resolve({
      tools: [
        { name: "bash", description: "在工作目录执行命令" },
        { name: "file_read", description: "读取文本文件内容" },
        { name: "todo_write", description: "更新任务清单" },
      ],
    });
  }

  listSkills() {
    return Promise.resolve({
      skills: [
        { name: "pdf", description: "处理 PDF 文件" },
        { name: "docx", description: "处理 Word 文档" },
      ],
    });
  }

  loadSkill(name: string) {
    return Promise.resolve({
      name,
      content: `skill body for ${name}`,
    });
  }
}

function normalizedFrame(frame: string): string {
  return frame
    .split("\n")
    .map((line) => line.trimEnd())
    .join("\n")
    .trimEnd();
}

function collectTextBuffers(node: BaseRenderable, result: TextBufferRenderable[] = []): TextBufferRenderable[] {
  if (node instanceof TextBufferRenderable) result.push(node);
  for (const child of node.getChildren()) collectTextBuffers(child, result);
  return result;
}

async function waitForHighlighting(node: { getChildren(): unknown[] }): Promise<void> {
  const promises: Promise<void>[] = [];
  if ("highlightingDone" in node) {
    const highlightingDone = (node as { highlightingDone?: unknown }).highlightingDone;
    if (highlightingDone instanceof Promise) promises.push(highlightingDone);
  }
  for (const child of node.getChildren()) {
    if (!child || typeof child !== "object" || !("getChildren" in child)) continue;
    promises.push(waitForHighlighting(child as typeof node));
  }
  await Promise.all(promises);
}

function gatewayReady(history = true) {
  return {
    type: "gateway_ready",
    model: "gpt-5.4",
    permission_mode: "request_approval",
    session: {
      session_id: "session_20260717_143003_76e0de48",
      active_task: "重构 OTUI 界面",
    },
    context_window: {
      used_tokens: 24500,
      max_tokens: 100000,
      full_window_tokens: 100000,
      percent: 24.5,
      source: "provider",
    },
    usage: {
      prompt_tokens: 12800,
      completion_tokens: 3200,
      cached_prompt_tokens: 6400,
      requests: 4,
    },
    plan_state: { mode: "execute", status: "idle" },
    history: history
      ? [
          { role: "user", content: "检查当前界面的信息层级。" },
          { role: "assistant", content: "我会先检查主题、消息流和输入区。" },
        ]
      : [],
  };
}

async function renderApp(width: number, height: number, history = true) {
  // 动态导入确保 OpenTUI 的 Solid 编译插件已由 bunfig preload 注册。
  const [{ testRender }, { App }] = await Promise.all([
    import("@opentui/solid"),
    import("../app.js"),
  ]);
  const transport = new FakeTransport();
  const setup = await testRender(
    () => createComponent(App, { transport: transport as unknown as Transport }),
    { width, height, useMouse: true },
  );
  transport.emit("event", gatewayReady(history));
  transport.emit("event", {
    type: "mcp_status",
    status: "ready",
    total: 2,
    connected: 2,
    failed: 0,
    servers: [],
  });
  await setup.flush();
  await setup.waitForVisualIdle();
  await waitForHighlighting(setup.renderer.root);
  await setup.flush();
  return { transport, setup };
}

describe("OTUI 视觉帧", () => {
  test("鼠标选区覆盖默认文本并使用可见背景", async () => {
    const { setup } = await renderApp(80, 24);
    const buffers = collectTextBuffers(setup.renderer.root);
    expect(buffers.length).toBeGreaterThan(0);
    expect(buffers.every((buffer) => buffer.selectionBg?.slot === theme.selectionBackground.slot)).toBe(true);
    expect(buffers.every((buffer) => buffer.selectionFg?.slot === theme.selectionForeground.slot)).toBe(true);

    await setup.mockMouse.drag(2, 1, 20, 1);
    await setup.flush();
    const selectedText = setup.renderer.getSelection()?.getSelectedText?.() ?? "";
    expect(selectedText).toContain("cb-agent");
    const selectedSpans = setup
      .captureSpans()
      .lines
      .flatMap((line) => line.spans)
      .filter((span) => span.bg.slot === theme.selectionBackground.slot);
    expect(selectedSpans.length).toBeGreaterThan(0);
    setup.renderer.destroy();
  });

  test("120 列展示完整状态信息", async () => {
    const { setup } = await renderApp(120, 36);
    expect(normalizedFrame(setup.captureCharFrame())).toMatchSnapshot();
    setup.renderer.destroy();
  });

  test("80 列折叠会话和 Token 明细", async () => {
    const { setup } = await renderApp(80, 24);
    expect(normalizedFrame(setup.captureCharFrame())).toMatchSnapshot();
    setup.renderer.destroy();
  });

  test("52 列不出现固定侧栏或横向挤压", async () => {
    const { setup } = await renderApp(52, 18);
    const frame = normalizedFrame(setup.captureCharFrame());
    expect(frame).not.toContain("MCP\n");
    expect(frame).not.toContain("OTUI");
    expect(frame).toMatchSnapshot();
    setup.renderer.destroy();
  });

  test("工具、计划、Todo 和子 Agent 保持树状层级", async () => {
    const { transport, setup } = await renderApp(120, 42, false);
    transport.emit("event", { type: "reasoning_delta", delta: "先定位主题和布局入口。" });
    transport.emit("event", {
      type: "tool_start",
      call_id: "call-1",
      name: "file_read",
      arguments: { path: "ui-otui/src/app.tsx" },
    });
    transport.emit("event", {
      type: "tool_complete",
      call_id: "call-1",
      name: "file_read",
      result: "读取完成",
      duration_seconds: 0.18,
      is_error: false,
    });
    transport.emit("event", {
      type: "tool_start",
      call_id: "call-2",
      name: "bash",
      arguments: { command: "bun test" },
    });
    transport.emit("event", {
      type: "tool_complete",
      call_id: "call-2",
      name: "bash",
      result: "测试失败",
      duration_seconds: 0.31,
      is_error: true,
    });
    transport.emit("event", {
      type: "todo_list_updated",
      items: [
        { content: "重构主题", status: "completed" },
        { content: "验证窄屏", status: "in_progress" },
      ],
    });
    transport.emit("event", {
      type: "subagent_started",
      task_id: "task-1",
      subagent_id: "agent-1",
      subagent_type: "explore",
      description: "检查外部 TUI 参考",
      status: "running",
      phase: "starting",
    });
    transport.emit("event", {
      type: "subagent_completed",
      task_id: "task-1",
      subagent_id: "agent-1",
      subagent_type: "explore",
      description: "检查外部 TUI 参考",
      content: "Codex 主要依赖默认前景色和弱化文本。",
      status: "completed",
      rounds_used: 1,
      duration_seconds: 1.2,
    });
    transport.emit("event", {
      type: "plan_ready",
      plan: "1. 重构主题\n2. 移除侧栏\n3. 验证响应式布局",
      plan_state: { mode: "plan", status: "pending", pending_revision: 2 },
    });
    await setup.flush();
    await setup.waitForVisualIdle();
    await waitForHighlighting(setup.renderer.root);
    await setup.flush();
    expect(normalizedFrame(setup.captureCharFrame())).toMatchSnapshot();
    setup.renderer.destroy();
  });

  test("展开工具使用树状输入输出和差异", async () => {
    const [{ testRender }, { ThemeProvider }, { ToolBlock }] = await Promise.all([
      import("@opentui/solid"),
      import("../context/theme.js"),
      import("../components/ToolBlock.js"),
    ]);
    const item: ChatItem = {
      id: "tool-expanded",
      role: "tool",
      text: "",
      toolName: "apply_patch",
      toolArgs: { patch: "更新主题" },
      toolResult: JSON.stringify({
        __display__: "修改成功",
        diff: "--- a/theme.ts\n+++ b/theme.ts\n@@ -1,1 +1,1 @@\n-old\n+new",
      }),
      toolDone: true,
      toolDuration: 0.42,
      toolError: false,
      collapsed: false,
    };
    const setup = await testRender(
      () => createComponent(ThemeProvider, {
        get children() {
          return createComponent(ToolBlock, { item });
        },
      }),
      { width: 80, height: 18 },
    );
    await setup.flush();
    await setup.waitForVisualIdle();
    expect(normalizedFrame(setup.captureCharFrame())).toMatchSnapshot();
    setup.renderer.destroy();
  });

  test("活动问询停靠在输入框上方", async () => {
    const { transport, setup } = await renderApp(80, 24, false);
    transport.emit("event", {
      type: "ask_user_question",
      question_id: "question-1",
      question: "选择本次重构的视觉方向",
      options: [
        { label: "Codex 优先", description: "终端原生、克制配色" },
        { label: "OpenCode 优先", description: "保留主题化面板" },
      ],
      recommended_index: 0,
      multi_select: false,
      allow_other: true,
    });
    await setup.flush();
    await setup.waitForVisualIdle();
    expect(normalizedFrame(setup.captureCharFrame())).toMatchSnapshot();
    setup.renderer.destroy();
  });

  test("流式助手输出保持纯文本并显示工作状态", async () => {
    const { transport, setup } = await renderApp(80, 24, false);
    await setup.mockInput.typeText("请检查当前改动");
    setup.mockInput.pressEnter();
    transport.emit("event", { type: "reasoning_delta", delta: "正在检查工作区。" });
    transport.emit("event", { type: "text_delta", delta: "当前改动已经通过初步检查。" });
    await setup.flush();
    await setup.waitForVisualIdle();
    expect(normalizedFrame(setup.captureCharFrame())).toMatchSnapshot();
    setup.renderer.destroy();
  });

  test("命令列表和工具弹窗使用无填充选择样式", async () => {
    const { setup } = await renderApp(80, 24, false);
    await setup.mockInput.typeText("/");
    await setup.flush();
    await setup.waitForVisualIdle();
    expect(normalizedFrame(setup.captureCharFrame())).toMatchSnapshot();

    await setup.mockInput.typeText("tools");
    setup.mockInput.pressEnter();
    await setup.waitForFrame((value) => value.includes("已注册 3 个工具"));
    await setup.waitForVisualIdle();
    expect(normalizedFrame(setup.captureCharFrame())).toMatchSnapshot();
    setup.renderer.destroy();
  });
});
