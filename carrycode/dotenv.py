"""极简 .env 读取器（纯标准库）。

为什么自己写：项目红线是零第三方依赖，而这件事只有几十行。

支持：
    KEY=value
    KEY="value" / KEY='value'   （成对引号会被剥掉）
    # 整行注释、空行、export 前缀

刻意不支持（需要时再加，避免过度设计）：
    多行值、${VAR} 插值、行内注释

注意一个容易踩的坑：值里的 `#` 一律当普通字符，只有**整行**才是注释。
这样 `.env` 里写带 `#` 的 URL 片段或密码不会被悄悄截断。
"""

from __future__ import annotations

import re
from pathlib import Path

# 环境变量名必须是 ASCII 标识符；不符合的行直接忽略，不报错
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_dotenv(text: str) -> dict[str, str]:
    """把 .env 文本解析成字典。同名的键后者覆盖前者。"""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_RE.match(key):
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_dotenv(path: Path) -> dict[str, str]:
    """读一个 .env 文件。文件不存在返回空字典（有兜底链，不是错误）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    return parse_dotenv(text)
