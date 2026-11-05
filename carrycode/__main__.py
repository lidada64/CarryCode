"""CarryCode 最小 agent loop（纯标准库）。

一轮任务：

    用户 → 模型 → stop_reason=tool_use → dispatch() 执行工具 → 结果回传 → 模型继续 → 直到 stop_reason 不是 tool_use

用法：

    .\\run-carrycode.cmd                      # 进对话框，一行一条任务
    python -m carrycode "看看这个目录"        # 一次性跑完就退出

    对话框里：/help 看命令，/new 清空历史，/exit 或 Ctrl-D 退出，Ctrl-C 中断当前轮

配置：先读仓库根的 .env，再用同名环境变量覆盖

    ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL

文件分工：

    __main__.py        入口：配置、系统提示、模型请求、回合循环、对话框
    tools/base.py      公共件：路径、文本读写、参数兜底、共享常量（不 import 任何东西）
    tools/tool.py      花名册 + dispatch（不认识任何具体工具）
    tools/__init__.py  装配：装哪几个工具、按什么顺序（唯一能改顺序的地方）
    tools/<name>.py    单个工具：三件套 NAME / SCHEMA / run

工具清单在模块加载时装一次，之后不再改。这样每次请求发出去的工具说明都一样，
模型服务端的前缀缓存才认得出来；要改顺序或改说明，等于让前缀失效，得当成一次
有代价的变更来做。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .tools import TOOL_SCHEMAS, dispatch

BASE_DIR = Path(__file__).resolve().parent.parent  # 仓库根，.env 在这里

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 8192

MAX_STEPS = 5  # 一回合最多 5 次模型请求：stop_reason 不是 tool_use 就停
REQUEST_TIMEOUT = 120  # 单次模型请求超时（秒）


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
2. 看文件用 read_file（带行号，一次没读完就用 offset 接着读），按名字找文件用 glob，
   按内容搜用 grep；不要用 bash 的 type / cat / dir / findstr 代替它们。
3. 改文件用 edit_file：把一段唯一出现的原文替换掉；只有整份重写才用 write_file。
4. 跑测试、git、安装依赖和其它命令用 bash，一次一条命令。
5. 工具结果里 is_error=true 表示这一步失败了，读内容再决定下一步；bash 看 exit_code。
6. 只有真的做完才给结论；没有执行过的步骤不要说成已完成。
7. 不要执行破坏性命令（删库、格式化、git push），除非用户明确要求。
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
        "tools": TOOL_SCHEMAS,
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
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        detail = _http_detail(error)
        raise RuntimeError(f"HTTP {error.code}：{detail}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"连不上 {url}：{error.reason}") from None
    except TimeoutError:
        raise RuntimeError(f"模型请求超时（{REQUEST_TIMEOUT}s）") from None


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


def _describe_call(call: dict[str, Any]) -> str:
    """打印一行给人看的调用说明。"""
    name = str(call.get("name") or "")
    args = call.get("input") or {}
    if name == "bash":
        return f"bash: {args.get('command', '')}"
    detail = json.dumps(args, ensure_ascii=False)
    if len(detail) > 120:
        detail = detail[:120] + "…"
    return f"{name}: {detail}"


# ---------------------------------------------------------------------------
# 循环：模型 ↔ 工具，直到模型不再调工具
# ---------------------------------------------------------------------------


def _run_steps(messages: list[dict[str, Any]], config: dict[str, str]) -> int:
    """在已有历史上跑一轮：模型 → 工具 → 模型 …，直到 stop_reason 不是 tool_use。"""
    for _ in range(MAX_STEPS):
        data = call_model(messages, config)
        blocks = [block for block in data.get("content") or [] if isinstance(block, dict)]
        tool_calls = [block for block in blocks if block.get("type") == "tool_use"]
        text = _text_of(blocks)
        stop_reason = data.get("stop_reason")

        if text:
            print(text)
            if stop_reason == "tool_use":
                print()

        # assistant 的原始 blocks（含 thinking 与 tool_use）原样入历史，
        # DeepSeek 的 Anthropic 兼容端点要求这样回传。
        messages.append({"role": "assistant", "content": blocks})

        # 边界只看 stop_reason：tool_use 表示「还要调工具」，其余都收尾。
        if stop_reason != "tool_use":
            if not text:
                print(f"（没有文本，stop_reason={stop_reason}）", file=sys.stderr)
                return 1
            if stop_reason == "max_tokens":
                print("（输出被 max_tokens 截断，本轮不算完成）", file=sys.stderr)
                return 1
            return 0

        if not tool_calls:
            print("（stop_reason=tool_use，但没有 tool_use 块）", file=sys.stderr)
            return 1

        results: list[dict[str, Any]] = []
        for call in tool_calls:
            print(f"→ {_describe_call(call)}")
            result = dispatch(call)
            mark = "失败" if result["is_error"] else "ok"
            print(f"← {mark} · {len(result['content'])} 字符")
            results.append(result)
        # 一轮里的所有 tool_result 必须放在同一条 user 消息里
        messages.append({"role": "user", "content": results})
        print()

    print(f"到 {MAX_STEPS} 步上限，停止。", file=sys.stderr)
    return 1


def run_turn(messages: list[dict[str, Any]], task: str, config: dict[str, str]) -> int:
    """把一条任务追加进历史再跑。

    中断或请求出错时，把这一轮追加的消息全撤掉：历史停在上一轮结束的位置，
    既不会留下「user 发了、assistant 没回」，也不会留下只调了工具没回结果的半截。
    """
    mark = len(messages)
    messages.append({"role": "user", "content": [{"type": "text", "text": task}]})
    try:
        return _run_steps(messages, config)
    except BaseException:
        del messages[mark:]
        raise


def run(task: str, config: dict[str, str]) -> int:
    """一次性：跑完就退出（退出码 0 完成 / 1 没跑完）。"""
    return run_turn([], task, config)


def _prompt() -> str:
    """输入提示符：控制台编码放不下 › 时退回 >。"""
    try:
        "› ".encode(sys.stdout.encoding or "utf-8")
    except (LookupError, UnicodeEncodeError):
        return "> "
    return "› "


def repl(config: dict[str, str]) -> int:
    """对话框：一行一条任务，历史一直留着，所以上下文能接着聊。

    历史是往下追加的，前面几轮的请求开头原样不动，
    模型服务端的前缀缓存就能接着用。
    """
    messages: list[dict[str, Any]] = []

    print(f"● {config['model']}  ·  {config['base_url']}")
    print("输入任务回车发送；/help 看命令，/exit 退出。")
    print()

    while True:
        try:
            line = input(_prompt()).strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("\n（想退出打 /exit，或按 Ctrl-D）")
            continue

        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return 0
        if line == "/help":
            print("  /help   这条帮助")
            print("  /new    清空历史，开一段新对话（上下文从头来）")
            print("  /exit   退出（Ctrl-D 也行）")
            print(f"  其它输入都当任务发给模型；每轮最多 {MAX_STEPS} 次模型请求，Ctrl-C 中断当前轮。")
            print()
            continue
        if line == "/new":
            messages.clear()
            print("（历史已清空，接着输入任务）")
            print()
            continue

        try:
            run_turn(messages, line, config)
        except KeyboardInterrupt:
            print("（已中断这一轮，历史回到中断前）", file=sys.stderr)
        except RuntimeError as error:
            print(f"✗ {error}", file=sys.stderr)
        print()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="carrycode",
        description="CarryCode - 最小 agent loop：6 个工具，带对话框",
    )
    parser.add_argument("prompt", nargs="*", help="要交给模型的任务；不写就进对话框")
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

    config = load_config()
    if not config["api_key"]:
        print("缺少 ANTHROPIC_API_KEY：写进 CarryCode\\.env，或设为环境变量。", file=sys.stderr)
        return 2

    if not task:
        # 没给任务就进对话框
        try:
            return repl(config)
        except KeyboardInterrupt:
            print()
            return 0

    print(f"● {config['model']}  ·  {config['base_url']}")
    print()
    try:
        return run(task, config)
    except KeyboardInterrupt:
        print("\n（已中断）", file=sys.stderr)
        return 130
    except RuntimeError as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
