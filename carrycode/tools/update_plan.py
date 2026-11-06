"""update_plan 工具：模型改计划的唯一入口。

工具层不 import 计划模块（架构约束：工具不能向上引用），所以行为从外面接进来：
会话启动时 `bind(plan)`，把这一场的 PlanManager 挂上；换会话重新 bind。

工具只做三件事：接参、交给 PlanManager、把结果变成字符串。
校验规则一条都不在这里——那属于 plan.py，改规则只改一处。
"""

from __future__ import annotations

import json
from typing import Any, Callable

NAME = "update_plan"

# 只改会话里的清单：不碰文件、不执行命令。
# 所以写 read：审批不该为“记一下待办”弹窗，只读模式下也该能用。
RISK = "read"

SCHEMA = {
    "description": (
        "把当前会话的待办清单整张换成新的一份：加、删、改标题、调顺序、改状态都靠它。"
        "提交完整列表——已有条目带上原来的 id，新条目不要带 id，空数组表示清空计划。"
        "同一时间最多一个 in_progress。这个工具只记计划，不执行任务，也不会让对话继续。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "完整的新清单",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "已有条目的 id；新条目不要填",
                        },
                        "title": {
                            "type": "string",
                            "description": "一句话说清这一步做什么",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["title", "status"],
                },
            },
        },
        "required": ["todos"],
    },
}

# 收一份 todos，返回 (给模型看的文本, 是不是失败)。
Update = Callable[[Any], tuple[str, bool]]

_update: Update | None = None


def bind(plan: Any) -> None:
    """把这一场会话的 PlanManager 接上；传 None 表示先不接（测试收尾用）。

    plan 只要有 update(todos) 就行，校验失败时抛 ValueError。
    """
    global _update
    _update = None if plan is None else _make_update(plan)


def _make_update(plan: Any) -> Update:
    def update(todos: Any) -> tuple[str, bool]:
        try:
            document = plan.update(todos)
        except ValueError as error:  # PlanError 是 ValueError 子类
            return str(error), True
        return json.dumps(document, ensure_ascii=False), False

    return update


def run(args: dict[str, Any]) -> tuple[str, bool]:
    if _update is None:
        return "计划状态还没接上（内部错误）：这一轮不要更新计划，继续做任务。", True

    todos = args.get("todos")
    if not isinstance(todos, list):
        return '缺少 todos 参数，它必须是一个数组，例如 [{"title": "读入口", "status": "pending"}]', True

    return _update(todos)
