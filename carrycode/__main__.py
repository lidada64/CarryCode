"""CarryCode 最小 agent loop（纯标准库，全部实现就这一个文件）。

一轮任务：

    用户 → 模型 → 模型要调 bash → 本机执行 → 结果回传 → 模型继续 → 直到它不再调工具

用法：

    python -m carrycode "用 bash 统计当前目录下 .py 文件的数量"
    .\\run-carrycode.cmd "..."

配置：先读仓库根的 .env，再用同名环境变量覆盖

    ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL

对应 minicode 的三个概念，这里压成一个文件：

    anthropic_adapter.py   请求与解析        → call_model()
    tooling.py + tools/    工具定义与执行    → TOOLS + run_bash()
    agent_loop.py          回合循环与停止    → run()
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__

BASE_DIR = Path(__file__).resolve().parent.parent  # 仓库根，.env 在这里

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 8192

MAX_STEPS = 20  # 一回合最多几次模型请求，防止死循环
COMMAND_TIMEOUT = 120  # 单条命令超时（秒）
OUTPUT_LIMIT = 12_000  # 回给模型的单条输出上限（字符）

TOOLS: list[dict[str, Any]] = [
    {
        "name": "bash",
        "description": (
            "在工作区执行一条 shell 命令，返回退出码、stdout、stderr。"
            "用于看目录、搜索、跑测试、执行 git 等。"
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
]


# ---------------------------------------------------------------------------
# 配置：.env + 环境变量（环境变量优先）
# ---------------------------------------------------------------------------


def load_config() -> dict[str, str]:
    values: dict[str, str] = {}
    dotenv = BASE_DIR / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")

    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "CARRYCODE_MODEL"):
        if os.environ.get(key):
            values[key] = os.environ[key]

    return {
        "api_key": values.get("ANTHROPIC_API_KEY", ""),
        "base_url": values.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL,
        "model": values.get("ANTHROPIC_MODEL") or values.get("CARRYCODE_MODEL") or DEFAULT_MODEL,
    }


def system_prompt() -> str:
    shell = "cmd" if os.name == "nt" else "sh"
    return f"""你是 CarryCode，一个跑在用户本机终端里的编程助手。

当前环境：
- 工作区：{os.getcwd()}
- 系统：{os.name}（命令通过 {shell} 执行）

规则：
1. 与用户交流用中文；命令、路径、代码、文件内容保持原样，不要翻译。
2. 需要查看文件、跑测试或执行命令时调用 bash 工具，一次一条命令。
3. 工具结果里的 exit_code 非 0 表示命令失败，读 stderr 再决定下一步。
4. 只有真的做完才给结论；没有执行过的步骤不要说成已完成。
5. 不要执行破坏性命令（删库、格式化、git push），除非用户明确要求。
"""


# ---------------------------------------------------------------------------
# 模型：一次 Messages 请求
# ---------------------------------------------------------------------------


def call_model(messages: list[dict[str, Any]], config: dict[str, str]) -> dict[str, Any]:
    base = config["base_url"].rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = f"{base}/v1/messages"

    body = {
        "model": config["model"],
        "max_tokens": MAX_TOKENS,
        "system": system_prompt(),
        "messages": messages,
        "tools": TOOLS,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": config["api_key"],
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=COMMAND_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        detail = _http_detail(error)
        raise RuntimeError(f"HTTP {error.code}：{detail}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"连不上 {url}：{error.reason}") from None
    except TimeoutError:
        raise RuntimeError(f"模型请求超时（{COMMAND_TIMEOUT}s）") from None


def _http_detail(error: urllib.error.HTTPError) -> str:
    raw = error.read().decode("utf-8", "replace") if error.fp else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        inner = payload.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("message"), str):
            return inner["message"]
        if isinstance(inner, str):
            return inner
    return raw.strip()[:300] or "(no body)"


def _text_of(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        block["text"]
        for block in blocks
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    ).strip()


# ---------------------------------------------------------------------------
# 工具：真正执行命令
# ---------------------------------------------------------------------------


def run_bash(command: str) -> tuple[str, int]:
    """执行一条命令，返回 (回给模型的文本, 退出码)。"""
    if not command.strip():
        return "Empty command: nothing to run.", -1

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
        return f"Command timed out after {COMMAND_TIMEOUT}s: {command}", -1

    output = f"exit_code: {completed.returncode}"
    if completed.stdout.strip():
        output += f"\nstdout:\n{completed.stdout.strip()}"
    if completed.stderr.strip():
        output += f"\nstderr:\n{completed.stderr.strip()}"
    if len(output) > OUTPUT_LIMIT:
        output = output[:OUTPUT_LIMIT] + f"\n…[truncated {len(output) - OUTPUT_LIMIT} chars]"
    return output, completed.returncode


# ---------------------------------------------------------------------------
# 循环：模型 ↔ 工具，直到模型不再调工具
# ---------------------------------------------------------------------------


def run(task: str, config: dict[str, str]) -> int:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": task}]}
    ]

    for _ in range(MAX_STEPS):
        data = call_model(messages, config)
        blocks = [block for block in data.get("content") or [] if isinstance(block, dict)]
        tool_calls = [block for block in blocks if block.get("type") == "tool_use"]
        text = _text_of(blocks)

        if text:
            print(text)
            if tool_calls:
                print()

        # assistant 的原始 blocks（含 thinking 与 tool_use）原样入历史，
        # DeepSeek 的 Anthropic 兼容端点要求这样回传。
        messages.append({"role": "assistant", "content": blocks})

        if not tool_calls:
            if not text:
                print(f"（模型没有返回文本，stop_reason={data.get('stop_reason')}）", file=sys.stderr)
                return 1
            return 0

        results: list[dict[str, Any]] = []
        for call in tool_calls:
            command = str((call.get("input") or {}).get("command", "")).strip()
            print(f"→ bash: {command}")
            output, code = run_bash(command)
            print(f"← exit_code {code} · {len(output)} 字符")
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.get("id", ""),
                    "content": output,
                    "is_error": code != 0,
                }
            )
        # 一轮里的所有 tool_result 必须放在同一条 user 消息里
        messages.append({"role": "user", "content": results})
        print()

    print(f"到 {MAX_STEPS} 步上限，停止。", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="carrycode",
        description="CarryCode - 最小 agent loop：模型可以调用 bash",
    )
    parser.add_argument("prompt", nargs="*", help="要交给模型的任务")
    parser.add_argument("--version", action="store_true", help="打印版本")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")

    if args.version:
        print(f"CarryCode {__version__}")
        return 0

    task = " ".join(args.prompt).strip()
    if not task:
        build_parser().print_help()
        return 2

    config = load_config()
    if not config["api_key"]:
        print("缺少 ANTHROPIC_API_KEY：写进 CarryCode\\.env，或设为环境变量。", file=sys.stderr)
        return 2

    print(f"● {config['model']}  ·  {config['base_url']}")
    print()
    try:
        return run(task, config)
    except RuntimeError as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
