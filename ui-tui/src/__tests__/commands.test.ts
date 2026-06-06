import { describe, it, expect, vi, beforeEach } from "vitest";
import { COMMANDS, filterCommands, findCommand, formatMCPStatus } from "../commands.js";
import type { CommandCtx } from "../commands.js";
import { readClipboardImageAttachment } from "../clipboardImage.js";

vi.mock("../clipboardImage.js", () => ({
  readClipboardImageAttachment: vi.fn(),
}));

describe("commands", () => {
  describe("filterCommands", () => {
    it("空 query 返回所有", () => {
      expect(filterCommands("")).toEqual([...COMMANDS]);
    });

    it("prefix 匹配", () => {
      const got = filterCommands("h");
      expect(got.map((c) => c.name)).toEqual(["/help"]);
    });

    it("大小写不敏感", () => {
      expect(filterCommands("CL").map((c) => c.name)).toEqual(["/clear"]);
    });

    it("无匹配返回空", () => {
      expect(filterCommands("xyz")).toEqual([]);
    });

    it("/compact 可按 prefix 找到", () => {
      expect(filterCommands("co").map((c) => c.name)).toEqual(["/compact"]);
    });
  });

  describe("findCommand", () => {
    it("精确匹配命中", () => {
      expect(findCommand("/help")?.name).toBe("/help");
      expect(findCommand("/tools")?.name).toBe("/tools");
      expect(findCommand("/compact")?.name).toBe("/compact");
      expect(findCommand("/mcp")?.name).toBe("/mcp");
    });
    it("含空格也能 trim", () => {
      expect(findCommand("  /clear  ")?.name).toBe("/clear");
    });
    it("不命中返回 undefined", () => {
      expect(findCommand("/nope")).toBeUndefined();
      expect(findCommand("hello")).toBeUndefined();
    });
  });

  describe("命令 handler", () => {
    let ctx: CommandCtx;
    let appendSystemMock: ReturnType<typeof vi.fn>;
    let setItemsMock: ReturnType<typeof vi.fn>;
    let applySessionPayloadMock: ReturnType<typeof vi.fn>;
    let setContextWindowMock: ReturnType<typeof vi.fn>;
    let resetContextWindowMock: ReturnType<typeof vi.fn>;
    let openSessionSwitcherMock: ReturnType<typeof vi.fn>;
    let toggleActivityMock: ReturnType<typeof vi.fn>;
    let setBuddyStateMock: ReturnType<typeof vi.fn>;
    let setAttachmentsMock: ReturnType<typeof vi.fn>;
    let transportMock: any;

    beforeEach(() => {
      appendSystemMock = vi.fn();
      setItemsMock = vi.fn();
      applySessionPayloadMock = vi.fn();
      setContextWindowMock = vi.fn();
      resetContextWindowMock = vi.fn();
      openSessionSwitcherMock = vi.fn();
      toggleActivityMock = vi.fn();
      setBuddyStateMock = vi.fn();
      setAttachmentsMock = vi.fn();
      transportMock = {
        clearHistory: vi.fn(),
        compactSession: vi.fn(),
        mcpStatus: vi.fn(),
        listTools: vi.fn(),
        loadSkill: vi.fn(),
        createSession: vi.fn(),
        switchSession: vi.fn(),
        runBuddyCommand: vi.fn(),
      };
      ctx = {
        transport: transportMock,
        input: "",
        args: "",
        appendSystem: appendSystemMock,
        setItems: setItemsMock,
        applySessionPayload: applySessionPayloadMock,
        setContextWindow: setContextWindowMock,
        resetContextWindow: resetContextWindowMock,
        openSessionSwitcher: openSessionSwitcherMock,
        toggleActivity: toggleActivityMock,
        setBuddyState: setBuddyStateMock,
        attachments: [],
        setAttachments: setAttachmentsMock,
      };
    });

    it("/help 输出命令清单", () => {
      const cmd = findCommand("/help")!;
      cmd.handler(ctx);
      expect(appendSystemMock).toHaveBeenCalledOnce();
      const text = appendSystemMock.mock.calls[0][0];
      expect(text).toContain("/help");
      expect(text).toContain("/clear");
      expect(text).toContain("/tools");
      expect(text).toContain("/sessions");
      expect(text).toContain("/compact");
      expect(text).toContain("/mcp");
      expect(text).toContain("/buddy");
      expect(text).toContain("/attach");
      expect(text).toContain("/paste-image");
      expect(text).toContain("/attachments");
      expect(text).toContain("/detach");
    });

    it("/clear 调 transport.clearHistory + 清 items + 给个提示", () => {
      const cmd = findCommand("/clear")!;
      cmd.handler(ctx);
      expect(transportMock.clearHistory).toHaveBeenCalledOnce();
      expect(setItemsMock).toHaveBeenCalledOnce();
      // setItems 收到的 updater 应当返回空数组
      const updater = setItemsMock.mock.calls[0][0];
      expect(updater([{ id: "x", role: "user", text: "old" }])).toEqual([]);
      expect(resetContextWindowMock).toHaveBeenCalledOnce();
      expect(appendSystemMock).toHaveBeenCalled();
    });

    it("/sessions 打开可见会话切换面板", () => {
      const cmd = findCommand("/sessions")!;
      cmd.handler(ctx);
      expect(openSessionSwitcherMock).toHaveBeenCalledOnce();
    });

    it("/compact 调后端压缩并只追加系统提示", async () => {
      transportMock.compactSession.mockResolvedValue({
        session: { session_id: "session_20260602_120000_abcdef12" },
        history: [{ role: "assistant", content: "【上下文压缩】摘要", kind: "compact_record" }],
        summary: "【上下文压缩】摘要",
        before_messages: 12,
        after_messages: 3,
        persisted: true,
        context_window: { used_tokens: 120, max_tokens: 8000, percent: 1.5 },
      });

      const cmd = findCommand("/compact")!;
      await cmd.handler(ctx);

      expect(transportMock.compactSession).toHaveBeenCalledOnce();
      expect(applySessionPayloadMock).not.toHaveBeenCalled();
      expect(setItemsMock).not.toHaveBeenCalled();
      expect(setContextWindowMock).toHaveBeenCalledWith({ used_tokens: 120, max_tokens: 8000, percent: 1.5 });
      expect(appendSystemMock.mock.calls[0][0]).toContain("history 12 -> 3");
      expect(appendSystemMock.mock.calls[0][0]).toContain("已落盘");
    });

    it("/compact 空上下文时给 no-op 提示", async () => {
      transportMock.compactSession.mockResolvedValue({
        session: null,
        history: [],
        summary: "",
        before_messages: 0,
        after_messages: 0,
        persisted: false,
        no_op: true,
      });

      const cmd = findCommand("/compact")!;
      await cmd.handler(ctx);

      expect(appendSystemMock.mock.calls[0][0]).toContain("没有可压缩");
    });

    it("/new 调 session.create 并应用返回的会话 payload", async () => {
      const payload = {
        session: { session_id: "session_20260602_120000_abcdef12" },
        history: [],
      };
      transportMock.createSession.mockResolvedValue(payload);
      const cmd = findCommand("/new")!;
      await cmd.handler(ctx);
      expect(transportMock.createSession).toHaveBeenCalledOnce();
      expect(applySessionPayloadMock).toHaveBeenCalledWith(
        payload,
        expect.stringContaining("session_20260602_120000_abcdef12"),
      );
    });

    it("/switch 带参数时调用后端切换并应用恢复 history", async () => {
      const payload = {
        session: { session_id: "session_20260602_120000_abcdef12" },
        history: [{ role: "user", content: "old", kind: null }],
      };
      transportMock.switchSession.mockResolvedValue(payload);
      const cmd = findCommand("/switch session_20260602_120000_abcdef12")!;
      await cmd.handler({ ...ctx, args: "session_20260602_120000_abcdef12" });
      expect(transportMock.switchSession).toHaveBeenCalledWith("session_20260602_120000_abcdef12");
      expect(applySessionPayloadMock).toHaveBeenCalledWith(
        payload,
        expect.stringContaining("session_20260602_120000_abcdef12"),
      );
    });

    it("/switch 缺参数时给出用法", async () => {
      const cmd = findCommand("/switch")!;
      await cmd.handler({ ...ctx, args: "" });
      expect(transportMock.switchSession).not.toHaveBeenCalled();
      expect(appendSystemMock.mock.calls[0][0]).toContain("/switch");
    });

    it("/tools 成功时格式化输出", async () => {
      transportMock.listTools.mockResolvedValue({
        tools: [
          { name: "bash", description: "执行 shell" },
          { name: "read", description: "" },
        ],
      });
      const cmd = findCommand("/tools")!;
      await cmd.handler(ctx);
      expect(appendSystemMock).toHaveBeenCalledOnce();
      const text = appendSystemMock.mock.calls[0][0];
      expect(text).toContain("已注册 2 个工具");
      expect(text).toContain("bash");
      expect(text).toContain("read");
    });

    it("/tools 空列表时给提示", async () => {
      transportMock.listTools.mockResolvedValue({ tools: [] });
      const cmd = findCommand("/tools")!;
      await cmd.handler(ctx);
      expect(appendSystemMock.mock.calls[0][0]).toContain("未注册");
    });

    it("/tools RPC 报错时输出错误信息", async () => {
      transportMock.listTools.mockRejectedValue(new Error("timeout"));
      const cmd = findCommand("/tools")!;
      await cmd.handler(ctx);
      const text = appendSystemMock.mock.calls[0][0];
      expect(text).toContain("✗");
      expect(text).toContain("timeout");
    });

    it("/skill 带参数时加载后端 Skill 内容", async () => {
      transportMock.loadSkill.mockResolvedValue({
        name: "pdf",
        content: "## Skill: pdf\nPDF body",
      });
      const cmd = findCommand("/skill pdf foo.pdf")!;
      await cmd.handler({ ...ctx, args: "pdf foo.pdf" });

      expect(transportMock.loadSkill).toHaveBeenCalledWith("pdf", "foo.pdf");
      expect(appendSystemMock.mock.calls[0][0]).toContain("## Skill: pdf");
    });

    it("/skill 缺参数时列出后端 Skill", async () => {
      transportMock.loadSkill.mockResolvedValue({
        name: null,
        content: "已发现 1 个 Skill：\n  - pdf: PDF skill",
      });
      const cmd = findCommand("/skill")!;
      await cmd.handler({ ...ctx, args: "" });

      expect(transportMock.loadSkill).toHaveBeenCalledWith("", "");
      expect(appendSystemMock.mock.calls[0][0]).toContain("已发现 1 个 Skill");
    });

    it("/skill RPC 报错时输出错误信息", async () => {
      transportMock.loadSkill.mockRejectedValue(new Error("missing skill"));
      const cmd = findCommand("/skill nope")!;
      await cmd.handler({ ...ctx, args: "nope" });

      const text = appendSystemMock.mock.calls[0][0];
      expect(text).toContain("✗");
      expect(text).toContain("missing skill");
    });

    it("/mcp 成功时格式化后台连接状态", async () => {
      transportMock.mcpStatus.mockResolvedValue({
        status: "loading",
        total: 2,
        connected: 1,
        failed: 0,
        servers: [
          { name: "filesystem", status: "connected", tools_count: 4, elapsed_seconds: 0.3 },
          { name: "playwright", status: "connecting" },
        ],
      });
      const cmd = findCommand("/mcp")!;
      await cmd.handler(ctx);
      expect(transportMock.mcpStatus).toHaveBeenCalledOnce();
      const text = appendSystemMock.mock.calls[0][0];
      expect(text).toContain("MCP 状态：loading");
      expect(text).toContain("filesystem: connected");
      expect(text).toContain("playwright: connecting");
    });

    it("/mcp RPC 报错时输出错误信息", async () => {
      transportMock.mcpStatus.mockRejectedValue(new Error("mcp timeout"));
      const cmd = findCommand("/mcp")!;
      await cmd.handler(ctx);
      const text = appendSystemMock.mock.calls[0][0];
      expect(text).toContain("✗");
      expect(text).toContain("mcp timeout");
    });

    it("/buddy 调后端命令并刷新 Buddy 状态", async () => {
      const state = {
        enabled: true,
        status: "ready",
        muted: false,
        companion: { name: "Waddles" },
      };
      transportMock.runBuddyCommand.mockResolvedValue({
        text: "Buddy 已孵化",
        changed: true,
        state,
      });

      const cmd = findCommand("/buddy hatch")!;
      await cmd.handler({ ...ctx, args: "hatch" });

      expect(transportMock.runBuddyCommand).toHaveBeenCalledWith("hatch");
      expect(setBuddyStateMock).toHaveBeenCalledWith(state);
      expect(appendSystemMock.mock.calls[0][0]).toContain("Buddy 已孵化");
    });

    it.each(["pet", "off", "on", "mute", "unmute", "rehatch"])(
      "/buddy %s 原样交给后端",
      async (sub) => {
        transportMock.runBuddyCommand.mockResolvedValue({
          text: "",
          changed: true,
          state: { enabled: true, status: "ready", muted: false, companion: null },
        });

        const cmd = findCommand(`/buddy ${sub}`)!;
        await cmd.handler({ ...ctx, args: sub });

        expect(transportMock.runBuddyCommand).toHaveBeenCalledWith(sub);
      },
    );

    it("/buddy RPC 报错时输出错误信息", async () => {
      transportMock.runBuddyCommand.mockRejectedValue(new Error("buddy timeout"));
      const cmd = findCommand("/buddy pet")!;
      await cmd.handler({ ...ctx, args: "pet" });
      const text = appendSystemMock.mock.calls[0][0];
      expect(text).toContain("✗");
      expect(text).toContain("buddy timeout");
    });

    it("/attach 添加本地附件到队列", () => {
      const cmd = findCommand("/attach C:\\tmp\\shot.png")!;
      cmd.handler({ ...ctx, args: "C:\\tmp\\shot.png" });

      expect(setAttachmentsMock).toHaveBeenCalledOnce();
      const updater = setAttachmentsMock.mock.calls[0][0];
      const next = updater([]);
      expect(next).toHaveLength(1);
      expect(next[0]).toMatchObject({
        path: "C:\\tmp\\shot.png",
        source: "direct",
        fileName: "shot.png",
      });
      expect(appendSystemMock.mock.calls[0][0]).toContain("已添加附件");
    });

    it("/attach 缺参数时提示用法", () => {
      const cmd = findCommand("/attach")!;
      cmd.handler({ ...ctx, args: "" });

      expect(setAttachmentsMock).not.toHaveBeenCalled();
      expect(appendSystemMock.mock.calls[0][0]).toContain("/attach <path>");
    });

    it("/paste-image 从剪贴板读取图片并加入队列", async () => {
      vi.mocked(readClipboardImageAttachment).mockResolvedValue({
        id: "clip_1",
        path: "C:\\tmp\\clipboard.png",
        fileName: "clipboard.png",
        modality: "image",
        source: "clipboard",
        size: 10,
      });

      const cmd = findCommand("/paste-image")!;
      await cmd.handler(ctx);

      expect(readClipboardImageAttachment).toHaveBeenCalledOnce();
      expect(setAttachmentsMock).toHaveBeenCalledOnce();
      const updater = setAttachmentsMock.mock.calls[0][0];
      expect(updater([])[0].fileName).toBe("clipboard.png");
      expect(appendSystemMock.mock.calls[0][0]).toContain("已从剪贴板添加图片");
    });

    it("/paste-image 失败时展示错误", async () => {
      vi.mocked(readClipboardImageAttachment).mockRejectedValue(new Error("剪贴板里没有图片"));

      const cmd = findCommand("/paste-image")!;
      await cmd.handler(ctx);

      expect(setAttachmentsMock).not.toHaveBeenCalled();
      expect(appendSystemMock.mock.calls[0][0]).toContain("剪贴板图片读取失败");
    });

    it("/attachments 展示队列", () => {
      const cmd = findCommand("/attachments")!;
      cmd.handler({
        ...ctx,
        attachments: [{
          id: "a1",
          path: "C:\\tmp\\shot.png",
          fileName: "shot.png",
          source: "direct",
          size: 12,
        }],
      });

      expect(appendSystemMock.mock.calls[0][0]).toContain("shot.png");
      expect(appendSystemMock.mock.calls[0][0]).toContain("12B");
    });

    it("/detach all 清空队列", () => {
      const cmd = findCommand("/detach all")!;
      cmd.handler({
        ...ctx,
        args: "all",
        attachments: [{
          id: "a1",
          path: "C:\\tmp\\shot.png",
          fileName: "shot.png",
          source: "direct",
        }],
      });

      expect(setAttachmentsMock).toHaveBeenCalledOnce();
      const updater = setAttachmentsMock.mock.calls[0][0];
      expect(updater([{ id: "x" }])).toEqual([]);
      expect(appendSystemMock.mock.calls[0][0]).toContain("已清空 1 个");
    });

    it("/detach index 移除指定附件", () => {
      const attachments = [
        { id: "a1", path: "one.png", fileName: "one.png", source: "direct" as const },
        { id: "a2", path: "two.png", fileName: "two.png", source: "direct" as const },
      ];
      const cmd = findCommand("/detach 2")!;
      cmd.handler({ ...ctx, args: "2", attachments });

      expect(setAttachmentsMock).toHaveBeenCalledOnce();
      const updater = setAttachmentsMock.mock.calls[0][0];
      expect(updater(attachments).map((item: any) => item.fileName)).toEqual(["one.png"]);
      expect(appendSystemMock.mock.calls[0][0]).toContain("two.png");
    });

    it("/detach 越界时给提示", () => {
      const cmd = findCommand("/detach 9")!;
      cmd.handler({ ...ctx, args: "9", attachments: [] });

      expect(setAttachmentsMock).not.toHaveBeenCalled();
      expect(appendSystemMock.mock.calls[0][0]).toContain("超出范围");
    });

    it("/log 触发 toggleActivity", () => {
      const cmd = findCommand("/log")!;
      cmd.handler(ctx);
      expect(toggleActivityMock).toHaveBeenCalledOnce();
    });
  });

  describe("formatMCPStatus", () => {
    it("能格式化空 server 且保留 error", () => {
      const text = formatMCPStatus({
        status: "disabled",
        total: 0,
        connected: 0,
        failed: 0,
        error: "未找到 mcp.json",
        servers: [],
      });
      expect(text).toContain("disabled");
      expect(text).toContain("未找到 mcp.json");
    });
  });
});
