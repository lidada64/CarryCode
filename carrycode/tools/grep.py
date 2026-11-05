"""grep 工具：在工作区里按正则搜内容，返回「路径:行号: 内容」。"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from .base import SKIP_DIRS, as_int, resolve_path, secret_name_reason

NAME = "grep"
RISK = "read"  # 只读，不改任何东西
PATHS = ("path",)  # 这个入参受工作区约束
GREP_LIMIT = 100  # 默认最多返回多少条
MAX_FILE_BYTES = 2 * 1024 * 1024  # 单个文件超过 2MB 不搜
MAX_LINE_CHARS = 300  # 命中的行太长就截断
BINARY_PROBE_BYTES = 4_096  # 开头这些字节里有 \x00 就当二进制

SCHEMA = {
    "description": (
        "在工作区里按正则表达式搜索文件内容，返回「路径:行号: 内容」，按路径和行号排序。"
        "例如 read_file\\(、TODO|FIXME；会自动跳过 .git、node_modules 这类目录和二进制文件。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Python 正则表达式，例如 read_file\\( 或 TODO|FIXME",
            },
            "path": {
                "type": "string",
                "description": "从哪个目录开始搜，默认当前工作目录",
            },
            "glob": {
                "type": "string",
                "description": "只搜文件名匹配的文件，例如 *.py；默认所有文本文件",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回多少条，默认 100",
            },
        },
        "required": ["pattern"],
    },
}


def run(args: dict[str, Any]) -> tuple[str, bool]:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return "缺少 pattern 参数（正则表达式）。", True
    try:
        regex = re.compile(pattern)
    except re.error as error:
        return f"正则写错了：{pattern}（{error}）", True

    root = resolve_path(str(args.get("path") or "."))
    if not root.exists():
        return f"路径不存在：{root}", True
    if not root.is_dir():
        return "path 要是目录；只想看一个文件用 read_file。", True

    name_filter = str(args.get("glob") or "").strip()
    limit = max(1, as_int(args.get("limit"), GREP_LIMIT))

    hits: list[tuple[str, int, str]] = []
    scanned = 0
    skipped_binary = 0
    skipped_secret = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for filename in sorted(filenames):
            if name_filter and not fnmatch.fnmatch(filename, name_filter):
                continue
            # 禁区文件不当成搜索素材：跳过 .env、私钥这类（内容读到就进上下文）
            if secret_name_reason(filename):
                skipped_secret += 1
                continue
            full = Path(dirpath) / filename
            try:
                if full.stat().st_size > MAX_FILE_BYTES:
                    continue
                raw = full.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:BINARY_PROBE_BYTES]:
                skipped_binary += 1
                continue
            scanned += 1

            relative = Path(os.path.relpath(full, root)).as_posix()
            for number, line in enumerate(raw.decode("utf-8", "replace").split("\n"), start=1):
                if not regex.search(line):
                    continue
                shown = line.strip()
                if len(shown) > MAX_LINE_CHARS:
                    shown = shown[:MAX_LINE_CHARS] + "…"
                hits.append((relative, number, shown))
                if len(hits) >= limit:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break

    if not hits:
        extra = f"，跳过 {skipped_binary} 个二进制文件" if skipped_binary else ""
        if skipped_secret:
            extra += f"，跳过 {skipped_secret} 个禁区文件"
        return f"没有匹配 {pattern} 的内容（搜了 {scanned} 个文件{extra}）。", False

    hits.sort(key=lambda item: (item[0], item[1]))
    output = [f"{path}:{number}: {text}" for path, number, text in hits]
    if truncated:
        output.append(f"…命中到 {limit} 条上限就停了，缩小范围或调大 limit。")
    return "\n".join(output), False
