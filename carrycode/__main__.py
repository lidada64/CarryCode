"""CarryCode 最小 agent loop（纯标准库，全部实现就这一个文件）。

一轮任务：

    用户 → 模型 → stop_reason=tool_use → dispatch() 执行工具 → 结果回传 → 模型继续 → 直到 stop_reason 不是 tool_use

用法：

    .\\run-carrycode.cmd                      # 进对话框，一行一条任务
    python -m carrycode "看看这个目录"        # 一次性跑完就退出

    对话框里：/help 看命令，/new 清空历史，/exit 或 Ctrl-D 退出，Ctrl-C 中断当前轮

配置：先读仓库根的 .env，再用同名环境变量覆盖

    ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL

对应 minicode 的三个概念，这里压成一个文件：

    anthropic_adapter.py   请求与解析        → call_model()
    tooling.py + tools/    工具定义与执行    → TOOLS 字典 + dispatch()
    agent_loop.py          回合循环与停止    → run()

工具表是模块级常量：启动时建一次，之后不再改。这样一来每次请求发出去的
工具说明都一样，模型服务端的前缀缓存才认得出来。
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

MAX_STEPS = 5  # 一回合最多 5 次模型请求：stop_reason 不是 tool_use 就停
COMMAND_TIMEOUT = 120  # 单条命令超时（秒）
OUTPUT_LIMIT = 12_000  # 回给模型的单条输出上限（字符）

READ_LIMIT = 200  # read_file 默认读多少行
MAX_READ_BYTES = 10 * 1024 * 1024  # 超过 10MB 的文件不让 read_file 整份读
MAX_LINE_CHARS = 2_000  # 单行太长就截断，免得一行吃掉整个输出
GLOB_LIMIT = 100  # glob 默认最多列多少个文件
GLOB_SKIP_DIRS = {  # glob 默认跳过：体积大、可再生，列出来只会淹没有用信息
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}


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
2. 看文件用 read_file（带行号，一次没读完就用 offset 接着读），找文件用 glob；
   不要用 bash 的 type / cat / dir 代替它们。
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
# 工具用的几个小函数：路径、读写、整数兜底
# ---------------------------------------------------------------------------


def _resolve(path: str) -> Path:
    """把模型给的路径换成绝对路径：相对路径按当前工作目录算。"""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    return target


def _read_text(target: Path) -> tuple[str, bool]:
    """读文件，返回 (内容, 原文件是不是 CRLF 换行)。

    把 CRLF 统一成 LF 再交给模型匹配，写回去时再还原，
    这样模型给的 old_string 用普通换行也能对上，文件行尾风格也不会被改。
    """
    text = target.read_bytes().decode("utf-8", "replace")
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    return text, crlf


def _write_text(target: Path, text: str, crlf: bool) -> int:
    """写文件（父目录自动创建），返回写入的字节数。按原文件风格还原换行。"""
    if crlf:
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return len(data)


def _as_int(value: Any, default: int) -> int:
    """模型可能把数字写成字符串，转不过去就用默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def run_bash_tool(args: dict[str, Any]) -> tuple[str, bool]:
    text, code = run_bash(str(args.get("command") or ""))
    return text, code != 0


def run_read_file(args: dict[str, Any]) -> tuple[str, bool]:
    """读文件的一段，返回带行号的内容。"""
    path = str(args.get("path") or "").strip()
    if not path:
        return "缺少 path 参数。", True

    target = _resolve(path)
    if not target.exists():
        return f"文件不存在：{path}", True
    if target.is_dir():
        return f"{path} 是目录，不是文件。用 glob 看它下面有什么。", True

    try:
        size = target.stat().st_size
    except OSError as error:
        return f"读不了 {path}：{error}", True
    if size > MAX_READ_BYTES:
        return (
            f"{path} 有 {size // 1024 // 1024} MB，太大，不适合整份读。"
            "先用 bash 的 findstr / grep 定位到行号，再用 offset 读那一小段。",
            True,
        )

    try:
        text, _ = _read_text(target)
    except OSError as error:
        return f"读不了 {path}：{error}", True

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # 结尾的换行不算一整行
    total = len(lines)
    if total == 0:
        return f"{path}：空文件。", False

    offset = max(1, _as_int(args.get("offset"), 1))
    limit = max(1, _as_int(args.get("limit"), READ_LIMIT))
    if offset > total:
        return f"{path} 只有 {total} 行，offset={offset} 超出范围。", True

    start = offset - 1
    end = min(total, start + limit)
    body: list[str] = []
    for number in range(start, end):
        line = lines[number]
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + f"…[{len(line) - MAX_LINE_CHARS} 字符已截断]"
        body.append(f"{number + 1:>6}→{line}")

    output = [f"{path}（共 {total} 行，显示 {start + 1}-{end}）", *body]
    if end < total:
        output.append(f"…还有 {total - end} 行，接着读用 offset={end + 1}")
    return "\n".join(output), False


def run_write_file(args: dict[str, Any]) -> tuple[str, bool]:
    """整份写入（覆盖），父目录不存在就建。"""
    path = str(args.get("path") or "").strip()
    if not path:
        return "缺少 path 参数。", True
    content = args.get("content")
    if not isinstance(content, str):
        return "缺少 content 参数（要写入的完整文本）。", True

    target = _resolve(path)
    if target.is_dir():
        return f"{path} 是目录，不能写入。", True

    # 覆盖已有文件时沿用它的行尾风格：CRLF 的文件写回还是 CRLF
    crlf = False
    if target.exists():
        try:
            with open(target, "rb") as handle:
                crlf = b"\r\n" in handle.read(65_536)
        except OSError:
            crlf = False

    try:
        size = _write_text(target, content, crlf)
    except OSError as error:
        return f"写不了 {path}：{error}", True

    # 行数和 read_file 的算法对齐：结尾的换行不算新的一行
    lines = 0 if not content else content.count("\n") + (0 if content.endswith("\n") else 1)
    return f"已写入 {path}：{lines} 行，{size} 字节。", False


def run_edit_file(args: dict[str, Any]) -> tuple[str, bool]:
    """把一段唯一出现的原文替换掉；不唯一就报错，不去猜。"""
    path = str(args.get("path") or "").strip()
    if not path:
        return "缺少 path 参数。", True
    old = args.get("old_string")
    new = args.get("new_string")
    if not isinstance(old, str) or not old:
        return "缺少 old_string（要替换的原文，不能是空字符串）。", True
    if not isinstance(new, str):
        return "缺少 new_string（替换成什么，可以是空字符串）。", True

    target = _resolve(path)
    if not target.exists():
        return f"文件不存在：{path}", True
    if target.is_dir():
        return f"{path} 是目录，不能编辑。", True

    try:
        text, crlf = _read_text(target)
    except OSError as error:
        return f"读不了 {path}：{error}", True

    occurrences = text.count(old)
    if occurrences == 0:
        return (
            f"在 {path} 里找不到 old_string。注意缩进、空格、换行要和文件完全一致；"
            "也可能是这段内容已经被改过或删掉了，先 read_file 看一眼。",
            True,
        )

    replace_all = bool(args.get("replace_all"))
    if occurrences > 1 and not replace_all:
        return (
            f"old_string 在 {path} 里出现了 {occurrences} 次，无法确定改哪一处。"
            "带上更多上下文让它唯一，或者传 replace_all=true 全部替换。",
            True,
        )

    first = text.index(old)
    line_number = text.count("\n", 0, first) + 1
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)

    try:
        _write_text(target, updated, crlf)
    except OSError as error:
        return f"写不了 {path}：{error}", True

    changed = occurrences if replace_all else 1
    return f"已修改 {path}：替换 {changed} 处（起始第 {line_number} 行）。", False


def run_glob(args: dict[str, Any]) -> tuple[str, bool]:
    """按 glob 模式找文件，返回排序后的相对路径。"""
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return "缺少 pattern 参数，例如 **/*.py", True

    limit = max(1, _as_int(args.get("limit"), GLOB_LIMIT))
    root = Path.cwd()

    try:
        found = [
            path
            for path in root.glob(pattern)
            if path.is_file()
            and not (set(path.relative_to(root).parts[:-1]) & GLOB_SKIP_DIRS)
        ]
    except (OSError, ValueError) as error:
        return f"pattern 无效：{pattern}（{error}）", True

    if not found:
        return f"没有匹配 {pattern} 的文件（当前目录 {root}）。", False

    paths = sorted(path.relative_to(root).as_posix() for path in found)
    output = [*paths[:limit]]
    if len(paths) > limit:
        output.append(f"…共 {len(paths)} 个，只列出前 {limit} 个。缩小 pattern 或调大 limit。")
    return "\n".join(output), False


# ---------------------------------------------------------------------------
# 工具表与分发：名字 → 说明 + 函数
#
# 说明（schema）就是要发给模型的那部分，必须是常量：不含时间、路径、随机值。
# 要停用某个工具，就在 dispatch() 里拒绝并返回结果，不要从这张表里删。
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict[str, Any]] = {
    "bash": {
        "schema": {
            "description": (
                "在工作区执行一条 shell 命令，返回退出码、stdout、stderr。"
                "用于跑测试、执行 git、安装依赖等；看文件和找文件优先用 read_file / glob。"
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
        },
        "run": run_bash_tool,
    },
    "read_file": {
        "schema": {
            "description": (
                "读出文件的一段内容，每行前面带行号。改文件之前先读一遍；"
                "文件很长时用 offset 一行一段接着读。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径，相对路径按当前工作目录算",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "从第几行开始读，从 1 算，默认 1",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多读多少行，默认 200",
                    },
                },
                "required": ["path"],
            },
        },
        "run": run_read_file,
    },
    "write_file": {
        "schema": {
            "description": (
                "把完整内容写入文件，覆盖原有内容，父目录会自动创建。"
                "只改文件里的一小段请用 edit_file，不要整份重写。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径，相对路径按当前工作目录算",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的完整文本",
                    },
                },
                "required": ["path", "content"],
            },
        },
        "run": run_write_file,
    },
    "edit_file": {
        "schema": {
            "description": (
                "把文件里一段原文替换成新内容。old_string 必须在文件里唯一出现，"
                "并且和文件里的字符完全一致（含缩进、空格、换行）。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径，相对路径按当前工作目录算",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "要被替换的原文，最好带上几行上下文保证唯一",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "替换成的新内容，可以是空字符串（等于删掉这段）",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "上面那段出现多次时是否全部替换，默认 false",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
        "run": run_edit_file,
    },
    "glob": {
        "schema": {
            "description": (
                "按 glob 模式在当前工作区找文件，返回按路径排序的相对路径。"
                "例如 **/*.py、src/**/*.ts；会自动跳过 .git、node_modules 这类目录。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "glob 模式，例如 **/*.py",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多列多少个文件，默认 100",
                    },
                },
                "required": ["pattern"],
            },
        },
        "run": run_glob,
    },
}

# 每次请求原样发出去的那份：顺序就是上面字典的顺序，模块加载时算一次。
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"name": name, **spec["schema"]} for name, spec in TOOLS.items()
]


def dispatch(call: dict[str, Any]) -> dict[str, Any]:
    """把模型的一个 tool_use 块变成 tool_result 块。

    未知工具、参数错、函数抛异常都变成结果返回，不往上抛：
    循环不该因为一次调用失败就断掉，模型看到错误自己会调整。
    """
    name = str(call.get("name") or "")
    spec = TOOLS.get(name)
    if spec is None:
        text, is_error = f"未知工具：{name}", True
    else:
        raw_args = call.get("input")
        args = raw_args if isinstance(raw_args, dict) else {}
        try:
            text, is_error = spec["run"](args)
        except Exception as error:  # 工具自己的 bug 也要让模型看到
            text, is_error = f"{type(error).__name__}: {error}", True

    if len(text) > OUTPUT_LIMIT:
        text = text[:OUTPUT_LIMIT] + f"\n…[truncated {len(text) - OUTPUT_LIMIT} chars]"

    return {
        "type": "tool_result",
        "tool_use_id": call.get("id", ""),
        "content": text,
        "is_error": is_error,
    }


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
        description="CarryCode - 最小 agent loop：5 个工具，带对话框",
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
