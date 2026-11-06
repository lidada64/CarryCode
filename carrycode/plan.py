"""计划状态：一份内存里的待办清单。

分工（三个组件各管一件事）：

    plan.py               存和校验：PlanManager
    plan_context.py       把最新清单放进模型请求：with_plan_context
    tools/update_plan.py  模型改清单的入口：整表提交

规则（整表替换）：

    - 每次提交完整列表，加、删、改标题、调顺序都靠它
    - 已有条目带上原来的 id，新条目不要带 id；不认识的 id 直接拒
    - 同一时间最多一个 in_progress
    - 空数组 = 清空计划

计划只活在内存里：新会话从空清单开始，恢复会话也不恢复它。
它也不进 messages——每次调用模型前从 manager 读一份快照再拼进 system
（见 plan_context.py），所以历史保持只追加，清单也不会在会话日志里越积越多。

中断那一轮时历史会回退，计划不回退：状态比历史稍新一点，这是有意的。
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

STATUSES = ("pending", "in_progress", "completed")

# 清单每轮都要塞进 system，不限个数等于不限上下文
MAX_TODOS = 20

# 标记表：给人看的 /plan、给模型看的注入，共用一份
MARKERS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}


class PlanError(ValueError):
    """计划不合法。消息直接给模型看，所以要说清哪里不对、怎么改。"""


class PlanManager:
    """这一场会话唯一的计划状态。改它只有 update / reset 两条路。"""

    def __init__(self) -> None:
        self._todos: list[dict[str, str]] = []
        self._listeners: list[Callable[[], None]] = []

    # -- 读 ---------------------------------------------------------

    def snapshot(self) -> list[dict[str, str]]:
        """当前清单的副本。交给谁都行，改它不影响内部状态。"""
        return [dict(todo) for todo in self._todos]

    @property
    def is_empty(self) -> bool:
        return not self._todos

    # -- 写 ---------------------------------------------------------

    def update(self, todos: Any) -> list[dict[str, str]]:
        """整张换掉。校验不过就抛 PlanError，内部状态一个字段都不动。"""
        parsed = _parse(todos, self._todos)
        self._todos = parsed
        self._notify()
        return self.snapshot()

    def reset(self) -> None:
        """清空。新会话、以及以后“恢复会话不恢复计划”都走这里。"""
        self._todos = []
        self._notify()

    # -- 订阅 -------------------------------------------------------

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """状态一变就回调。界面读 manager 就够了，不用去解析聊天记录。"""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


def format_plan(todos: list[dict[str, str]]) -> str:
    """给人看的清单。空清单也说一句话，不要返回空字符串。"""
    if not todos:
        return "当前没有计划。"

    lines = [
        f"{MARKERS[todo['status']]} {todo['title']} ({todo['id']})"
        for todo in todos
    ]
    done = sum(1 for todo in todos if todo["status"] == "completed")
    lines.append(f"（{done}/{len(todos)} 已完成）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 校验：只在这里认识“什么算合法的清单”
# ---------------------------------------------------------------------------


def _parse(todos: Any, current: list[dict[str, str]]) -> list[dict[str, str]]:
    if not isinstance(todos, list):
        raise PlanError('todos 必须是一个数组，元素形如 {"title": "…", "status": "pending"}')
    if len(todos) > MAX_TODOS:
        raise PlanError(
            f"一次最多 {MAX_TODOS} 项，现在给了 {len(todos)} 项；拆小一点再提交。"
        )

    known = {todo["id"] for todo in current}
    seen: set[str] = set()
    parsed: list[dict[str, str]] = []
    in_progress = 0

    for index, item in enumerate(todos, start=1):
        if not isinstance(item, dict):
            raise PlanError(f'第 {index} 项不是对象，应该是 {{"title": …, "status": …}}')

        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise PlanError(f"第 {index} 项缺 title：一句话说清这一步做什么")

        status = item.get("status")
        if status not in STATUSES:
            allowed = "、".join(STATUSES)
            raise PlanError(f"第 {index} 项的 status 不认识：{status!r}（允许：{allowed}）")
        if status == "in_progress":
            in_progress += 1

        todo_id = _todo_id(item.get("id"), known, seen, index)
        seen.add(todo_id)
        parsed.append({"id": todo_id, "title": title.strip(), "status": str(status)})

    if in_progress > 1:
        raise PlanError(f"同一时间只能有一个 in_progress，现在有 {in_progress} 个")

    return parsed


def _todo_id(raw: Any, known: set[str], seen: set[str], index: int) -> str:
    """已有条目原样带回 id；新条目发一个新 id。"""
    if raw is None:
        fresh = uuid.uuid4().hex[:8]
        while fresh in known or fresh in seen:
            fresh = uuid.uuid4().hex[:8]
        return fresh

    if not isinstance(raw, str) or not raw.strip():
        raise PlanError(f"第 {index} 项的 id 不是有效字符串；新条目不要填 id")

    if raw not in known:
        raise PlanError(f"不认识的 id：{raw}。新条目不要填 id，已有条目原样带回来")
    if raw in seen:
        raise PlanError(f"id 重复：{raw}")

    return raw
