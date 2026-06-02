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
import { RUN_AGENT_ARGS, STDERR_UI_LINE_MAX, Transport } from "../transport.js";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

describe("Transport launch args", () => {
  it("starts the backend in lightweight markdown memory mode by default", () => {
    expect(RUN_AGENT_ARGS).toEqual([
      "run_agent.py",
      "--transport",
      "jsonrpc",
      "--memory-system",
      "light",
    ]);
  });
});

// 用反射改造 Transport：跳过 spawn，直接喂数据到 handleStdout
function makeFakeTransport(): { t: Transport; feed: (s: string) => void; feedErr: (s: string) => void; events: any[]; responses: any[]; errors: any[]; stderrLines: string[] } {
  const t = Object.create(Transport.prototype);
  EventEmitter.call(t);
  // 复制 Transport 构造的内部状态
  (t as any).stdoutBuf = "";
  (t as any).stderrBuf = "";
  (t as any).rpcCounter = 0;
  (t as any).stderrLogPath = "/dev/null";
  (t as any).proc = null;

  const events: any[] = [];
  const responses: any[] = [];
  const errors: any[] = [];
  const stderrLines: string[] = [];
  t.on("event", (e: any) => events.push(e));
  t.on("response", (id: any, body: any) => responses.push({ id, body }));
  t.on("protocolError", (raw: string, err: Error) => errors.push({ raw, err }));
  t.on("stderr", (line: string) => stderrLines.push(line));

  const feed = (s: string) => (t as any).handleStdout(s);
  const feedErr = (s: string) => (t as any).handleStderr(s);
  return { t, feed, feedErr, events, responses, errors, stderrLines };
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

describe("Transport stderr line splitter", () => {
  it("emits one event per line, preserves content", () => {
    const { feedErr, stderrLines } = makeFakeTransport();
    feedErr("first\nsecond\n");
    expect(stderrLines).toEqual(["first", "second"]);
  });

  it("buffers across chunk boundary", () => {
    const { feedErr, stderrLines } = makeFakeTransport();
    feedErr("partia");
    expect(stderrLines).toEqual([]);
    feedErr("l line\nnext\n");
    expect(stderrLines).toEqual(["partial line", "next"]);
  });

  it("strips trailing CR (Windows-style \\r\\n)", () => {
    const { feedErr, stderrLines } = makeFakeTransport();
    feedErr("hello\r\nworld\r\n");
    expect(stderrLines).toEqual(["hello", "world"]);
  });

  it("emits empty lines (so panel keeps visual spacing)", () => {
    const { feedErr, stderrLines } = makeFakeTransport();
    feedErr("a\n\nb\n");
    expect(stderrLines).toEqual(["a", "", "b"]);
  });

  it("does not emit a partial last line until newline arrives", () => {
    const { feedErr, stderrLines } = makeFakeTransport();
    feedErr("no newline yet");
    expect(stderrLines).toEqual([]);
  });

  it("clips very long stderr lines for the live UI preview", () => {
    const { feedErr, stderrLines } = makeFakeTransport();
    feedErr(`${"x".repeat(STDERR_UI_LINE_MAX + 25)}\n`);
    expect(stderrLines).toHaveLength(1);
    expect(stderrLines[0].length).toBeLessThan(STDERR_UI_LINE_MAX + 80);
    expect(stderrLines[0]).toContain("实时日志已截断");
  });
});

describe("Transport answerQuestion RPC", () => {
  // 用一个抓 stdin 写入的桩 proc
  function makeWithStdin(): { t: any; written: string[] } {
    const t = Object.create(Transport.prototype);
    EventEmitter.call(t);
    (t as any).rpcCounter = 0;
    const written: string[] = [];
    (t as any).proc = { stdin: { write: (s: string) => { written.push(s); } } };
    return { t, written };
  }

  it("serializes a single-select answer", () => {
    const { t, written } = makeWithStdin();
    const id = t.answerQuestion({ question_id: "q_x", selected_labels: ["A"] });
    expect(written).toHaveLength(1);
    const msg = JSON.parse(written[0].trim());
    expect(msg.method).toBe("session.answer_question");
    expect(msg.id).toBe(id);
    expect(msg.params).toEqual({ question_id: "q_x", selected_labels: ["A"] });
  });

  it("serializes a multi-select answer with other_text", () => {
    const { t, written } = makeWithStdin();
    t.answerQuestion({
      question_id: "q_y",
      selected_labels: ["Other"],
      other_text: "Hello",
    });
    const msg = JSON.parse(written[0].trim());
    expect(msg.params.selected_labels).toEqual(["Other"]);
    expect(msg.params.other_text).toBe("Hello");
  });

  it("serializes a cancellation", () => {
    const { t, written } = makeWithStdin();
    t.answerQuestion({ question_id: "q_z", selected_labels: [], cancelled: true });
    const msg = JSON.parse(written[0].trim());
    expect(msg.params.cancelled).toBe(true);
  });

  it("serializes session list/create/switch/compact RPCs", () => {
    const { t, written } = makeWithStdin();
    t.listSessions();
    t.createSession();
    t.switchSession("session_20260602_120000_abcdef12");
    t.compactSession();

    const messages = written.map((line) => JSON.parse(line.trim()));
    expect(messages[0].method).toBe("session.list_sessions");
    expect(messages[1].method).toBe("session.create");
    expect(messages[2].method).toBe("session.switch");
    expect(messages[2].params).toEqual({ session_id: "session_20260602_120000_abcdef12" });
    expect(messages[3].method).toBe("session.compact");
    expect(messages[3].params).toEqual({});
  });
});
