"""花名册 + 照册办事的规矩。

这里不认识任何具体工具：工具由 tools/__init__.py 登记进来。
本文件只依赖 base.py，所以不会和具体工具成环。
"""

from __future__ import annotations

from typing import Any, Callable

from .base import MAX_OUTPUT_CHARS

# 一个工具的执行函数：收参数，返回 (给模型看的文本, 是不是失败)
ToolRun = Callable[[dict[str, Any]], tuple[str, bool]]

# 审批块要给人看的那几行：只读，不许有副作用
ToolPreview = Callable[[dict[str, Any]], list[str]]

# 按这一次的入参说清档位：返回 (档位, 审批块正文)。也是只读。
ToolInspect = Callable[[dict[str, Any]], tuple[str, list[str]]]

# 风险档：工具说"这一次有多危险"，策略拿它决定要不要问。
# read          无副作用（看、找、搜）
# write         改动文件
# exec          能执行命令
# irreversible  不可撤销（策略必须问；模式与"本会话不再问"都不管用）
RISK_LEVELS = ("read", "write", "exec", "irreversible")

# 缺省值故意落在"要问"那一边：忘了声明风险的工具会被问，而不是静默执行。
DEFAULT_RISK = "write"

# 名字 → {schema, run, risk, paths, preview}。按登记顺序保存（Python 3.7 起字典保序）。
TOOLS: dict[str, dict[str, Any]] = {}


def register(
    name: str,
    schema: dict[str, Any],
    run: ToolRun,
    *,
    risk: str = DEFAULT_RISK,
    paths: tuple[str, ...] = (),
    preview: ToolPreview | None = None,
    inspect: ToolInspect | None = None,
) -> None:
    """往册子上记一个工具。

    登记顺序就是发给模型的顺序，也是服务端能认出来的请求开头；
    装配完就不要在运行时改它（不要删、不要改名、不要插到中间）。

    risk / paths / preview 是工具侧的三个可选件，只描述事实、不做判断：
    要不要问、能不能碰，由 permissions.py 那一边决定。
    """
    if name in TOOLS:
        raise ValueError(f"工具重名：{name}")
    if risk not in RISK_LEVELS:
        raise ValueError(f"{name} 的风险档不认识：{risk}（允许：{'、'.join(RISK_LEVELS)}）")
    TOOLS[name] = {
        "schema": schema,
        "run": run,
        "risk": risk,
        "paths": paths,
        "preview": preview,
        "inspect": inspect,
    }


def build_schemas() -> list[dict[str, Any]]:
    """把册子翻译成发给模型的那份清单。装配完调用一次就够了。"""
    return [{"name": name, **spec["schema"]} for name, spec in TOOLS.items()]


def dispatch(call: dict[str, Any], permissions: Any) -> dict[str, Any]:
    """把一个 tool_use 块变成 tool_result 块。

    permissions 是必填的位置参数：所有调用都要先过审批，没有"忘了传就放行"这个状态。
    审批拒绝也是一条正常结果（is_error=True），不抛异常——模型看到原因才能换条路。

    未知工具、缺参数、函数抛异常，全都变成一条错误结果返回，不往上抛：
    模型看到错误会自己调整，循环不该因为一次调用失败就断掉。
    """
    name = str(call.get("name") or "")
    spec = TOOLS.get(name)
    raw_args = call.get("input")
    args = raw_args if isinstance(raw_args, dict) else {}

    if spec is None:
        text, is_error = f"未知工具：{name}", True
    else:
        decision = permissions.check(name, args, spec)
        if not decision.allowed:
            text, is_error = decision.message, True
        else:
            try:
                text, is_error = spec["run"](args)
            except Exception as error:  # 工具自己写错了也要让模型看到
                text, is_error = f"{type(error).__name__}: {error}", True

    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n…[truncated {len(text) - MAX_OUTPUT_CHARS} chars]"

    return {
        "type": "tool_result",
        "tool_use_id": call.get("id", ""),
        "content": text,
        "is_error": is_error,
    }
