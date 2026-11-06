"""生命周期钩子：会话开始/结束时喊一声。

这里只有「通知」，没有「判定」——审批要不要放行仍然只看 permissions.py。
handler 抛异常只影响它自己：退出时报告画不出来，不该把退出流程一起弄挂。
"""

from __future__ import annotations

from typing import Any, Callable

# 事件名就是普通字符串，以后加新事件不用动这里。
SESSION_START = "session_start"
SESSION_END = "session_end"

Handler = Callable[..., None]


class Hooks:
    """事件名 → handler 列表。按注册顺序依次调用。"""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}

    def on(self, event: str, handler: Handler) -> Callable[[], None]:
        """挂一个 handler，返回摘掉它的方法（和注册成对出现）。"""
        self._handlers.setdefault(event, []).append(handler)

        def off() -> None:
            handlers = self._handlers.get(event, [])
            if handler in handlers:
                handlers.remove(handler)

        return off

    def emit(self, event: str, **data: Any) -> list[Any]:
        """喊一声，把 handler 的返回值收起来；异常就地吞掉，不往上传。"""
        results: list[Any] = []
        for handler in list(self._handlers.get(event, ())):
            try:
                results.append(handler(**data))
            except Exception as error:  # 钩子出错不耽误主流程
                results.append(error)
        return results
