/** 把 token 数格式化为 Footer 使用的紧凑文本。 */
export function formatTokenCount(tokens: number): string {
  if (!Number.isFinite(tokens) || tokens <= 0) return "0";
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}k`;
  return `${Math.round(tokens)}`;
}

/** Context 本地估算值带波浪号，provider 实际值直接显示。 */
export function formatContextTokenCount(tokens: number, source?: string): string {
  const prefix = source === "provider" ? "" : "~";
  return `${prefix}${formatTokenCount(tokens)}`;
}

/** 分别格式化累计输入、缓存子集和输出；缓存不会从输入中扣除。 */
export function formatUsageCounts(prompt: number, cached: number, completion: number) {
  return {
    input: formatTokenCount(prompt),
    cached: formatTokenCount(cached),
    output: formatTokenCount(completion),
  };
}
