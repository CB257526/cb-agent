/**
 * TransportProvider：持有唯一的 Transport 实例（spawn 出的 Python 进程 + JSON-RPC），
 * 通过 Solid context 暴露给组件树。
 *
 * Transport 本身与框架无关（node:child_process + EventEmitter），从旧 ui-tui 原样复用。
 * 这里只负责：在 Provider 卸载时 close()，并把实例透传下去供 SessionProvider 订阅事件、
 * 供 Prompt/命令调用 RPC 方法。
 */

import { createContext, useContext, onCleanup, type ParentProps } from "solid-js";
import type { Transport } from "../transport.js";

const TransportContext = createContext<Transport>();

export function TransportProvider(props: ParentProps<{ transport: Transport }>) {
  onCleanup(() => props.transport.close());
  return (
    <TransportContext.Provider value={props.transport}>
      {props.children}
    </TransportContext.Provider>
  );
}

export function useTransport(): Transport {
  const t = useContext(TransportContext);
  if (!t) throw new Error("useTransport 必须在 TransportProvider 内使用");
  return t;
}
