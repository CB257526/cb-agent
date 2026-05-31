/**
 * 不依赖 ink 也不依赖 Python 后端，纯单元测试 transport 的 NDJSON 解析。
 *
 * 用 stream.PassThrough 模拟 child_process.stdout，验证：
 *   - 一行一条 JSON 正确切分
 *   - chunk 在行中间被切断时缓冲正确
 *   - notification（无 id）走 event 通道
 *   - response（带 id）走 response 通道
 *   - 非 JSON 行走 protocolError
 */

import { describe, it, expect } from "vitest";
import { Transport } from "../transport.js";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

// 用反射改造 Transport：跳过 spawn，直接喂数据到 handleStdout
function makeFakeTransport(): { t: Transport; feed: (s: string) => void; events: any[]; responses: any[]; errors: any[] } {
  const t = Object.create(Transport.prototype);
  EventEmitter.call(t);
  // 复制 Transport 构造的内部状态
  (t as any).stdoutBuf = "";
  (t as any).rpcCounter = 0;
  (t as any).stderrLogPath = "/dev/null";
  (t as any).proc = null;

  const events: any[] = [];
  const responses: any[] = [];
  const errors: any[] = [];
  t.on("event", (e: any) => events.push(e));
  t.on("response", (id: any, body: any) => responses.push({ id, body }));
  t.on("protocolError", (raw: string, err: Error) => errors.push({ raw, err }));

  const feed = (s: string) => (t as any).handleStdout(s);
  return { t, feed, events, responses, errors };
}

describe("Transport NDJSON parser", () => {
  it("parses one event per line", () => {
    const { feed, events } = makeFakeTransport();
    feed('{"jsonrpc":"2.0","method":"event","params":{"type":"text_delta","delta":"hi"}}\n');
    expect(events).toEqual([{ type: "text_delta", delta: "hi" }]);
  });

  it("buffers across chunk boundary", () => {
    const { feed, events } = makeFakeTransport();
    feed('{"jsonrpc":"2.0","method":"event","par');
    expect(events).toHaveLength(0);
    feed('ams":{"type":"done"}}\n');
    expect(events).toEqual([{ type: "done" }]);
  });

  it("handles multiple lines in one chunk", () => {
    const { feed, events } = makeFakeTransport();
    feed(
      '{"jsonrpc":"2.0","method":"event","params":{"type":"a"}}\n' +
      '{"jsonrpc":"2.0","method":"event","params":{"type":"b"}}\n'
    );
    expect(events.map((e) => e.type)).toEqual(["a", "b"]);
  });

  it("dispatches response by id", () => {
    const { feed, responses } = makeFakeTransport();
    feed('{"jsonrpc":"2.0","id":"r1","result":{"ok":true}}\n');
    expect(responses).toEqual([{ id: "r1", body: { result: { ok: true }, error: undefined } }]);
  });

  it("dispatches error response", () => {
    const { feed, responses } = makeFakeTransport();
    feed('{"jsonrpc":"2.0","id":"r2","error":{"code":-32601,"message":"no"}}\n');
    expect(responses[0].body.error.code).toBe(-32601);
  });

  it("emits protocolError on garbage line, recovers next line", () => {
    const { feed, events, errors } = makeFakeTransport();
    feed('not json\n');
    feed('{"jsonrpc":"2.0","method":"event","params":{"type":"recovered"}}\n');
    expect(errors).toHaveLength(1);
    expect(events).toEqual([{ type: "recovered" }]);
  });

  it("ignores empty lines", () => {
    const { feed, events } = makeFakeTransport();
    feed("\n\n\n");
    expect(events).toHaveLength(0);
  });
});
