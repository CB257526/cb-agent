"""Skill 脚本执行器

负责执行 Skill 捆绑的 Python 脚本，通过子进程运行。
"""

import json
import sys
import subprocess
from pathlib import Path


class SkillExecutor:
    """Skill 脚本执行器

    通过 subprocess 执行 Skill 捆绑的 Python 脚本。
    """

    def __init__(self, timeout: int = 60):
        """初始化执行器

        Args:
            timeout: 脚本执行超时时间（秒）
        """
        self.timeout = timeout

    def run_script(self, script_path: Path, args: list = None,
                   stdin_data: str = None) -> str:
        """执行 Python 脚本并返回 stdout

        Args:
            script_path: 脚本文件的绝对路径
            args: 命令行参数列表
            stdin_data: 通过 stdin 传入的数据

        Returns:
            脚本的 stdout 输出，或错误信息
        """
        script_path = Path(script_path)
        if not script_path.is_file():
            return f"[ERROR] 脚本文件不存在: {script_path}"

        cmd = [sys.executable, str(script_path)]
        if args:
            cmd.extend(args)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self.timeout,
                cwd=str(script_path.parent),
                input=stdin_data,
                text=True,
            )

            output_parts = []
            if result.stdout:
                output_parts.append(result.stdout)
            if result.stderr:
                output_parts.append(f"[stderr]\n{result.stderr}")

            if result.returncode != 0:
                output_parts.append(f"[退出码: {result.returncode}]")

            return "\n".join(output_parts) if output_parts else "(无输出)"

        except subprocess.TimeoutExpired:
            return f"[ERROR] 脚本执行超时（{self.timeout}秒）: {script_path.name}"
        except FileNotFoundError:
            return f"[ERROR] Python 解释器未找到: {sys.executable}"
        except Exception as e:
            return f"[ERROR] 脚本执行失败: {e}"

    def run_script_with_context(self, script_path: Path, context: dict,
                                 args: list = None) -> str:
        """执行脚本，通过 stdin 传入 JSON 上下文

        Args:
            script_path: 脚本文件的绝对路径
            context: 要传入的上下文数据（将序列化为 JSON）
            args: 命令行参数列表

        Returns:
            脚本的 stdout 输出
        """
        try:
            stdin_data = json.dumps(context, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            return f"[ERROR] 上下文序列化失败: {e}"

        return self.run_script(script_path, args=args, stdin_data=stdin_data)
