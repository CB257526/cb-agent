#!/usr/bin/env python3
"""cb-agent Linux 部署前自检脚本。

这个脚本只做只读检查：读取当前环境变量、项目根目录下的 .env、常见命令是否存在，
然后把可能影响 Linux 部署的缺口打印出来。它不会安装依赖，也不会修改配置文件。
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
MCP_FILE = PROJECT_ROOT / "mcp.json"


@dataclass
class Check:
    """单条检查结果；level 用于决定最终退出码和终端展示。"""

    level: str
    name: str
    message: str
    tip: str = ""


def load_dotenv(path: Path) -> dict[str, str]:
    """轻量解析 .env，避免部署自检额外依赖 python-dotenv。"""

    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


DOTENV = load_dotenv(ENV_FILE)


def env_value(name: str, default: str = "") -> str:
    """环境变量优先，其次使用项目 .env；不会打印敏感值本身。"""

    return os.environ.get(name, DOTENV.get(name, default))


def is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def command_version(name: str, args: Iterable[str] = ("--version",)) -> str:
    """读取命令版本；失败时只返回简短错误，避免部署检查卡住。"""

    executable = shutil.which(name) or name
    try:
        proc = subprocess.run(
            [executable, *args],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=8,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - 只在特殊系统环境触发
        return f"无法读取版本: {exc}"
    output = normalize_command_output(proc.stdout or "")
    if output is None:
        return "已安装，但版本输出不是普通文本"
    first_line = output.splitlines()[0:1]
    return first_line[0].strip() if first_line else "已安装，但没有版本输出"


def normalize_command_output(text: str) -> str | None:
    """清理外部命令版本输出中的控制字符。

    部署检查只需要“一眼知道命令存在和大概版本”。如果某些平台 shim 输出了
    NUL 或大量不可打印字符，直接降级为普通提示，比把乱码打到控制台更有用。
    """

    if "\x00" in text:
        return None
    cleaned = "".join(ch if ch in "\r\n\t" or ord(ch) >= 32 else " " for ch in text)
    visible = sum(1 for ch in cleaned if not ch.isspace())
    if cleaned and visible / max(len(cleaned), 1) < 0.2:
        return None
    return cleaned


def safe_console(text: str) -> str:
    """把外部命令输出整理成当前控制台一定能打印的文本。

    Windows 的 npm/npx shim 偶尔会输出当前控制台编码无法表达的字符；这里不让
    版本探测结果影响整个部署自检，只把不可打印字符替换成问号。
    """

    encoding = sys.stdout.encoding or "utf-8"
    text = text.replace("\ufffd", "?")
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def add(results: list[Check], level: str, name: str, message: str, tip: str = "") -> None:
    results.append(Check(level=level, name=name, message=message, tip=tip))


def check_python(results: list[Check]) -> None:
    current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        add(results, "OK", "Python 版本", f"当前解释器 {current}，满足 >=3.10。")
    else:
        add(results, "FAIL", "Python 版本", f"当前解释器 {current}，低于项目要求。", "请使用 Python 3.10+ 创建虚拟环境。")

    configured = env_value("CB_AGENT_PYTHON")
    if configured:
        path = Path(configured).expanduser()
        if path.exists():
            add(results, "OK", "TUI Python 路径", f"CB_AGENT_PYTHON 已指向存在的解释器: {path}")
        else:
            add(results, "FAIL", "TUI Python 路径", f"CB_AGENT_PYTHON 指向的文件不存在: {path}")
    else:
        expected = PROJECT_ROOT.parent / "venv" / "bin" / "python"
        if expected.exists():
            add(results, "OK", "TUI Python 路径", f"发现推荐虚拟环境解释器: {expected}")
        else:
            add(
                results,
                "WARN",
                "TUI Python 路径",
                "未设置 CB_AGENT_PYTHON，且没有发现 ../venv/bin/python。",
                "Linux 上 TUI 会兜底 python3；服务器部署建议显式 export CB_AGENT_PYTHON=/path/to/venv/bin/python。",
            )


def check_required_env(results: list[Check]) -> None:
    required = ["LLM_MODEL_ID", "LLM_API_KEY", "LLM_BASE_URL"]
    missing = [name for name in required if not env_value(name)]
    if missing:
        add(results, "FAIL", "LLM 配置", f"缺少必填项: {', '.join(missing)}", "请复制 .env.example 为 .env 并填入模型配置。")
    else:
        add(results, "OK", "LLM 配置", "LLM_MODEL_ID / LLM_API_KEY / LLM_BASE_URL 已配置。")

    if is_truthy(env_value("CBAGENT_DANGEROUSLY_SKIP_PERMISSIONS")):
        add(
            results,
            "WARN",
            "危险权限模式",
            "CBAGENT_DANGEROUSLY_SKIP_PERMISSIONS 已启用，BashTool 会跳过权限确认和高危命令拦截。",
            "共享服务器、公网服务、QQ 群聊或不可信提示词场景不建议开启。",
        )


def check_commands(results: list[Check]) -> None:
    if platform.system() == "Linux":
        add(results, "OK", "操作系统", f"当前系统为 Linux: {platform.platform()}")
    else:
        add(results, "WARN", "操作系统", f"当前系统不是 Linux: {platform.platform()}", "此脚本仍可运行，但部署提示按 Linux 场景设计。")

    for command, required, note in [
        ("bash", True, "BashTool 和后台任务依赖 bash。"),
        ("rg", False, "local grep/glob 会优先使用 rg；缺失时会降级到 Python 遍历，速度较慢。"),
        ("node", False, "TUI 和 npx MCP server 需要 Node.js。"),
        ("npm", False, "TUI 安装依赖需要 npm。"),
        ("npx", False, "mcp.json 里的 amap/playwright/tavily MCP 默认通过 npx 启动。"),
    ]:
        if command_exists(command):
            add(results, "OK", f"命令 {command}", command_version(command))
        else:
            level = "FAIL" if required else "WARN"
            add(results, level, f"命令 {command}", f"未找到 {command}。", note)


def iter_strings(value: Any) -> Iterable[str]:
    """递归提取 JSON 中的字符串，用于发现 ${VAR} 环境变量占位符。"""

    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)


def check_mcp(results: list[Check]) -> None:
    if not MCP_FILE.exists():
        add(results, "INFO", "MCP 配置", "未发现 mcp.json；跳过 MCP 自检。")
        return

    try:
        data = json.loads(MCP_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        add(results, "FAIL", "MCP 配置", f"mcp.json 不是合法 JSON: {exc}")
        return

    servers = data.get("mcpServers") or {}
    npx_servers = [name for name, cfg in servers.items() if isinstance(cfg, dict) and cfg.get("command") == "npx"]
    if npx_servers and not command_exists("npx"):
        add(results, "WARN", "MCP npx", f"这些 MCP server 需要 npx: {', '.join(npx_servers)}", "安装 Node.js/npm，或启动时使用 --no-mcp。")
    elif npx_servers:
        add(results, "OK", "MCP npx", f"npx 可用，检测到 npx MCP server: {', '.join(npx_servers)}")

    placeholders = sorted({match.group(1) for text in iter_strings(servers) for match in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text)})
    missing = [name for name in placeholders if not env_value(name)]
    if missing:
        add(results, "WARN", "MCP 环境变量", f"mcp.json 引用了未配置变量: {', '.join(missing)}", "相关 MCP server 可能启动失败；不用 MCP 时可加 --no-mcp。")
    elif placeholders:
        add(results, "OK", "MCP 环境变量", f"mcp.json 引用的变量已配置: {', '.join(placeholders)}")

    if "playwright" in servers:
        add(results, "INFO", "Playwright MCP", "Linux 首次使用 Playwright MCP 时可能需要安装浏览器依赖。", "可在服务器上运行: npx playwright install --with-deps")


def check_clipboard(results: list[Check]) -> None:
    """检查 TUI 剪贴板图片粘贴的 Linux 桌面依赖。"""

    if platform.system() != "Linux":
        return
    if os.environ.get("WAYLAND_DISPLAY"):
        if command_exists("wl-paste"):
            add(results, "OK", "Wayland 剪贴板", "检测到 WAYLAND_DISPLAY，且 wl-paste 可用。")
        else:
            add(results, "WARN", "Wayland 剪贴板", "检测到 WAYLAND_DISPLAY，但未找到 wl-paste。", "安装 wl-clipboard，或使用 /attach <path>。")
    elif os.environ.get("DISPLAY"):
        if command_exists("xclip"):
            add(results, "OK", "X11 剪贴板", "检测到 DISPLAY，且 xclip 可用。")
        else:
            add(results, "WARN", "X11 剪贴板", "检测到 DISPLAY，但未找到 xclip。", "安装 xclip，或使用 /attach <path>。")
    else:
        add(results, "INFO", "剪贴板图片", "未检测到 DISPLAY/WAYLAND_DISPLAY，像纯 SSH/headless 环境无法使用剪贴板图片粘贴。", "使用 /attach <path> 添加图片或音频附件。")


def check_qq(results: list[Check]) -> None:
    if not is_truthy(env_value("QQ_ENABLE")):
        add(results, "INFO", "QQ/NapCat", "QQ_ENABLE 未启用；跳过 QQ transport 深度检查。")
        return

    if importlib.util.find_spec("websockets"):
        add(results, "OK", "QQ 依赖 websockets", "当前 Python 环境可以 import websockets。")
    else:
        add(results, "FAIL", "QQ 依赖 websockets", "当前 Python 环境缺少 websockets。", "运行 pip install -r requirements.txt 或 pip install websockets。")

    host = env_value("QQ_HOST", "127.0.0.1")
    port = env_value("QQ_PORT", "6199")
    token = env_value("QQ_ACCESS_TOKEN")
    add(results, "INFO", "QQ 监听地址", f"NapCat 应连接: ws://{host}:{port}/onebot/v11/ws")
    if host in {"0.0.0.0", "::"} and not token:
        add(results, "WARN", "QQ 访问令牌", "QQ_HOST 对外监听但 QQ_ACCESS_TOKEN 为空。", "跨机器或容器部署时建议配置 token。")

    sticker_dir = Path(env_value("CBAGENT_STICKER_DIR", "./assets/stickers"))
    if not sticker_dir.is_absolute():
        sticker_dir = PROJECT_ROOT / sticker_dir
    if sticker_dir.exists() and sticker_dir.is_dir():
        add(results, "OK", "表情包目录", f"目录存在: {sticker_dir}")
    else:
        add(results, "WARN", "表情包目录", f"目录不存在: {sticker_dir}", "send_message_asset(kind=sticker) 按名称查找表情包时会失败。")

    add(
        results,
        "INFO",
        "QQ 文件发送路径",
        "send_message_asset 会把本地文件路径交给 NapCat 的 OneBot action。",
        "请确保 cb-agent 与 NapCat 同机部署，或至少能访问同一份路径；不同容器/不同机器需要共享目录或后续接 HTTP 静态文件服务。",
    )


def print_results(results: list[Check]) -> int:
    order = {"FAIL": 0, "WARN": 1, "OK": 2, "INFO": 3}
    for item in sorted(results, key=lambda c: (order.get(c.level, 9), c.name)):
        print(safe_console(f"[{item.level}] {item.name}: {item.message}"))
        if item.tip:
            print(safe_console(f"       建议: {item.tip}"))

    fail_count = sum(1 for item in results if item.level == "FAIL")
    warn_count = sum(1 for item in results if item.level == "WARN")
    print()
    print(safe_console(f"汇总: {fail_count} 个失败，{warn_count} 个警告，{len(results)} 项检查。"))
    return 1 if fail_count else 0


def main() -> int:
    results: list[Check] = []
    check_commands(results)
    check_python(results)
    check_required_env(results)
    check_mcp(results)
    check_clipboard(results)
    check_qq(results)
    return print_results(results)


if __name__ == "__main__":
    raise SystemExit(main())
