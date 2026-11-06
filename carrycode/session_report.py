"""会话结束时的 usage 报告：统计 → markdown → 尽量用 rich 渲染。

为什么先出 markdown：报告本身要能直接读，也能原样贴进 issue。
有 rich 就渲染成终端样式；没装 rich 就退化成带 ANSI 的纯文本——
本项目的默认承诺仍然是「只依赖标准库」，rich 是可选件。
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO

from .render import render_markdown


@dataclass
class SessionStats:
    """一次会话里值得记的数：算力（tokens/请求）和工具调用。"""

    model: str = ""
    workspace: str = ""
    started_at: float = field(default_factory=time.time)
    turns: int = 0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: Counter[str] = field(default_factory=Counter)
    tool_errors: int = 0

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.started_at)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_tool_calls(self) -> int:
        return sum(self.tool_calls.values())

    def record_usage(self, usage: Any) -> None:
        """累加一次模型请求的 usage 块；缺字段就当 0。"""
        if not isinstance(usage, dict):
            return
        self.input_tokens += _as_int(usage.get("input_tokens"))
        self.output_tokens += _as_int(usage.get("output_tokens"))

    def record_tool(self, name: str, is_error: bool) -> None:
        self.tool_calls[name or "(未知)"] += 1
        if is_error:
            self.tool_errors += 1


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"{total} 秒"
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def build_markdown(stats: SessionStats) -> str:
    """把统计写成一份 markdown 报告。"""
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stats.started_at))
    lines = [
        "# CarryCode 会话报告",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 模型 | {stats.model or '(未知)'} |",
        f"| 工作区 | {stats.workspace or '(未知)'} |",
        f"| 开始时间 | {started} |",
        f"| 时长 | {_fmt_duration(stats.elapsed)} |",
        f"| 任务轮数 | {stats.turns} |",
        f"| 模型请求 | {stats.requests} |",
        "",
        "## 算力",
        "",
        "| 输入 tokens | 输出 tokens | 合计 |",
        "| ---: | ---: | ---: |",
        f"| {stats.input_tokens:,} | {stats.output_tokens:,} | {stats.total_tokens:,} |",
        "",
        "## 工具调用",
        "",
    ]

    if stats.total_tool_calls == 0:
        lines.append("本次没有调用工具。")
    else:
        lines.extend(["| 工具 | 次数 |", "| --- | ---: |"])
        for name, count in stats.tool_calls.most_common():
            lines.append(f"| {name} | {count} |")
        lines.append("")
        lines.append(
            f"共 {stats.total_tool_calls} 次，其中失败 {stats.tool_errors} 次。"
        )

    return "\n".join(lines) + "\n"


def render(markdown: str, stream: TextIO | None = None) -> None:
    """渲染报告；具体用 rich 还是纯文本由 render.py 决定。"""
    render_markdown(markdown, stream)


def make_session_report_hook(stream: TextIO | None = None) -> Callable[..., None]:
    """做成 session_end 的 handler：收 stats，出报告。"""

    def handler(*, stats: SessionStats) -> None:
        print(file=stream or sys.stdout)
        render(build_markdown(stats), stream)

    return handler
