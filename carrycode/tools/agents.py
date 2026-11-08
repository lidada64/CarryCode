"""四个子 agent 管理工具共用的接线。

管理工具要用的是会话里的那个 SubAgentManager，但工具层不许往上 import
（架构约束：tools/ 里的文件只能往下指），所以和 update_plan 走同一条路：
会话启动时 bind(manager) 接进来，换会话重新 bind。

这里只存一个引用、给一句统一的报错。参数校验和结果拼装还在各工具自己文件里。
"""

from __future__ import annotations

from typing import Any

# 没接上管理器时给模型的话：说清是内部状态，不鼓励它换条路乱试
NOT_BOUND = "子 agent 管理器还没接上（内部错误）：这一轮不要调用子 agent，直接做任务。"

_manager: Any = None


def bind(manager: Any) -> None:
    """接上这一场会话的管理器；传 None 表示先不接（测试收尾用）。"""
    global _manager
    _manager = manager


def current() -> Any:
    return _manager
