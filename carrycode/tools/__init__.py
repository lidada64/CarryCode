"""装配：决定装哪几个工具、按什么顺序。

这是唯一能改顺序的地方。顺序决定请求开头那段，
也就决定服务端记住的部分还能不能接着用——不要改成字母序，不要扫目录。
"""

from __future__ import annotations

from . import (
    bash,
    close_agent,
    edit_file,
    glob,
    grep,
    list_agents,
    read_file,
    spawn_agent,
    update_plan,
    wait_agent,
    write_file,
)
from .agents import bind as bind_agents
from .tool import TOOLS, build_schemas, dispatch, register

# 顺序 = 发给模型的顺序。新工具追加到末尾，不要插到中间。
# 四个管理工具只给主 agent：worker 的清单是 subagents.py 里单独裁的那三个只读工具。
_MODULES = (
    bash,
    read_file,
    write_file,
    edit_file,
    glob,
    grep,
    update_plan,
    spawn_agent,
    list_agents,
    wait_agent,
    close_agent,
)

for _module in _MODULES:
    register(
        _module.NAME,
        _module.SCHEMA,
        _module.run,
        # 四个可选声明，工具没写就用缺省值；缺省的 risk 是 write（要问）
        risk=getattr(_module, "RISK", "write"),
        paths=getattr(_module, "PATHS", ()),
        preview=getattr(_module, "preview", None),
        inspect=getattr(_module, "inspect", None),
    )

TOOL_SCHEMAS = build_schemas()

__all__ = ["TOOL_SCHEMAS", "TOOLS", "bind_agents", "dispatch", "update_plan"]
