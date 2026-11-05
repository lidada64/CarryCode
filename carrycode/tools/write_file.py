"""write_file 工具：整份写入（覆盖），父目录不存在就建。"""

from __future__ import annotations

from typing import Any

from .base import resolve_path, write_text

NAME = "write_file"

SCHEMA = {
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
}


def run(args: dict[str, Any]) -> tuple[str, bool]:
    path = str(args.get("path") or "").strip()
    if not path:
        return "缺少 path 参数。", True
    content = args.get("content")
    if not isinstance(content, str):
        return "缺少 content 参数（要写入的完整文本）。", True

    target = resolve_path(path)
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
        size = write_text(target, content, crlf)
    except OSError as error:
        return f"写不了 {path}：{error}", True

    # 行数和 read_file 的算法对齐：结尾的换行不算新的一行
    lines = 0 if not content else content.count("\n") + (0 if content.endswith("\n") else 1)
    return f"已写入 {path}：{lines} 行，{size} 字节。", False
