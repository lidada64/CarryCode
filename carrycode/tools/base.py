"""工具层公共件：路径、文本读写、参数兜底、几个共享常量。

三条规矩：

1. 这个文件不 import 项目里的任何东西（只用 Python 自带），所以它不会成环。
2. 只放「两个以上地方都要用」的东西（工具，或者审批那一层）；只有一处用的，留在它自己文件里。
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


# ---------------------------------------------------------------------------
# 禁区：这几类文件里是凭据，读到就等于泄漏（内容会进模型上下文）
#
# 两个地方要用它：读工具读文件之前要跳过，审批要直接拒。所以放在这里。
# ---------------------------------------------------------------------------

SECRET_NAMES = (".env",)  # 前缀匹配：.env、.env.local、.env.production …
SECRET_FILENAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ed25519_sk")
SECRET_EXAMPLES = (".env.example", ".env.sample", ".env.template", ".env.dist")
SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".keystore")
SECRET_DIRS = ("~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gcloud", "~/.azure")
SECRET_FILES = ("~/.git-credentials", "~/.netrc", "~/.pgpass")


def secret_name_reason(name: str) -> str | None:
    """只看文件名：是不是禁区文件。是就返回一句原因（给模型看的英文）。"""
    lowered = name.lower()
    if lowered in SECRET_EXAMPLES:
        return None
    if lowered in SECRET_FILENAMES:
        return "this file holds an SSH private key and is never readable"
    for prefix in SECRET_NAMES:
        if lowered == prefix or lowered.startswith(prefix + "."):
            return "this file holds credentials (API keys) and is never readable"
    for suffix in SECRET_SUFFIXES:
        if lowered.endswith(suffix):
            return "this file holds a key or certificate and is never readable"
    return None


def secret_path_reason(target: Path) -> str | None:
    """完整判定：文件名、所在目录、具体路径。"""
    reason = secret_name_reason(target.name)
    if reason:
        return reason
    resolved = _resolve_quietly(target)
    for directory in SECRET_DIRS:
        if is_within(_resolve_quietly(Path(directory).expanduser()), resolved):
            return "this directory holds credentials and is never readable"
    for file in SECRET_FILES:
        if resolved == _resolve_quietly(Path(file).expanduser()):
            return "this file holds credentials and is never readable"
    return None


def is_within(root: Path, target: Path) -> bool:
    """target 在 root 里面（含 root 本身）。"""
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_quietly(target: Path) -> Path:
    try:
        return target.resolve()
    except OSError:
        return target


def as_int(value: Any, default: int) -> int:
    """模型可能把数字写成字符串，转不过去就用默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
