import React, { useEffect, useState } from "react";
import { Text } from "ink";

const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

/** 朴素 spinner，~80ms 一帧。完整版 Claude Code 还有 shimmer 效果，这里先做 V0。 */
export function Spinner({ color }: { color?: string }) {
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setFrame((f) => (f + 1) % FRAMES.length), 80);
    return () => clearInterval(t);
  }, []);
  return <Text color={color}>{FRAMES[frame]}</Text>;
}
