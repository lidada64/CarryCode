"""close_agent 工具：请一个子 agent 停下。

关闭是协作式的：置上取消事件，等 worker 真的退出再返回。
已经在路上的同步请求没法强杀，可能要等到它返回或超时——这是有意的，
比起把线程留在后台乱写，慢一点更安全。
"""

from __future__ import annotations

import json
from typing import Any

from .agents import NOT_BOUND, current

NAME = "close_agent"

RISK = "read"  # 只关一个内存里的调查线程，不碰文件也不执行命令

SCHEMA = {
    "description": (
        "请一个子 agent 停下，等它真的退出后返回最终状态。"
        "它跑偏了、不需要了、或者不想再花模型请求时用它；已经结束的 id 再关也安全。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "要停掉的子 agent id",
            },
        },
        "required": ["agent_id"],
    },
}


def run(args: dict[str, Any]) -> tuple[str, bool]:
    manager = current()
    if manager is None:
        return NOT_BOUND, True

    agent_id = str(args.get("agent_id") or "").strip()
    if not agent_id:
        return "缺少 agent_id：要一个 spawn_agent 返回的 id。", True

    try:
        record = manager.close(agent_id)
    except ValueError as error:  # 不认识的 id
        return str(error), True

    return json.dumps(
        {
            "agent_id": record["agent_id"],
            "status": record["status"],
            "result": record["result"],
            "error": record["error"],
        },
        ensure_ascii=False,
    ), False
