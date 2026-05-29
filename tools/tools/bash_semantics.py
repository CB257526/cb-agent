"""Shell 命令退出码语义

参考 Claude Code commandSemantics.ts 的设计。

许多命令用非零退出码表达"信息"而非"错误"，把这个解释注入给
模型可以避免 agent 误判命令失败后反复重试。

格式：{命令名: {退出码: ("ok"|"error", "解释文本")}}
"""

from typing import Dict, Optional, Tuple

EXIT_CODE_SEMANTICS: Dict[str, Dict[int, Tuple[str, str]]] = {
    # grep / rg: 0=匹配成功, 1=无匹配(normal), ≥2=真错误
    "grep": {1: ("ok", "未找到匹配项")},
    "rg":   {1: ("ok", "未找到匹配项")},

    # find: 0=完成, 1=完成但有部分目录不可达, ≥2=错误
    "find": {1: ("ok", "部分目录不可访问（权限不足等）")},

    # diff: 0=无差异, 1=有差异(normal), ≥2=错误
    "diff": {1: ("ok", "文件存在差异")},

    # test / [: 0=条件为真, 1=条件为假(normal), ≥2=语法错误
    "test": {1: ("ok", "条件为假")},
    "[":    {1: ("ok", "条件为假")},
}


def lookup_semantic(command: str, exit_code: int) -> Optional[Dict[str, str]]:
    """查询指定命令的退出码语义。

    Returns:
        {"status": "ok", "message": "..."} 或 None（表默认语义：非零=error）
    """
    base = command.strip().split()[0].split("/")[-1]
    table = EXIT_CODE_SEMANTICS.get(base)
    if not table:
        return None
    entry = table.get(exit_code)
    if entry:
        return {"status": entry[0], "message": entry[1]}
    return None
