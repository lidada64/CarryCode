"""模型请求：一次 Messages 请求，主 agent 和子 agent 共用。

分工：

    model.py        HTTP 请求、超时、错误转换、进程内唯一的一把请求锁
    agent_loop.py   模型 → 工具 → 模型的循环（不认识 HTTP）
    __main__.py     装配：一份 Model 同时交给主循环和子 agent 管理器

为什么只有一把锁：同一进程同时只发一份请求。主、子 agent 一起发只会互相抢配额，
也看不清 token 花在谁身上。锁只包住「发请求 + 读完响应」，不包工具执行——
工具跑多久都不占着锁。

取消：子 agent 在等锁的时候按 LOCK_POLL_SECONDS 看一眼自己的取消事件，置位就抛
Closed，这次请求不发出。已经在路上的同步请求没法强杀，只能等它返回或超时；
关闭的完整语义在 subagents.py。

配置：先读仓库根的 .env，再用同名环境变量覆盖（ANTHROPIC_API_KEY /
ANTHROPIC_BASE_URL / ANTHROPIC_MODEL，CARRYCODE_MODEL 也认）。
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent  # 仓库根，.env 在这里

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 8192
REQUEST_TIMEOUT = 120  # 单次模型请求超时（秒）

LOCK_POLL_SECONDS = 0.05  # 等锁时多久看一次取消事件


class Closed(RuntimeError):
    """收到关闭请求：这次请求没有发出去。"""


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


# ---------------------------------------------------------------------------
# 请求：锁 → 检查取消 → 发；响应读完才放锁
# ---------------------------------------------------------------------------


class Model:
    """一个共享的模型调用对象。配置在构造时定下来，之后只读。"""

    def __init__(self, config: dict[str, str]) -> None:
        self.config = dict(config)
        self._lock = threading.Lock()

    def call(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """发一次请求。同一进程里的所有调用在这里排队，一次只发一份。"""
        self._acquire(cancel)
        try:
            if cancel is not None and cancel.is_set():
                raise Closed("已请求关闭：这次请求没有发出去。")
            return self.send(messages, system, tools)
        finally:
            self._lock.release()

    def send(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """真的发 HTTP；测试里换掉这个方法就不用联网。"""
        base = self.config["base_url"].rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        url = f"{base}/v1/messages"

        body = {
            "model": self.config["model"],
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if tools:  # 综合请求不带工具：空数组不如干脆不传
            body["tools"] = tools
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": self.config["api_key"],
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            detail = _http_detail(error)
            raise RuntimeError(f"HTTP {error.code}：{detail}") from None
        except urllib.error.URLError as error:
            raise RuntimeError(f"连不上 {url}：{error.reason}") from None
        except TimeoutError:
            raise RuntimeError(f"模型请求超时（{REQUEST_TIMEOUT}s）") from None

    # ------------------------------------------------------------------
    # 内部：拿锁的时候要给取消留一条路
    # ------------------------------------------------------------------

    def _acquire(self, cancel: threading.Event | None) -> None:
        if cancel is None:
            self._lock.acquire()
            return
        while not self._lock.acquire(timeout=LOCK_POLL_SECONDS):
            if cancel.is_set():
                raise Closed("已请求关闭：这次请求没有发出去。")


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
