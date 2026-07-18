/**
 * Spinner：模型工作期间的加载动效（左下角）。
 *
 * 用 Solid 的 createSignal + setInterval 做帧动画（~80ms 一帧），onCleanup 清掉
 * 定时器避免泄漏。只在 busy 时由 Footer 挂载，所以组件自身不判断 busy——
 * 父组件用 <Show when={busy}> 控制挂载/卸载即可。
 *
 * 旁边带一行随机轮换的状态词（thinking…/working… 之类），更接近 opencode 观感。
 */

import { createSignal, onCleanup } from "solid-js";
import type { ColorInput } from "@opentui/core";

const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

export function Spinner(props: { color?: ColorInput; label?: string }) {
  const [frame, setFrame] = createSignal(0);
  const timer = setInterval(() => setFrame((f) => (f + 1) % FRAMES.length), 80);
  onCleanup(() => clearInterval(timer));

  return (
    <text fg={props.color}>
      {FRAMES[frame()]}
      {props.label ? ` ${props.label}` : ""}
    </text>
  );
}
