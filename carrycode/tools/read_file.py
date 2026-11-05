"""read_file 工具：读文件的一段，返回带行号的内容。"""

from __future__ import annotations

from typing import Any

from .base import as_int, read_text, resolve_path

NAME = "read_file"
RISK = "read"  # 只读，不改任何东西
PATHS = ("path",)  # 这个入参受工作区约束
READ_LIMIT = 200  # 默认读多少行
MAX_READ_BYTES = 10 * 1024 * 1024  # 超过 10MB 的文件不让整份读
MAX_LINE_CHARS = 2_000  # 单行太长就截断，免得一行吃掉整个输出

SCHEMA = {
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
}


def run(args: dict[str, Any]) -> tuple[str, bool]:
    path = str(args.get("path") or "").strip()
    if not path:
        return "缺少 path 参数。", True

    target = resolve_path(path)
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
            "先用 grep 定位到行号，再用 offset 读那一小段。",
            True,
        )

    try:
        text, _ = read_text(target)
    except OSError as error:
        return f"读不了 {path}：{error}", True

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # 结尾的换行不算一整行
    total = len(lines)
    if total == 0:
        return f"{path}：空文件。", False

    offset = max(1, as_int(args.get("offset"), 1))
    limit = max(1, as_int(args.get("limit"), READ_LIMIT))
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
