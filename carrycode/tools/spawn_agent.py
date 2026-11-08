"""spawn_agent 工具：起一个只读子 agent，立刻返回 id。

工具只做参数校验和调管理器；记录、线程、状态都在 subagents.py。
管理器由会话启动时 bind 进来（见 tools/agents.py）。
"""

from __future__ import annotations

import json
from typing import Any

from .agents import NOT_BOUND, current

NAME = "spawn_agent"

# 子 agent 只读、不碰文件也不执行命令：写 read，审批不该为它弹窗，只读模式下也能用
RISK = "read"

SCHEMA = {
    "description": (
        "派一个只读子 agent 去查一件独立的事，立刻返回 agent_id，不等它跑完。"
        "它有自己的上下文、看不到当前对话，只能用 read_file / glob / grep，"
        "不能改文件、跑命令、也不会再派子 agent；最多 3 个同时跑。"
        "任务要写清楚查什么、要什么证据。拿到 id 后用 wait_agent 收报告。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "一件独立、自洽的调查任务；子 agent 看不到对话和计划",
            },
        },
        "required": ["task"],
    },
}


def run(args: dict[str, Any]) -> tuple[str, bool]:
    manager = current()
    if manager is None:
        return NOT_BOUND, True

    task = str(args.get("task") or "").strip()
    if not task:
        return "缺少 task：说清楚要子 agent 查什么，例如「列出 tests 里每个文件测什么」。", True

    try:
        record = manager.spawn(task)
    except ValueError as error:
        return str(error), True

    return json.dumps(
        {"agent_id": record["agent_id"], "status": record["status"]},
        ensure_ascii=False,
    ), False
