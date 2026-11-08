"""会话报告与生命周期钩子：不联网、不开终端，只断言拼出来的东西对不对。"""

from __future__ import annotations

import io
import unittest

from carrycode.hooks import SESSION_END, Hooks
from carrycode.session_report import (
    SessionStats,
    build_markdown,
    make_session_report_hook,
)


class HooksTest(unittest.TestCase):
    def test_handlers_run_in_registration_order(self):
        hooks = Hooks()
        seen: list[str] = []
        hooks.on(SESSION_END, lambda **_: seen.append("first"))
        hooks.on(SESSION_END, lambda **_: seen.append("second"))

        hooks.emit(SESSION_END, stats=None)

        self.assertEqual(seen, ["first", "second"])

    def test_handler_error_does_not_break_the_others(self):
        hooks = Hooks()
        seen: list[str] = []

        def boom(**_: object) -> None:
            raise RuntimeError("画不出来")

        hooks.on(SESSION_END, boom)
        hooks.on(SESSION_END, lambda **_: seen.append("still ran"))

        results = hooks.emit(SESSION_END, stats=None)

        self.assertEqual(seen, ["still ran"])
        self.assertIsInstance(results[0], RuntimeError)

    def test_off_removes_only_that_handler(self):
        hooks = Hooks()
        seen: list[str] = []
        off = hooks.on(SESSION_END, lambda **_: seen.append("gone"))
        hooks.on(SESSION_END, lambda **_: seen.append("stays"))

        off()
        hooks.emit(SESSION_END, stats=None)

        self.assertEqual(seen, ["stays"])

    def test_unknown_event_is_a_no_op(self):
        self.assertEqual(Hooks().emit("nobody-listens", stats=None), [])


class SessionStatsTest(unittest.TestCase):
    def test_usage_accumulates_and_missing_fields_count_as_zero(self):
        stats = SessionStats()
        stats.record_usage({"input_tokens": 100, "output_tokens": 20})
        stats.record_usage({"input_tokens": 5})
        stats.record_usage(None)

        self.assertEqual(stats.input_tokens, 105)
        self.assertEqual(stats.output_tokens, 20)
        self.assertEqual(stats.total_tokens, 125)

    def test_tool_calls_are_counted_per_name(self):
        stats = SessionStats()
        stats.record_tool("bash", False)
        stats.record_tool("bash", True)
        stats.record_tool("read_file", False)

        self.assertEqual(stats.tool_calls["bash"], 2)
        self.assertEqual(stats.total_tool_calls, 3)
        self.assertEqual(stats.tool_errors, 1)

    def test_requests_are_counted_for_main_and_workers(self):
        stats = SessionStats()
        stats.record_request()
        stats.record_request()

        self.assertEqual(stats.requests, 2)


class MarkdownTest(unittest.TestCase):
    def _stats(self) -> SessionStats:
        stats = SessionStats(model="deepseek-v4-flash", workspace="D:\\demo")
        stats.turns = 2
        stats.requests = 3
        stats.input_tokens = 1234
        stats.output_tokens = 56
        stats.record_tool("bash", False)
        stats.record_tool("bash", True)
        stats.record_tool("read_file", False)
        return stats

    def test_markdown_carries_the_numbers(self):
        text = build_markdown(self._stats())

        self.assertIn("# CarryCode 会话报告", text)
        self.assertIn("deepseek-v4-flash", text)
        self.assertIn("| 模型请求（含子 agent） | 3 |", text)
        self.assertIn("| 1,234 | 56 | 1,290 |", text)
        self.assertIn("| bash | 2 |", text)
        self.assertIn("共 3 次，其中失败 1 次。", text)

    def test_empty_session_still_reads_like_a_report(self):
        text = build_markdown(SessionStats())

        self.assertIn("本次没有调用工具。", text)
        self.assertIn("| 0 | 0 | 0 |", text)

    def test_hook_writes_a_report_to_the_given_stream(self):
        buffer = io.StringIO()
        handler = make_session_report_hook(buffer)

        handler(stats=self._stats())

        self.assertIn("会话报告", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
