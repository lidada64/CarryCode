"""工具层公共件：路径、文本读写、参数兜底、几个共享常量。

三条规矩：

1. 这个文件不 import 项目里的任何东西（只用 Python 自带），所以它不会成环。
2. 只放「两个以上工具都要用」的东西；只有一个工具用的，留在它自己文件里。
3. 每个函数只做一件事。哪天某个函数长出第二件事（比如开始问权限），
   就把它搬去单独的 workspace.py，而不是在这里加分支。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_OUTPUT_CHARS = 12_000  # 回给模型的单条结果上限：bash 自己截一次，dispatch 再截一次

# glob / grep 默认跳过的目录：体积大、可再生，列出来只会淹没有用信息
SKIP_DIRS = frozenset({
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
})


def resolve_path(path: str) -> Path:
    """把模型给的路径换成绝对路径：相对路径按当前工作目录算。"""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    return target


def read_text(target: Path) -> tuple[str, bool]:
    """读文件，返回 (文本, 原文件是不是 CRLF 换行)。

    把 CRLF 统一成 LF 再交给工具匹配，写回去时再还原：
    这样模型给的 old_string 用普通换行也能对上，文件行尾风格也不会被改。
    """
    text = target.read_bytes().decode("utf-8", "replace")
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    return text, crlf


def write_text(target: Path, text: str, crlf: bool) -> int:
    """写文件（父目录自动创建），按 crlf 还原换行，返回写入的字节数。"""
    if crlf:
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return len(data)


def as_int(value: Any, default: int) -> int:
    """模型可能把数字写成字符串，转不过去就用默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
