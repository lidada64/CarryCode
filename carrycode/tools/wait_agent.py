"""wait_agent 工具：等指定的一批子 agent 跑完，把报告收回来。

参照 MiniCode 的语义：一次可以等多个，默认 30 秒、最长 120 秒；
超时只是返回当前状态，不取消它们。等待的时间全在管理器里，工具只做参数校验。
"""

from __future__ import annotations

import json
from typing import Any

from .agents import NOT_BOUND, current

NAME = "wait_agent"

RISK = "read"  # 只等结果，不改任何东西

DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 120_000

SCHEMA = {
    "description": (
        "等指定的子 agent 全部结束，返回它们的最终状态和报告。"
        "一次可以传多个 id；超时只返回当前状态，不会关掉它们。"
        "默认等 30000 毫秒，最长 120000 毫秒。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "spawn_agent 返回的 id，至少一个",
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_TIMEOUT_MS,
                "description": "最多等多少毫秒，默认 30000，范围 0–120000",
            },
        },
        "required": ["agent_ids"],
    },
}


def run(args: dict[str, Any]) -> tuple[str, bool]:
    manager = current()
    if manager is None:
        return NOT_BOUND, True

    try:
        agent_ids = _agent_ids(args.get("agent_ids"))
        timeout_ms = _timeout(args.get("timeout_ms"))
        timed_out, records = manager.wait(agent_ids, timeout_ms)
    except ValueError as error:  # 参数不对，或者 id 不认识
        return str(error), True

    payload = {
        "timed_out": timed_out,
        "agents": [
            {
                "agent_id": record["agent_id"],
                "task": record["task"],
                "status": record["status"],
                "result": record["result"],
                "error": record["error"],
            }
            for record in records
        ],
    }
    return json.dumps(payload, ensure_ascii=False), False


def _agent_ids(raw: Any) -> list[str]:
    """参数不合法就抛 ValueError；消息直接给模型看，所以要说清怎么改。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("缺少 agent_ids：要一个非空数组，元素是 spawn_agent 返回的 id。")

    ids: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("agent_ids 里有一项不是有效字符串；每一项都得是 spawn_agent 返回的 id。")
        ids.append(item.strip())
    return ids


def _timeout(raw: Any) -> int:
    """没传就用默认值；传了就必须是 0–120000 的整数毫秒。"""
    if raw is None:
        return DEFAULT_TIMEOUT_MS
    if isinstance(raw, bool):
        raise ValueError("timeout_ms 要是整数毫秒，不是布尔值。")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("timeout_ms 要是整数毫秒，例如 30000。") from None
    if isinstance(raw, float) and raw != value:
        raise ValueError("timeout_ms 要是整数毫秒，不能带小数。")
    if not 0 <= value <= MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms 超出范围：{value}（允许 0–{MAX_TIMEOUT_MS}）。")
    return value
