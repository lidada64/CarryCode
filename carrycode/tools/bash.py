"""bash 工具：在工作区执行一条命令，返回退出码、stdout、stderr。"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from .base import MAX_OUTPUT_CHARS

NAME = "bash"
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
