"""模型 → 工具 → 模型的循环，主 agent 和子 agent 共用。

循环只认外面传进来的几样东西，自己不认识计划、不认识子 agent，也不 print：

    request(messages, system, tools) -> 响应    发一次请求（model.py 的 Model.call）
    system(step) -> str                         每一步的 system（主 agent 在这里拼计划）
    tools                                       这次请求开放的工具说明
    execute(tool_use 块) -> tool_result 块      执行一个工具（审批、白名单都在这一层）
    on_text(text, final)                        模型说了话（final 表示这是收尾的那句）
    on_tools_done()                             一批工具跑完了（只影响排版）

取消只在这一层检查「下一个请求／下一个工具之前」：已经在路上的同步请求收不回来，
那由 model.py 和 subagents.py 负责。

收尾时主 agent 可能已经说完一句话、而子 agent 的报告还没到；这时那句话先压着不发
（on_text 的 final 分支由 __main__ 决定压不压），等报告收齐再用 synthesize() 请求
一次综合。synthesize 不带工具：它的活是写结论，不是接着干活。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from .model import Closed


@dataclass(frozen=True)
class Outcome:
    """一轮跑完的结果。problem 是给人看的那句话，空字符串表示没有出问题。"""

    code: int = 0
    final_text: str = ""
    stop_reason: str = ""
    problem: str = ""
    exhausted: bool = False  # 是不是撞到步数上限才停的
    steps: int = 0


def run_steps(
    messages: list[dict[str, Any]],
    *,
    request: Callable[..., dict[str, Any]],
    system: Callable[[int], str],
    tools: list[dict[str, Any]],
    execute: Callable[[dict[str, Any]], dict[str, Any]],
    max_steps: int,
    on_text: Callable[[str, bool], None] | None = None,
    on_tools_done: Callable[[], None] | None = None,
    cancel: threading.Event | None = None,
) -> Outcome:
    """在已有历史上跑一轮：模型 → 工具 → 模型 …，直到 stop_reason 不是 tool_use。

    每次请求前都现算一份 system，所以主 agent 的计划永远是最新的那一份。
    assistant 的原始 blocks（含 thinking 与 tool_use）原样入历史，
    DeepSeek 的 Anthropic 兼容端点要求这样回传。
    """
    for step in range(max_steps):
        _check_cancel(cancel)
        data = request(messages, system(step), tools)
        blocks = [block for block in data.get("content") or [] if isinstance(block, dict)]
        tool_calls = [block for block in blocks if block.get("type") == "tool_use"]
        text = _text_of(blocks)
        stop_reason = data.get("stop_reason")
        final = stop_reason != "tool_use"

        if text and on_text is not None:
            on_text(text, final)

        messages.append({"role": "assistant", "content": blocks})

        # 边界只看 stop_reason：tool_use 表示「还要调工具」，其余都收尾。
        if final:
            if not text:
                return Outcome(
                    code=1,
                    stop_reason=str(stop_reason),
                    problem=f"（没有文本，stop_reason={stop_reason}）",
                    steps=step + 1,
                )
            if stop_reason == "max_tokens":
                return Outcome(
                    code=1,
                    final_text=text,
                    stop_reason=str(stop_reason),
                    problem="（输出被 max_tokens 截断，本轮不算完成）",
                    steps=step + 1,
                )
            return Outcome(
                code=0,
                final_text=text,
                stop_reason=str(stop_reason),
                steps=step + 1,
            )

        if not tool_calls:
            return Outcome(
                code=1,
                stop_reason=str(stop_reason),
                problem="（stop_reason=tool_use，但没有 tool_use 块）",
                steps=step + 1,
            )

        results: list[dict[str, Any]] = []
        for call in tool_calls:
            _check_cancel(cancel)
            results.append(execute(call))
        # 一轮里的所有 tool_result 必须放在同一条 user 消息里
        messages.append({"role": "user", "content": results})
        if on_tools_done is not None:
            on_tools_done()

    return Outcome(
        code=1,
        problem=f"到 {max_steps} 步上限，停止。",
        exhausted=True,
        steps=max_steps,
    )


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Closed("已请求关闭：这次不再发起请求或工具。")


def _text_of(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        block["text"]
        for block in blocks
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    ).strip()


# ---------------------------------------------------------------------------
# 综合：把子 agent 的报告交给主 agent，写最终答复
# ---------------------------------------------------------------------------

REPORT_INTRO = (
    "下面是你派出去的子 agent 的结果。请基于它们给出最终答复："
    "查清楚的直接给结论，没查清的说明没查清；工具已经收起来了，不要再要求调用工具。"
)


def synthesize(
    messages: list[dict[str, Any]],
    *,
    request: Callable[..., dict[str, Any]],
    system: str,
    report_text: str,
) -> str:
    """把报告塞进上下文，再请求一次模型，返回最终答复文本（空字符串＝没综合成功）。

    这一次请求不开放工具：它的活是写结论，不是接着干活。
    回复里只要出现 tool_use 就当成没综合成功——工具已经收起来了，模型还要求调用，
    说明它没按约定收尾；这种回复也不能原样进历史，assistant 的 tool_use 没有配对的结果。
    被 max_tokens 截断的答复同样不算：半截结论不能当最终答复，抛给调用方去展示原始报告。
    """
    messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": f"{REPORT_INTRO}\n\n{report_text}"}],
        }
    )

    data = request(messages, system, [])
    blocks = [block for block in data.get("content") or [] if isinstance(block, dict)]
    text = _text_of(blocks)
    wants_tools = any(block.get("type") == "tool_use" for block in blocks)

    if wants_tools or not text:
        if text:
            # 只留文字，别把没有结果的 tool_use 带进历史
            messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        return ""

    messages.append({"role": "assistant", "content": blocks})
    if data.get("stop_reason") == "max_tokens":
        raise RuntimeError("综合请求被 max_tokens 截断，没有给出完整答复。")
    return text
