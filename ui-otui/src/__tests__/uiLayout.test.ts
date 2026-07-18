import { describe, expect, test } from "bun:test";
import {
  getDialogMetrics,
  getHorizontalPadding,
  getLayoutMode,
  getPromptMaxHeight,
  shortSessionId,
  sliceAroundSelection,
} from "../layout.js";
import { theme } from "../theme.js";

describe("OTUI 响应式布局", () => {
  test("宽度断点保持明确且连续", () => {
    expect(getLayoutMode(52)).toBe("narrow");
    expect(getLayoutMode(67)).toBe("narrow");
    expect(getLayoutMode(68)).toBe("medium");
    expect(getLayoutMode(99)).toBe("medium");
    expect(getLayoutMode(100)).toBe("wide");
  });

  test("外边距与输入高度随终端尺寸收缩", () => {
    expect(getHorizontalPadding(79)).toBe(1);
    expect(getHorizontalPadding(80)).toBe(2);
    expect(getPromptMaxHeight(10)).toBe(3);
    expect(getPromptMaxHeight(18)).toBe(4);
    expect(getPromptMaxHeight(24)).toBe(6);
    expect(getPromptMaxHeight(40)).toBe(8);
  });

  test("弹窗尺寸不会超过计划中的可用区域", () => {
    expect(getDialogMetrics(120, 36)).toEqual({ width: 72, maxHeight: 32 });
    expect(getDialogMetrics(80, 24)).toEqual({ width: 72, maxHeight: 20 });
    expect(getDialogMetrics(52, 18)).toEqual({ width: 48, maxHeight: 14 });
  });

  test("列表窗口围绕当前项并在尾部向前补齐", () => {
    const items = Array.from({ length: 12 }, (_, index) => index);
    expect(sliceAroundSelection(items, 0, 5)).toEqual({ items: [0, 1, 2, 3, 4], start: 0 });
    expect(sliceAroundSelection(items, 6, 5)).toEqual({ items: [4, 5, 6, 7, 8], start: 4 });
    expect(sliceAroundSelection(items, 11, 5)).toEqual({ items: [7, 8, 9, 10, 11], start: 7 });
  });

  test("会话标识与终端颜色意图保持稳定", () => {
    expect(shortSessionId("session_20260717_143003_76e0de48")).toBe("20260717_143003_76e0de48");
    expect(theme.background.intent).toBe("default");
    expect(theme.text.intent).toBe("default");
    expect(theme.primary.intent).toBe("indexed");
    expect(theme.primary.slot).toBe(6);
    expect(theme.agent.slot).toBe(5);
    expect(theme.success.slot).toBe(2);
    expect(theme.error.slot).toBe(1);
  });
});
