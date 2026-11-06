"""把模型输出的 markdown 渲染到终端。

有 rich 就用 `rich.markdown` 排版；没有就退化成纯文本——
标题用下划线、行内标记去掉、代码块缩进，全程不输出 ANSI，
所以老版 PowerShell 里也不会出现 `←[1m` 这种乱码。
"""

from __future__ import annotations

import re
import sys
from typing import TextIO

# 行内的 `**粗体**` 和 `` `代码` ``：没有 rich 时把标记去掉，留下文字。
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")


def rich_available() -> bool:
    """rich 是可选件：装上就渲染，没装就走纯文本。"""
    try:
        import rich.markdown  # noqa: F401
    except ImportError:
        return False
    return True


def render_markdown(text: str, stream: TextIO | None = None) -> None:
    """渲染一段 markdown；空文本什么也不做。"""
    if not text:
        return
    out = stream or sys.stdout
    try:
        from rich.console import Console
        from rich.markdown import Markdown
    except ImportError:
        render_plain(text, out)
        return
    Console(file=out, highlight=False).print(Markdown(text))


def render_plain(text: str, stream: TextIO | None = None) -> None:
    """没有 rich 时的兜底：尽量保留结构，但不引入任何转义序列。"""
    out = stream or sys.stdout
    in_code = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            print(f"    {line}" if line else "", file=out)
            continue
        if line.startswith("# "):
            title = _strip_inline(line[2:])
            print(f"{title}\n{'=' * len(title)}", file=out)
        elif line.startswith("## "):
            title = _strip_inline(line[3:])
            print(f"{title}\n{'-' * len(title)}", file=out)
        else:
            print(_strip_inline(line), file=out)


def _strip_inline(line: str) -> str:
    line = _BOLD.sub(r"\1", line)
    return _CODE.sub(r"\1", line)
