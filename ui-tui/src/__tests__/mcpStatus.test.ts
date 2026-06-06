import { describe, expect, it } from "vitest";
import { formatMCPStatus } from "../commands.js";

describe("formatMCPStatus MCP transport", () => {
  it("显示每个 MCP server 的 transport 类型", () => {
    const text = formatMCPStatus({
      status: "loading",
      total: 2,
      connected: 1,
      failed: 0,
      servers: [
        { name: "github", status: "connecting", transport: "http" },
        { name: "playwright", status: "connected", transport: "stdio", tools_count: 3 },
      ],
    });

    expect(text).toContain("github");
    expect(text).toContain("transport=http");
    expect(text).toContain("playwright");
    expect(text).toContain("transport=stdio");
  });
});
