import { describe, expect, test } from "bun:test";
import {
  formatContextTokenCount,
  formatTokenCount,
  formatUsageCounts,
} from "../tokenDisplay.js";

describe("Footer token 展示", () => {
  test("紧凑格式化 token 数", () => {
    expect(formatTokenCount(0)).toBe("0");
    expect(formatTokenCount(999)).toBe("999");
    expect(formatTokenCount(1250)).toBe("1.3k");
  });

  test("估算 Context 带波浪号，provider 实际值不带", () => {
    expect(formatContextTokenCount(1200, "estimate")).toBe("~1.2k");
    expect(formatContextTokenCount(1200, "provider")).toBe("1.2k");
  });

  test("Cached 是 Input 子集，不从 Input 扣除", () => {
    expect(formatUsageCounts(10000, 8000, 500)).toEqual({
      input: "10.0k",
      cached: "8.0k",
      output: "500",
    });
  });
});
