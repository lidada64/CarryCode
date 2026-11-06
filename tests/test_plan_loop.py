"""循环里怎么用计划：每次都带最新清单；空计划一条提示只催一次；计划不进历史。

跑法（在 CarryCode 仓库根）：

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from carrycode import __main__ as cli
from carrycode.permissions import Permissions
from carrycode.plan import PlanManager
from carrycode.plan_context import HINT_NO_PLAN
from carrycode.tools import update_plan

CONFIG = {"api_key": "test", "base_url": "https://example.invalid", "model": "test-model"}


def text_reply(text: str = "做完了") -> dict:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def plan_reply(todos: list[dict]) -> dict:
    return {
        "content": [
            {"type": "tool_use", "id": "tool_1", "name": "update_plan", "input": {"todos": todos}}
        ],
        "stop_reason": "tool_use",
    }


class FakeModel:
    """按脚本回放，顺便记下每次请求带的 system。"""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.systems: list[str] = []

    def __call__(self, messages, config, system):
        self.systems.append(system)
        return self.replies.pop(0)


class PlanLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = PlanManager()
        update_plan.bind(self.plan)
        self.permissions = Permissions(mode="auto", workspace=Path.cwd())

    def tearDown(self) -> None:
        update_plan.bind(None)

    def run_turn(self, model, task: str = "做点事") -> tuple[int, list]:
        messages: list = []
        with (
            patch.object(cli, "call_model", model),
            patch.object(cli, "render_markdown", lambda text: None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = cli.run_turn(messages, task, CONFIG, self.permissions, self.plan)
        return code, messages

    def test_empty_plan_is_mentioned_once_per_turn(self):
        # 第一份请求带提示；模型交了张空清单（还是没计划），第二份就不再重复
        model = FakeModel([plan_reply([]), text_reply()])
        code, _ = self.run_turn(model)
        self.assertEqual(code, 0)
        self.assertEqual(len(model.systems), 2)
        self.assertIn(HINT_NO_PLAN, model.systems[0])
        self.assertNotIn(HINT_NO_PLAN, model.systems[1])

    def test_the_next_request_carries_the_list_just_written(self):
        model = FakeModel(
            [
                plan_reply(
                    [
                        {"title": "读入口", "status": "in_progress"},
                        {"title": "写测试", "status": "pending"},
                    ]
                ),
                text_reply(),
            ]
        )
        self.run_turn(model)
        self.assertIn("当前计划", model.systems[1])
        self.assertIn("[>] 读入口", model.systems[1])
        self.assertIn(self.plan.snapshot()[0]["id"], model.systems[1])
        self.assertNotIn(HINT_NO_PLAN, model.systems[1])

    def test_latest_snapshot_wins_between_requests(self):
        systems: list[str] = []

        def model(messages, config, system):
            systems.append(system)
            if len(systems) == 1:
                return plan_reply([{"title": "读入口", "status": "in_progress"}])
            if len(systems) == 2:
                return plan_reply([{**self.plan.snapshot()[0], "status": "completed"}])
            return text_reply()

        self.run_turn(model)
        self.assertIn("[>] 读入口", systems[1])
        self.assertIn("[x] 读入口", systems[2])

    def test_second_turn_sees_the_plan_and_gets_no_hint(self):
        first = FakeModel([plan_reply([{"title": "读入口", "status": "pending"}]), text_reply()])
        self.run_turn(first)
        second = FakeModel([text_reply()])
        self.run_turn(second, task="接着做")
        self.assertIn("[ ] 读入口", second.systems[0])
        self.assertNotIn(HINT_NO_PLAN, second.systems[0])

    def test_injected_plan_never_lands_in_history(self):
        model = FakeModel([plan_reply([{"title": "读入口", "status": "pending"}]), text_reply()])
        _, messages = self.run_turn(model)
        dumped = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn("当前计划", dumped)
        self.assertNotIn(HINT_NO_PLAN, dumped)

    def test_plan_survives_a_rolled_back_turn(self):
        messages: list = []

        def model(messages_arg, config, system):
            if self.plan.is_empty:
                return plan_reply([{"title": "读入口", "status": "pending"}])
            raise RuntimeError("模型出错")

        with (
            patch.object(cli, "call_model", model),
            patch.object(cli, "render_markdown", lambda text: None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(RuntimeError):
                cli.run_turn(messages, "做点事", CONFIG, self.permissions, self.plan)

        self.assertEqual(messages, [], "历史回到这一轮开始前")
        self.assertEqual(len(self.plan.snapshot()), 1, "计划不属于历史，不回退")


class ReplCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = PlanManager()
        update_plan.bind(self.plan)
        self.permissions = Permissions(mode="auto", workspace=Path.cwd())

    def tearDown(self) -> None:
        update_plan.bind(None)

    def run_repl(self, lines: list[str]) -> str:
        buffer = io.StringIO()
        with (
            patch("builtins.input", side_effect=lines),
            patch.object(cli, "render_markdown", lambda text: None),
            contextlib.redirect_stdout(buffer),
        ):
            code = cli.repl(CONFIG, self.permissions, self.plan)
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_slash_plan_prints_the_checklist(self):
        self.plan.update([{"title": "读入口", "status": "pending"}])
        self.assertIn("[ ] 读入口", self.run_repl(["/plan", "/exit"]))

    def test_new_session_clears_the_plan(self):
        self.plan.update([{"title": "上一轮的", "status": "in_progress"}])
        self.run_repl(["/new", "/exit"])
        self.assertTrue(self.plan.is_empty)

    def test_help_mentions_plan_and_new(self):
        help_text = self.run_repl(["/help", "/exit"])
        self.assertIn("/plan", help_text)
        self.assertIn("/new", help_text)


if __name__ == "__main__":
    unittest.main()
