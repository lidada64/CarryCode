"""list_agents 工具：看一眼本会话创建过的子 agent 都有谁、现在什么状态。

不返回完整报告——报告由 wait_agent 收集，列表只用来决定等谁、关谁。
"""

from __future__ import annotations

import json
from typing import Any

from .agents import NOT_BOUND, current

NAME = "list_agents"

RISK = "read"  # 只读内存里的记录

# 唯一一个真的不需要参数的工具：空对象是它的完整参数表
SCHEMA = {
    "description": (
        "列出本会话创建过的全部子 agent：id、任务、状态，失败的带一句错误摘要。"
        "要看完整报告用 wait_agent；还在跑的会显示 running。"
    ),
    "input_schema": {"type": "object", "properties": {}},
}


def run(args: dict[str, Any]) -> tuple[str, bool]:
    manager = current()
    if manager is None:
        return NOT_BOUND, True

    agents = [
        {
            "agent_id": record["agent_id"],
            "task": record["task"],
            "status": record["status"],
            "error": record["error"],
        }
        for record in manager.records()
    ]
    payload = {
        "running": manager.running_count,
        "max_concurrent": manager.max_concurrent,
        "agents": agents,
    }
    return json.dumps(payload, ensure_ascii=False), False
