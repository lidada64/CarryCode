"""glob 工具：按模式找文件，返回按路径排序的相对路径。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import SKIP_DIRS, as_int

NAME = "glob"
RISK = "read"  # 只列文件名，不读内容
GLOB_LIMIT = 100  # 默认最多列多少个文件

SCHEMA = {
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
}


def run(args: dict[str, Any]) -> tuple[str, bool]:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return "缺少 pattern 参数，例如 **/*.py", True

    limit = max(1, as_int(args.get("limit"), GLOB_LIMIT))
    root = Path.cwd()

    try:
        found = [
            path
            for path in root.glob(pattern)
            if path.is_file()
            and not (set(path.relative_to(root).parts[:-1]) & SKIP_DIRS)
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
