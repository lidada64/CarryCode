"""配置解析：三层兜底 + 来源追踪 + 脱敏。

优先级（高 → 低）：

    1. 显式传入（命令行参数）
    2. 进程环境变量
    3. 仓库根的 .env（本机私有，git 忽略）
    4. ~/.carrycode/settings.json
    5. ~/.mini-code/settings.json（复用 minicode 的现成配置，兜底）

为什么进程环境变量高于 .env：CI 与临时覆盖都靠环境变量，
本机文件不该压过命令行会话里的显式设置。

对应 minicode/config.py：同样是「多层合并 + 环境变量优先」，
区别是这里多了一层项目自己的 .env，并额外记录每个值来自哪里。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .dotenv import load_dotenv

DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TIMEOUT = 120.0

# 仓库根 = 本包目录的上一级，.env 与 pyproject.toml 同级
REPO_ROOT = Path(__file__).resolve().parent.parent
USER_SETTINGS = Path.home() / ".carrycode" / "settings.json"
COMPAT_SETTINGS = Path.home() / ".mini-code" / "settings.json"


def mask(value: str | None) -> str:
    """密钥脱敏：只留首尾各 4 位，够你确认「是不是这把」，不够别人拿去用。"""
    if not value:
        return "(未设置)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


@dataclass(slots=True)
class ModelSettings:
    """最终生效的模型配置。"""

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    auth_token: str | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = DEFAULT_TIMEOUT

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key or self.auth_token)

    def masked_view(self) -> dict[str, str]:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key": mask(self.api_key),
            "auth_token": mask(self.auth_token),
            "max_tokens": str(self.max_tokens),
            "timeout": f"{self.timeout:g}s",
        }

    def problems(self) -> list[str]:
        """启动前该拦住的问题。空列表 = 可用。"""
        if self.has_credentials:
            return []
        return [
            "缺少模型密钥：把 .env.example 复制为 .env 并填入 ANTHROPIC_API_KEY，"
            "或设置同名环境变量。"
        ]


@dataclass(slots=True)
class ResolvedSettings:
    settings: ModelSettings
    sources: dict[str, str] = field(default_factory=dict)
    env_files: list[Path] = field(default_factory=list)

    def describe(self) -> list[str]:
        lines = [f"{key:10s} {value}" for key, value in self.settings.masked_view().items()]
        lines.append("")
        lines.append("来源：")
        for key, source in self.sources.items():
            lines.append(f"  {key:10s} ← {source}")
        loaded = ", ".join(str(path) for path in self.env_files) if self.env_files else "(未找到)"
        lines.append(f"  {'.env':10s} ← {loaded}")
        return lines


# ---------------------------------------------------------------------------
# 内部：settings.json 与类型转换
# ---------------------------------------------------------------------------


def _read_settings(path: Path) -> dict[str, Any]:
    """读 settings.json；不存在或坏掉都当作空配置，不让配置问题阻断读取。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _settings_value(data: dict[str, Any], env_keys: Iterable[str], settings_key: str) -> Any:
    """从一份 settings.json 里按 minicode 的形状取值。

    支持的形状（三种都认，便于复用 ~/.mini-code/settings.json）：
        {"env": {"ANTHROPIC_*": "..."}}
        {"ANTHROPIC_*": "..."}
        {"model": "...", "maxTokens": 8192}
    """
    env_section = data.get("env")
    if isinstance(env_section, dict):
        for key in env_keys:
            if env_section.get(key):
                return env_section[key]

    for key in env_keys:
        if data.get(key):
            return data[key]

    if settings_key and data.get(settings_key):
        return data[settings_key]
    return None


def _to_int(value: Any, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _to_float(value: Any, fallback: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def resolve(
    *,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    repo_root: Path | None = None,
    env_paths: list[Path] | None = None,
    settings_paths: list[Path] | None = None,
) -> ResolvedSettings:
    """解析出最终配置。所有参数都可注入，方便单测不起网络也不用改家目录。"""
    overrides = dict(overrides or {})
    env = dict(os.environ if env is None else env)
    root = REPO_ROOT if repo_root is None else Path(repo_root)
    env_paths = [root / ".env"] if env_paths is None else [Path(path) for path in env_paths]
    settings_paths = (
        [USER_SETTINGS, COMPAT_SETTINGS]
        if settings_paths is None
        else [Path(path) for path in settings_paths]
    )

    # 多个 .env 时先出现者优先（setdefault）
    dotenv: dict[str, str] = {}
    env_files: list[Path] = []
    for path in env_paths:
        values = load_dotenv(path)
        if values:
            env_files.append(path)
        for key, value in values.items():
            dotenv.setdefault(key, value)

    layers = [(str(path), _read_settings(path)) for path in settings_paths]
    layers = [(label, data) for label, data in layers if data]

    sources: dict[str, str] = {}

    def pick(field_name: str, env_keys: list[str], settings_key: str) -> Any:
        value = overrides.get(field_name)
        if value not in (None, ""):
            sources[field_name] = "命令行"
            return value

        for key in env_keys:
            if env.get(key):
                sources[field_name] = f"env:{key}"
                return env[key]

        for key in env_keys:
            if dotenv.get(key):
                sources[field_name] = f".env:{key}"
                return dotenv[key]

        for label, data in layers:
            value = _settings_value(data, env_keys, settings_key)
            if value:
                sources[field_name] = label
                return value

        sources[field_name] = "默认值"
        return None

    settings = ModelSettings(
        model=pick("model", ["CARRYCODE_MODEL", "ANTHROPIC_MODEL"], "model") or DEFAULT_MODEL,
        base_url=pick("base_url", ["CARRYCODE_BASE_URL", "ANTHROPIC_BASE_URL"], "baseUrl")
        or DEFAULT_BASE_URL,
        api_key=pick("api_key", ["CARRYCODE_API_KEY", "ANTHROPIC_API_KEY"], "apiKey"),
        auth_token=pick("auth_token", ["CARRYCODE_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"], "authToken"),
        max_tokens=_to_int(
            pick("max_tokens", ["CARRYCODE_MAX_TOKENS", "ANTHROPIC_MAX_TOKENS"], "maxTokens"),
            DEFAULT_MAX_TOKENS,
        ),
        timeout=_to_float(pick("timeout", ["CARRYCODE_TIMEOUT"], "timeout"), DEFAULT_TIMEOUT),
    )

    return ResolvedSettings(settings=settings, sources=sources, env_files=env_files)
