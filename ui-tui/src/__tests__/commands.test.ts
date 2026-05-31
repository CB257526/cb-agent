import { describe, it, expect, vi, beforeEach } from "vitest";
import { COMMANDS, filterCommands, findCommand } from "../commands.js";
import type { CommandCtx } from "../commands.js";

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
  });

  describe("findCommand", () => {
    it("精确匹配命中", () => {
      expect(findCommand("/help")?.name).toBe("/help");
      expect(findCommand("/tools")?.name).toBe("/tools");
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
    let toggleActivityMock: ReturnType<typeof vi.fn>;
    let transportMock: any;

    beforeEach(() => {
      appendSystemMock = vi.fn();
      setItemsMock = vi.fn();
      toggleActivityMock = vi.fn();
      transportMock = {
        clearHistory: vi.fn(),
        listTools: vi.fn(),
      };
      ctx = {
        transport: transportMock,
        appendSystem: appendSystemMock,
        setItems: setItemsMock,
        toggleActivity: toggleActivityMock,
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
    });

    it("/clear 调 transport.clearHistory + 清 items + 给个提示", () => {
      const cmd = findCommand("/clear")!;
      cmd.handler(ctx);
      expect(transportMock.clearHistory).toHaveBeenCalledOnce();
      expect(setItemsMock).toHaveBeenCalledOnce();
      // setItems 收到的 updater 应当返回空数组
      const updater = setItemsMock.mock.calls[0][0];
      expect(updater([{ id: "x", role: "user", text: "old" }])).toEqual([]);
      expect(appendSystemMock).toHaveBeenCalled();
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

    it("/log 触发 toggleActivity", () => {
      const cmd = findCommand("/log")!;
      cmd.handler(ctx);
      expect(toggleActivityMock).toHaveBeenCalledOnce();
    });
  });
});
