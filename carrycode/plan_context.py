"""把计划放进模型请求。

每次调用模型前都重新读一遍快照再拼上去，不是建计划时拼一次。
压缩、回退、界面改过状态之后，请求里的清单都不会停在旧版本。

拼在 system 末尾，不塞进 messages：

    - 历史保持只追加，服务端的前缀缓存还能接着用
    - 会话日志里不会越积越多的旧清单

空计划只在一条用户提示刚进来的那一份请求里提一句（hint=True）；
之后同一轮不再重复。计划非空时永远带清单，也不再提“可以使用 update_plan”。
"""

from __future__ import annotations

from .plan import PlanManager, format_plan

# 计划为空时那一次提示。措辞照 产品文档 定：先说不存在，再给两个选择。
HINT_NO_PLAN = "当前没有计划。多步骤任务可使用 update_plan；简单任务直接执行。"

# 有计划时随清单一起给的规矩，防止模型改着改着把 id 丢了
PLAN_RULES = (
    "用 update_plan 改动计划时提交完整列表：已有条目保留 id，新条目省略 id。"
    "同一时间最多一个 in_progress。清单全部打勾也不等于任务完成。"
)


def with_plan_context(system: str, plan: PlanManager, *, hint: bool = False) -> str:
    """返回这一份请求要用的 system。

    hint 只在“一条用户提示刚进来”的那份请求里为 True。
    """
    todos = plan.snapshot()
    if todos:
        return f"{system}\n\n当前计划：\n{format_plan(todos)}\n\n{PLAN_RULES}"
    if not hint:
        return system
    return f"{system}\n\n{HINT_NO_PLAN}"
