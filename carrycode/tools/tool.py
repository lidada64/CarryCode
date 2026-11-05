"""花名册 + 照册办事的规矩。

这里不认识任何具体工具：工具由 tools/__init__.py 登记进来。
本文件只依赖 base.py，所以不会和具体工具成环。
"""

from __future__ import annotations

from typing import Any, Callable

from .base import MAX_OUTPUT_CHARS

# 一个工具的执行函数：收参数，返回 (给模型看的文本, 是不是失败)
ToolRun = Callable[[dict[str, Any]], tuple[str, bool]]

# 名字 → {说明, 执行函数}。按登记顺序保存（Python 3.7 起字典保序）。
TOOLS: dict[str, dict[str, Any]] = {}


def register(name: str, schema: dict[str, Any], run: ToolRun) -> None:
    """往册子上记一个工具。

    登记顺序就是发给模型的顺序，也是服务端能认出来的请求开头；
    装配完就不要在运行时改它（不要删、不要改名、不要插到中间）。
    """
    if name in TOOLS:
        raise ValueError(f"工具重名：{name}")
    TOOLS[name] = {"schema": schema, "run": run}


def build_schemas() -> list[dict[str, Any]]:
    """把册子翻译成发给模型的那份清单。装配完调用一次就够了。"""
    return [{"name": name, **spec["schema"]} for name, spec in TOOLS.items()]


def dispatch(call: dict[str, Any]) -> dict[str, Any]:
    """把一个 tool_use 块变成 tool_result 块。

    未知工具、缺参数、函数抛异常，全都变成一条错误结果返回，不往上抛：
    模型看到错误会自己调整，循环不该因为一次调用失败就断掉。
    """
    name = str(call.get("name") or "")
    spec = TOOLS.get(name)

    if spec is None:
        text, is_error = f"未知工具：{name}", True
    else:
        raw_args = call.get("input")
        args = raw_args if isinstance(raw_args, dict) else {}
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
