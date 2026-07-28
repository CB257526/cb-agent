from __future__ import annotations

import json
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.mcp_tools.client import MCPClient
from tools.mcp_tools.mcptool import MCPTool
from tools.mcp_tools.mcptools_add import load_mcp_server_configs
from agent.cancel import CancellationReason, ToolCancelledError


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


def _write_echo_server(path: Path, transport: str, port: Optional[int] = None, route: str = "/mcp") -> None:
    """写入一个最小 MCP server，用于真实连接 stdio/http/sse。"""
    if transport == "stdio":
        run_line = 'mcp.run(transport="stdio", show_banner=False)'
    else:
        run_line = (
            f'mcp.run(transport="{transport}", host="127.0.0.1", '
            f'port={port}, path="{route}", show_banner=False)'
        )

    path.write_text(textwrap.dedent(f"""
        from fastmcp import FastMCP

        mcp = FastMCP("{transport}-test")

        @mcp.tool()
        def echo(text: str) -> str:
            return "{transport}:" + text

        if __name__ == "__main__":
            {run_line}
    """), encoding="utf-8")


@contextmanager
def _running_http_server(script: Path, port: int) -> Iterator[None]:
    """启动 HTTP/SSE MCP server 子进程，并在测试结束时清理。"""
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=1)
                raise RuntimeError(
                    f"MCP server exited early: code={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise TimeoutError(f"MCP server did not listen on port {port}")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


class TestMCPServerConfig(unittest.TestCase):
    def test_tool_timeout_is_preserved_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "mcp.json"
            config_path.write_text(json.dumps({
                "mcpServers": {
                    "timed": {
                        "command": "python",
                        "args": ["server.py"],
                        "tool_timeout_sec": 3.5,
                    },
                }
            }), encoding="utf-8")
            server = load_mcp_server_configs(str(config_path))[0]
            self.assertEqual(server["tool_timeout_sec"], 3.5)

            config_path.write_text(json.dumps({
                "mcpServers": {
                    "broken": {
                        "command": "python",
                        "tool_timeout_sec": True,
                    },
                }
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "tool_timeout_sec"):
                load_mcp_server_configs(str(config_path))

            config_path.write_text(json.dumps({
                "mcpServers": {
                    "broken": {
                        "command": "python",
                        "tool_timeout_sec": float("nan"),
                    },
                }
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "有限数字"):
                load_mcp_server_configs(str(config_path))

    def test_load_mcp_server_configs_supports_stdio_http_and_sse(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"TOKEN": "secret-token"}):
            config_path = Path(td) / "mcp.json"
            config_path.write_text(json.dumps({
                "mcpServers": {
                    "local": {
                        "command": "python",
                        "args": ["server.py"],
                        "env": {"LOCAL_TOKEN": "${TOKEN}"},
                    },
                    "github": {
                        "type": "http",
                        "url": "https://api.githubcopilot.com/mcp/",
                        "headers": {"Authorization": "Bearer ${TOKEN}"},
                    },
                    "legacy-sse": {
                        "transport": "sse",
                        "url": "http://127.0.0.1:9999/sse",
                    },
                }
            }), encoding="utf-8")

            servers = {item["name"]: item for item in load_mcp_server_configs(str(config_path))}

        self.assertEqual(servers["local"]["transport"], "stdio")
        self.assertEqual(servers["local"]["server_command"], ["python", "server.py"])
        self.assertEqual(servers["local"]["env"], {"LOCAL_TOKEN": "secret-token"})
        self.assertEqual(servers["github"]["transport"], "http")
        self.assertEqual(servers["github"]["headers"], {"Authorization": "Bearer secret-token"})
        self.assertEqual(servers["legacy-sse"]["transport"], "sse")

    def test_stdio_config_requires_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "mcp.json"
            config_path.write_text(json.dumps({
                "mcpServers": {
                    "broken": {"type": "stdio", "args": ["server.py"]},
                }
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "command"):
                load_mcp_server_configs(str(config_path))

    def test_official_servers_request_init_headers_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"TOKEN": "secret-token"}):
            config_path = Path(td) / "mcp.json"
            config_path.write_text(json.dumps({
                "servers": {
                    "github": {
                        "url": "https://api.githubcopilot.com/mcp/",
                        "requestInit": {
                            "headers": {
                                "Authorization": "Bearer ${TOKEN}",
                            },
                        },
                    },
                }
            }), encoding="utf-8")

            servers = {item["name"]: item for item in load_mcp_server_configs(str(config_path))}

        self.assertEqual(servers["github"]["transport"], "http")
        self.assertEqual(servers["github"]["headers"], {"Authorization": "Bearer secret-token"})

    def test_unresolved_env_fails_before_remote_connection(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            config_path = Path(td) / "mcp.json"
            config_path.write_text(json.dumps({
                "mcpServers": {
                    "github": {
                        "type": "http",
                        "url": "https://api.githubcopilot.com/mcp/",
                        "headers": {"Authorization": "Bearer ${MISSING_GITHUB_PAT}"},
                    },
                }
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "MISSING_GITHUB_PAT"):
                load_mcp_server_configs(str(config_path))

    def test_collect_errors_keeps_other_mcp_servers_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            config_path = Path(td) / "mcp.json"
            config_path.write_text(json.dumps({
                "mcpServers": {
                    "broken-github": {
                        "type": "http",
                        "url": "https://api.githubcopilot.com/mcp/",
                        "headers": {"Authorization": "Bearer ${MISSING_GITHUB_PAT}"},
                    },
                    "local": {
                        "command": "python",
                        "args": ["server.py"],
                    },
                }
            }), encoding="utf-8")

            servers = {item["name"]: item for item in load_mcp_server_configs(str(config_path), collect_errors=True)}

        self.assertIn("MISSING_GITHUB_PAT", servers["broken-github"]["config_error"])
        self.assertEqual(servers["broken-github"]["transport"], "http")
        self.assertEqual(servers["local"]["transport"], "stdio")
        self.assertEqual(servers["local"]["server_command"], ["python", "server.py"])

    def test_client_rejects_direct_config_with_unresolved_env(self) -> None:
        with self.assertRaisesRegex(ValueError, "MISSING_TOKEN"):
            MCPClient({
                "name": "direct-http",
                "transport": "http",
                "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer ${MISSING_TOKEN}"},
            })


class TestMCPToolTransports(unittest.TestCase):
    def test_memory_mcp_hanging_tool_obeys_deadline(self) -> None:
        """挂起 MCP coroutine 必须按工具 deadline 取消，不能无界等待。"""
        from fastmcp import FastMCP

        server = FastMCP("timeout-test")

        @server.tool()
        async def hang() -> str:
            await asyncio.Event().wait()
            return "unreachable"

        tool = MCPTool(
            name="timeout",
            server=server,
            server_config={"tool_timeout_sec": 0.05},
            strict_discovery=True,
        )
        started = time.perf_counter()
        with self.assertRaises(ToolCancelledError) as caught:
            tool.get_expanded_tools()[0].run({})

        self.assertLess(time.perf_counter() - started, 2)
        self.assertEqual(caught.exception.reason, CancellationReason.TOOL_TIMEOUT)

    def _assert_wrapped_echo(self, name: str, config: Dict[str, object], expected: str) -> None:
        tool = MCPTool(name=name, server_config=config, strict_discovery=True)
        try:
            expanded = tool.get_expanded_tools()
            self.assertEqual([item.name for item in expanded], [f"{name}_echo"])
            result = expanded[0].run({"text": "hi"})
            self.assertIn(expected, result)
        finally:
            tool.close()

    def test_stdio_mcp_tool_discovers_and_calls_wrapped_tool(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "stdio_server.py"
            _write_echo_server(script, "stdio")

            self._assert_wrapped_echo(
                "stdio",
                {"transport": "stdio", "command": sys.executable, "args": [str(script)]},
                "stdio:hi",
            )

    def test_http_mcp_tool_discovers_and_calls_wrapped_tool(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            port = _free_port()
            script = Path(td) / "http_server.py"
            _write_echo_server(script, "http", port=port, route="/mcp")

            with _running_http_server(script, port):
                self._assert_wrapped_echo(
                    "http",
                    {"transport": "http", "url": f"http://127.0.0.1:{port}/mcp"},
                    "http:hi",
                )

    def test_sse_mcp_tool_discovers_and_calls_wrapped_tool(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            port = _free_port()
            script = Path(td) / "sse_server.py"
            _write_echo_server(script, "sse", port=port, route="/sse")

            with _running_http_server(script, port):
                self._assert_wrapped_echo(
                    "sse",
                    {"transport": "sse", "url": f"http://127.0.0.1:{port}/sse"},
                    "sse:hi",
                )


if __name__ == "__main__":
    unittest.main()
