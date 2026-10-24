"""CarryCode —— 只用 Python 标准库写的终端 code agent。

模块地图（每个文件对应 minicode 里的某个概念，边学边实现）：

    dotenv.py     .env 读取（本项目自己的约定，minicode 不读 .env）
    config.py     配置优先级 + 脱敏            ← minicode/config.py
    types.py      消息 / 工具调用 / 一步响应   ← minicode/types.py        （S1）
    events.py     事件流（核心与界面解耦）     ← （参考 PI 的多模式设计）  （S1）
    tools/        工具定义、校验、注册表       ← minicode/tooling.py      （S1）
    adapters/     模型协议边界                 ← minicode/anthropic_adapter.py（S1）
    loop.py       回合循环：何时继续、何时停   ← minicode/agent_loop.py    （S1）
    permissions.py 执行前审批                  ← minicode/permissions.py   （S1）
    cli.py        入口：一次性 / REPL / JSON   ← minicode/main.py          （S1）

当前进度：配置层完成；agent 主体按产品文档 9.1 施工单待建。
"""

__version__ = "0.1.0"
