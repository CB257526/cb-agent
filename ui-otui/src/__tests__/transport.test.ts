import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { buildBackendEnv, buildRunAgentArgs, parseBackendArgs } from "../transport.js";

describe("OTUI 后端启动参数", () => {
  test("默认使用 JSON-RPC 和 light 记忆", () => {
    expect(buildRunAgentArgs("/app", {})).toEqual([
      join("/app", "run_agent.py"),
      "--transport",
      "jsonrpc",
      "--memory-system",
      "light",
    ]);
  });

  test("显式记忆模式覆盖默认值", () => {
    const backendArgs = parseBackendArgs(["--memory-system", "full", "--no-mcp"]);
    expect(buildRunAgentArgs("/app", {}, backendArgs)).toEqual([
      join("/app", "run_agent.py"),
      "--transport",
      "jsonrpc",
      "--memory-system",
      "full",
      "--no-mcp",
    ]);
    expect(buildBackendEnv({}, backendArgs).CBAGENT_ENABLE_FULL_MEMORY).toBe("1");
  });

  test("等号形式的 full 模式同样启用完整记忆", () => {
    const backendArgs = parseBackendArgs(["--memory-system=full"]);
    expect(buildBackendEnv({}, backendArgs).CBAGENT_ENABLE_FULL_MEMORY).toBe("1");
  });

  test("light 和 off 模式不修改完整记忆开关", () => {
    expect(buildBackendEnv({}, ["--memory-system", "light"])).not.toHaveProperty(
      "CBAGENT_ENABLE_FULL_MEMORY",
    );
    expect(buildBackendEnv({}, ["--memory-system=off"])).not.toHaveProperty(
      "CBAGENT_ENABLE_FULL_MEMORY",
    );
  });

  test("拒绝切换 OTUI 的后端 transport", () => {
    expect(() => parseBackendArgs(["--transport", "qq"])).toThrow(
      "OTUI 不支持启动参数",
    );
  });

  test("危险权限环境变量只追加一次", () => {
    const args = buildRunAgentArgs(
      "/app",
      { CBAGENT_DANGEROUSLY_SKIP_PERMISSIONS: "1" },
      ["--dangerously-skip-permissions"],
    );
    expect(args.filter((arg) => arg === "--dangerously-skip-permissions")).toHaveLength(1);
  });
});
