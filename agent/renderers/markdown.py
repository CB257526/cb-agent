"""MarkdownAccumulator —— 流式 Markdown 安全切分。

问题场景：
  LLM 流式回答 chunk-by-chunk 来。如果直接把已拼接缓冲交给 Markdown 渲染器
  （Textual 的 RichLog / rich.markdown），会遇到：
    1. 半个代码块：'```python\nprint(1' 这时还没 ```，渲染成普通段落
    2. 半个标题：'# 引导' 后面还没换行 → 渲染对，但 '# 引' 渲染成空标题
    3. 半个表格：行没收齐就 render，列对不上
    4. 半个加粗：'**高亮' 没 `**` 闭合，渲染整段加粗到底

CLI 模式（直接 print 字符）不在乎这些，因为终端是字符流，最终视觉跟拼起来
一致。但 TUI/Web 模式需要"安全可渲染前缀 + 待稳定缓冲"二段切：
    feed(chunk) → 返回 self 上次 feed 之后**新增**的"稳定前缀"
                  （前端渲染这段；不会再变）
    flush()     → chat 结束，把剩余缓冲一次性当稳定输出

切分规则（保守，永远倾向于晚切）：
    - 只在 fenced code block (```) 之外切
    - 切点是最后一个 \\n\\n（段落分界）。没有就一个字符都不返回，等着
    - 不试图"半段标题切"——标题行短，等下一个 \\n 出现自然处理

为什么是段落级而不是字符级：
    渲染器（rich/Markdown）按段落组装，半个段落 push 进去会 reflow，
    Textual 终端会闪。Claude Code 桌面端、ChatGPT 都用类似策略。

不处理的：
    - 嵌套代码块：``` 内套 ``` 不能用 fence 匹配
      （Markdown 规范本身也不支持，按 fence 计数即可）
    - 行内 \\n：段落内换行不切，整段一起切

线程：本类 **不是** 线程安全的，调用方自己加锁（CLIRenderer 已经有 self._lock）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class MarkdownAccumulator:
    """流式 Markdown 累积器。"""

    # 完整缓冲（feed 进来的所有 chunk 拼起来）
    _buffer: str = ""
    # 已经吐过的"稳定前缀"长度（再 feed 时只看 _buffer[_emitted_len:]）
    _emitted_len: int = 0
    # 强制 flush 后不再切分，整段直出
    _flushed: bool = False

    def feed(self, chunk: str) -> str:
        """喂进新 chunk，返回**自上次 feed/flush 之后新增**的稳定前缀。

        没有新稳定前缀就返回 ""。永远不返回半个代码块或半个段落。
        """
        if not chunk:
            return ""
        self._buffer += chunk
        if self._flushed:
            # 已经 flush 过了，后续都直出
            new_part = self._buffer[self._emitted_len:]
            self._emitted_len = len(self._buffer)
            return new_part

        safe_end = self._safe_split_point(self._buffer)
        if safe_end <= self._emitted_len:
            return ""
        new_part = self._buffer[self._emitted_len:safe_end]
        self._emitted_len = safe_end
        return new_part

    def flush(self) -> str:
        """chat 结束时调一次，把剩余缓冲全部视为稳定输出。"""
        self._flushed = True
        new_part = self._buffer[self._emitted_len:]
        self._emitted_len = len(self._buffer)
        return new_part

    @property
    def stable(self) -> str:
        """已确认稳定的前缀（不再变）。"""
        return self._buffer[: self._emitted_len]

    @property
    def pending(self) -> str:
        """还没稳定的尾部缓冲（feed 后可能仍在变）。"""
        return self._buffer[self._emitted_len:]

    @property
    def full(self) -> str:
        """完整缓冲 = stable + pending。"""
        return self._buffer

    def reset(self) -> None:
        self._buffer = ""
        self._emitted_len = 0
        self._flushed = False

    # ---------- 内部 ----------

    @staticmethod
    def _safe_split_point(text: str) -> int:
        """返回 text[:k] 是"完整可渲染段落"的最大 k。

        规则：
          - 在 fenced code block (```) 之内 → 返回 0（整段都 pending）
          - 否则 → 返回最后一个 '\\n\\n' 之后的位置（段落分界）；
            没有 '\\n\\n' → 返回 0
        """
        # 计 ``` 出现的"独立行"次数。fence 必须独占一行（前面是 \n 或开头）
        in_code = MarkdownAccumulator._inside_fence(text)
        if in_code:
            return 0
        # 找最后一个段落分界 \n\n。注意要在 code fence 之外
        # 简化：fence 已经判过了在外面，整段直接搜 \n\n
        idx = text.rfind("\n\n")
        if idx < 0:
            return 0
        return idx + 2  # 包含两个换行

    @staticmethod
    def _inside_fence(text: str) -> bool:
        """text 末尾是否在某个未闭合的 fenced code block 之内。"""
        count = 0
        # 把 text 拆成行，遇到 ^``` 或 ^```lang 就计数
        for line in text.split("\n"):
            stripped = line.lstrip()
            if stripped.startswith("```"):
                count += 1
        return count % 2 == 1


__all__ = ["MarkdownAccumulator"]
