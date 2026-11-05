"""bash 工具：在工作区执行一条命令，返回退出码、stdout、stderr。"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from . import commands
from .base import MAX_OUTPUT_CHARS

NAME = "bash"
RISK = "exec"  # 能执行任意命令：有副作用；具体多危险要看这一次的命令（阶段 1 由 inspect 细化）
COMMAND_TIMEOUT = 120  # 单条命令超时（秒）

SCHEMA = {
    "description": (
        "在工作区执行一条 shell 命令，返回退出码、stdout、stderr。"
        "用于跑测试、执行 git、安装依赖等；看文件、找文件、搜内容优先用 read_file / glob / grep。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "完整命令行，例如 dir /b *.py 或 python -m unittest -q",
            }
        },
        "required": ["command"],
    },
}


def run(args: dict[str, Any]) -> tuple[str, bool]:
    command = str(args.get("command") or "")
    if not command.strip():
        return "空命令：没有可执行的内容。", True

    env = {**os.environ, "PYTHONUTF8": "1"}
    try:
        completed = subprocess.run(
            command,
            shell=True,  # Windows 走 cmd，其它平台走 /bin/sh
            cwd=os.getcwd(),
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"命令超时（{COMMAND_TIMEOUT}s）：{command}", True

    output = f"exit_code: {completed.returncode}"
    if completed.stdout.strip():
        output += f"\nstdout:\n{completed.stdout.strip()}"
    if completed.stderr.strip():
        output += f"\nstderr:\n{completed.stderr.strip()}"
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + f"\n…[truncated {len(output) - MAX_OUTPUT_CHARS} chars]"
    return output, completed.returncode != 0


def inspect(args: dict[str, Any]) -> tuple[str, list[str]]:
    """这一次到底多危险：返回 (档位, 审批块正文)。

    档位可能是 read / exec / irreversible / forbidden。
    看不懂的命令不猜，按 exec 走常规审批——猜错方向可能是"把危险的说轻"。
    """
    command = str(args.get("command") or "")
    verdict = commands.classify(command)
    if verdict is not None:
        return verdict.level, verdict.lines
    return "exec", [f"命令: {command.strip()}"]
