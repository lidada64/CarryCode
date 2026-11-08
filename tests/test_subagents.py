"""子 agent：独立消息、共享锁、白名单、并发上限、等待/超时、关闭、自动收尾。

全程不联网、不 sleep：要卡住的地方用 threading.Event，放行由测试自己 set；
假模型把每次请求的 messages / system / tools 都抄一份，测试靠这些断言上下文是独立的。
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from carrycode import __main__ as cli
from carrycode import model as model_module
from carrycode import subagents as subagents_module
from carrycode.permissions import MSG_NO_TTY, Permissions
from carrycode.plan import PlanManager
from carrycode.session_report import SessionStats
from carrycode.subagents import (
    MAX_CONCURRENT,
    STATUS_CLOSED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    WORKER_MAX_STEPS,
    WORKER_SCHEMAS,
    WORKER_TOOL_NAMES,
    SubAgentManager,
    format_reports,
)
from carrycode.tools import TOOLS, bind_agents, update_plan

CONFIG = {"api_key": "test", "base_url": "https://example.invalid", "model": "test-model"}


def text_reply(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def call_reply(name: str, args: dict, tool_id: str = "tool_1") -> dict:
    return {
        "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": args}],
        "stop_reason": "tool_use",
    }


def scripted(*items):
    """按顺序回放。给多了不报，给少了直接炸——漏写回复比静默继续好查。"""

    remaining = list(items)
    guard = threading.Lock()

    def respond(messages, system, tools, cancel, index):
        with guard:
            if not remaining:
                raise AssertionError("假模型没有更多回复了")
            item = remaining.pop(0)
        return item(messages, system, tools) if callable(item) else item

    return respond


class FakeModel:
    """假的模型调用：respond 拿到原始 messages，返回值当响应。

    记下的是副本，worker 之后往自己的历史里追加不会改到断言用的快照。
    """

    def __init__(self, respond) -> None:
        self.respond = respond
        self.calls: list[dict] = []
        self._guard = threading.Lock()

    def call(self, messages, system, tools, cancel=None):
        with self._guard:
            index = len(self.calls)
            self.calls.append(
                {
                    "messages": copy.deepcopy(messages),
                    "system": system,
                    "tools": list(tools),
                }
            )
        return self.respond(messages, system, tools, cancel, index)

    # -- 断言用的小读法 -------------------------------------------------

    def tool_result(self, call_index: int) -> dict:
        """第 call_index 次请求时，历史里最后一条 user 消息里的 tool_result。"""
        return self.calls[call_index]["messages"][-1]["content"][0]


def task_text(messages) -> str:
    """最后一条 user 消息的文字（worker 的任务，或者工具结果）。"""
    content = messages[-1].get("content")
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class ManagerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.permissions = Permissions(mode="auto", workspace=Path.cwd())
        self.stats = SessionStats(model="test-model", workspace=str(Path.cwd()))
        self.events: list[str] = []

    def tearDown(self) -> None:
        bind_agents(None)

    def make_manager(
        self,
        model,
        *,
        permissions=None,
        max_concurrent=MAX_CONCURRENT,
        max_steps=WORKER_MAX_STEPS,
    ):
        return SubAgentManager(
            model=model,
            permissions=permissions or self.permissions,
            workspace=Path.cwd(),
            stats=self.stats,
            emit=self.events.append,
            max_concurrent=max_concurrent,
            max_steps=max_steps,
        )


class WorkerToolsetTest(unittest.TestCase):
    def test_worker_only_gets_the_three_read_only_tools(self):
        self.assertEqual(WORKER_TOOL_NAMES, ("read_file", "glob", "grep"))
        self.assertEqual([schema["name"] for schema in WORKER_SCHEMAS], list(WORKER_TOOL_NAMES))
        for name in WORKER_TOOL_NAMES:
            self.assertEqual(TOOLS[name]["risk"], "read", f"{name} 不该进 worker 的清单")

    def test_management_tools_are_not_available_to_a_worker(self):
        for name in ("spawn_agent", "list_agents", "wait_agent", "close_agent"):
            self.assertNotIn(name, WORKER_TOOL_NAMES)


class IsolationTest(ManagerFixture):
    def test_each_worker_gets_its_own_messages_and_tools(self):
        model = FakeModel(
            lambda messages, system, tools, cancel, index: text_reply(
                "报告：" + task_text(messages)
            )
        )
        manager = self.make_manager(model)

        first = manager.spawn("查 tests")
        second = manager.spawn("查 docs")
        timed_out, _ = manager.wait([first["agent_id"], second["agent_id"]], 5_000)

        self.assertFalse(timed_out)
        records = {record["agent_id"]: record for record in manager.records()}
        self.assertEqual(records[first["agent_id"]]["result"], "报告：查 tests")
        self.assertEqual(records[second["agent_id"]]["result"], "报告：查 docs")

        for call in model.calls:
            self.assertEqual(len(call["messages"]), 1, "每个 worker 只有自己那条任务")
            self.assertEqual(
                [tool["name"] for tool in call["tools"]],
                ["read_file", "glob", "grep"],
            )
            self.assertNotIn("当前计划", call["system"])
            self.assertIn(str(Path.cwd()), call["system"])

    def test_worker_requests_count_in_the_session_stats(self):
        manager = self.make_manager(FakeModel(scripted(text_reply("报告"))))
        record = manager.spawn("查")
        manager.wait([record["agent_id"]], 5_000)
        self.assertEqual(self.stats.requests, 1)


class WhitelistTest(ManagerFixture):
    def test_real_tools_go_through_dispatch_and_permissions(self):
        globbed = []

        def respond(messages, system, tools, cancel, index):
            if index == 0:
                return call_reply("glob", {"pattern": "*.py"})
            return text_reply("报告：看过文件列表了")

        model = FakeModel(respond)
        manager = self.make_manager(model)
        real_dispatch = subagents_module.dispatch

        def spy(call, permissions):
            globbed.append(call["name"])
            return real_dispatch(call, permissions)

        with patch.object(subagents_module, "dispatch", spy):
            record = manager.spawn("查测试")
            manager.wait([record["agent_id"]], 5_000)

        self.assertEqual(globbed, ["glob"])
        self.assertFalse(model.tool_result(1)["is_error"])
        self.assertEqual(self.stats.tool_calls["glob"], 1, "worker 的工具调用也算进会话统计")

    def test_forged_tool_name_is_rejected_at_the_execution_layer(self):
        def respond(messages, system, tools, cancel, index):
            if index == 0:
                return call_reply("bash", {"command": "dir"})
            return text_reply("报告：只能承认调不了 bash")

        model = FakeModel(respond)
        manager = self.make_manager(model)
        real_dispatch = subagents_module.dispatch
        reached = []

        def spy(call, permissions):
            reached.append(call["name"])
            return real_dispatch(call, permissions)

        with patch.object(subagents_module, "dispatch", spy):
            record = manager.spawn("查测试")
            manager.wait([record["agent_id"]], 5_000)

        self.assertEqual(reached, [], "白名单外的工具不该进 dispatch")
        result = model.tool_result(1)
        self.assertTrue(result["is_error"])
        self.assertIn("只能用", result["content"])
        self.assertEqual(manager.records()[0]["status"], STATUS_COMPLETED)

    def test_worker_cannot_ask_the_user(self):
        asked: list[object] = []
        permissions = Permissions(
            mode="ask",
            asker=lambda request: asked.append(request) or "y",
            workspace=Path.cwd(),
        )

        def respond(messages, system, tools, cancel, index):
            if index == 0:
                return call_reply("read_file", {"path": str(Path.cwd().anchor)})
            return text_reply("报告：读不了工作区外面")

        model = FakeModel(respond)
        manager = self.make_manager(model, permissions=permissions)
        record = manager.spawn("查工作区外面")
        manager.wait([record["agent_id"]], 5_000)

        self.assertEqual(asked, [], "worker 不该抢终端的输入")
        result = model.tool_result(1)
        self.assertTrue(result["is_error"])
        self.assertIn(MSG_NO_TTY, result["content"])


class CapacityTest(ManagerFixture):
    def test_empty_task_is_rejected_without_a_record(self):
        manager = self.make_manager(FakeModel(scripted()))
        with self.assertRaises(ValueError):
            manager.spawn("   ")
        self.assertEqual(manager.records(), [])

    def test_fourth_agent_is_rejected_while_three_run(self):
        entered = threading.Semaphore(0)
        release = threading.Event()

        def respond(messages, system, tools, cancel, index):
            entered.release()
            release.wait(5)
            return text_reply("报告")

        manager = self.make_manager(FakeModel(respond))
        ids = [manager.spawn(f"查第 {index} 件")["agent_id"] for index in range(MAX_CONCURRENT)]
        for _ in range(MAX_CONCURRENT):
            self.assertTrue(entered.acquire(timeout=5), "三个 worker 都该跑到第一次请求")

        with self.assertRaisesRegex(ValueError, "最多"):
            manager.spawn("第四个")
        self.assertEqual(len(manager.records()), MAX_CONCURRENT, "被拒的那次不建记录")
        self.assertEqual(manager.running_count, MAX_CONCURRENT)

        release.set()
        manager.wait(ids, 5_000)
        self.assertEqual(manager.running_count, 0)


class FailureTest(ManagerFixture):
    def test_request_error_becomes_a_failed_record(self):
        def respond(messages, system, tools, cancel, index):
            raise RuntimeError("HTTP 500：boom")

        manager = self.make_manager(FakeModel(respond))
        record = manager.spawn("查")
        timed_out, records = manager.wait([record["agent_id"]], 5_000)

        self.assertFalse(timed_out)
        self.assertEqual(records[0]["status"], STATUS_FAILED)
        self.assertIn("boom", records[0]["error"])
        self.assertTrue(any("失败" in line and "agent_1" in line for line in self.events))

    def test_worker_that_ends_without_text_is_failed(self):
        manager = self.make_manager(
            FakeModel(scripted({"content": [], "stop_reason": "end_turn"}))
        )
        record = manager.spawn("查")
        _, records = manager.wait([record["agent_id"]], 5_000)

        self.assertEqual(records[0]["status"], STATUS_FAILED)
        self.assertIn("没有文本", records[0]["error"])

    def test_worker_that_hits_its_step_limit_is_failed(self):
        manager = self.make_manager(
            FakeModel(lambda messages, system, tools, cancel, index: call_reply("glob", {"pattern": "*.py"})),
            max_steps=2,
        )
        record = manager.spawn("查")
        _, records = manager.wait([record["agent_id"]], 5_000)

        self.assertEqual(records[0]["status"], STATUS_FAILED)
        self.assertIn("步上限", records[0]["error"])


class WaitTest(ManagerFixture):
    def test_wait_collects_several_ids_and_reports_timeout(self):
        started = threading.Event()
        release = threading.Event()

        def respond(messages, system, tools, cancel, index):
            task = task_text(messages)
            if "慢" in task:
                started.set()
                release.wait(5)
            return text_reply("报告：" + task)

        manager = self.make_manager(FakeModel(respond))
        fast = manager.spawn("快")
        slow = manager.spawn("慢")
        self.assertTrue(started.wait(5))
        manager.wait([fast["agent_id"]], 5_000)

        timed_out, first = manager.wait([fast["agent_id"], slow["agent_id"]], 50)
        statuses = {record["agent_id"]: record["status"] for record in first}
        self.assertTrue(timed_out)
        self.assertEqual(statuses[fast["agent_id"]], STATUS_COMPLETED)
        self.assertEqual(statuses[slow["agent_id"]], STATUS_RUNNING)

        release.set()
        timed_out, second = manager.wait([fast["agent_id"], slow["agent_id"]], 5_000)
        self.assertFalse(timed_out)
        results = {record["agent_id"]: record["result"] for record in second}
        self.assertEqual(results[fast["agent_id"]], "报告：快")
        self.assertEqual(results[slow["agent_id"]], "报告：慢")
        self.assertFalse(manager.has_pending(0), "wait 交过的报告不再算待交付")

    def test_wait_on_finished_agents_returns_at_once(self):
        manager = self.make_manager(FakeModel(scripted(text_reply("报告"))))
        record = manager.spawn("查")
        manager.wait([record["agent_id"]], 5_000)

        timed_out, records = manager.wait([record["agent_id"]], 0)
        self.assertFalse(timed_out)
        self.assertEqual(records[0]["status"], STATUS_COMPLETED)

    def test_wait_on_an_unknown_id_is_an_error(self):
        manager = self.make_manager(FakeModel(scripted()))
        with self.assertRaisesRegex(ValueError, "没有这个子 agent"):
            manager.wait(["agent_404"], 0)


class CloseTest(ManagerFixture):
    def test_close_waits_for_the_thread_to_really_exit(self):
        entered = threading.Event()
        release = threading.Event()

        def respond(messages, system, tools, cancel, index):
            entered.set()
            release.wait(5)
            return text_reply("迟到的报告")

        manager = self.make_manager(FakeModel(respond))
        record = manager.spawn("查")
        self.assertTrue(entered.wait(5))

        closer_started = threading.Event()
        close_returned = threading.Event()
        closed: list[dict] = []

        def closer():
            closer_started.set()
            closed.append(manager.close(record["agent_id"]))
            close_returned.set()

        thread = threading.Thread(target=closer)
        thread.start()
        self.assertTrue(closer_started.wait(5))
        self.assertFalse(close_returned.wait(0.05), "close 应该等到线程真的退出")

        release.set()
        self.assertTrue(close_returned.wait(5))
        thread.join(5)
        self.assertEqual(closed[0]["status"], STATUS_CLOSED)
        self.assertEqual(manager.records()[0]["result"], "", "关闭的 worker 不交报告")

    def test_worker_stops_while_waiting_for_the_request(self):
        entered = threading.Event()

        def respond(messages, system, tools, cancel, index):
            entered.set()
            cancel.wait(5)
            raise model_module.Closed("已请求关闭：这次请求没有发出去。")

        manager = self.make_manager(FakeModel(respond))
        record = manager.spawn("查")
        self.assertTrue(entered.wait(5))

        closed = manager.close(record["agent_id"])
        self.assertEqual(closed["status"], STATUS_CLOSED)

    def test_close_on_a_finished_agent_is_safe(self):
        manager = self.make_manager(FakeModel(scripted(text_reply("报告"))))
        record = manager.spawn("查")
        manager.wait([record["agent_id"]], 5_000)

        closed = manager.close(record["agent_id"])
        self.assertEqual(closed["status"], STATUS_COMPLETED)
        self.assertEqual(closed["result"], "报告")

    def test_closed_agent_is_handed_over_and_not_delivered_twice(self):
        entered = threading.Event()

        def respond(messages, system, tools, cancel, index):
            entered.set()
            cancel.wait(5)
            raise model_module.Closed("已请求关闭")

        manager = self.make_manager(FakeModel(respond))
        record = manager.spawn("查")
        self.assertTrue(entered.wait(5))
        manager.close(record["agent_id"])

        self.assertFalse(manager.has_pending(0), "close 交过的东西不再算待交付")


class EventTest(ManagerFixture):
    def test_start_and_finish_lines_carry_id_task_and_status(self):
        manager = self.make_manager(FakeModel(scripted(text_reply("报告"))))
        record = manager.spawn("查 tests 目录")
        manager.wait([record["agent_id"]], 5_000)

        self.assertEqual(len(self.events), 2)
        self.assertIn(record["agent_id"], self.events[0])
        self.assertIn("查 tests 目录", self.events[0])
        self.assertIn("启动", self.events[0])
        self.assertIn(record["agent_id"], self.events[1])
        self.assertIn("完成", self.events[1])

    def test_closed_worker_reports_closed(self):
        entered = threading.Event()

        def respond(messages, system, tools, cancel, index):
            entered.set()
            cancel.wait(5)
            raise model_module.Closed("已请求关闭")

        manager = self.make_manager(FakeModel(respond))
        record = manager.spawn("查")
        self.assertTrue(entered.wait(5))
        manager.close(record["agent_id"])

        self.assertIn(f"← {record['agent_id']} 已关闭", self.events)

class ReportTextTest(unittest.TestCase):
    def test_report_text_carries_status_result_and_error(self):
        text = format_reports(
            [
                {
                    "agent_id": "agent_1",
                    "task": "查 tests",
                    "status": STATUS_COMPLETED,
                    "result": "9 个测试文件",
                    "error": "",
                },
                {
                    "agent_id": "agent_2",
                    "task": "查 docs",
                    "status": STATUS_FAILED,
                    "result": "",
                    "error": "HTTP 500",
                },
                {
                    "agent_id": "agent_3",
                    "task": "查别的",
                    "status": STATUS_CLOSED,
                    "result": "",
                    "error": "",
                },
            ]
        )

        self.assertIn("### agent_1 · 完成", text)
        self.assertIn("9 个测试文件", text)
        self.assertIn("错误：HTTP 500", text)
        self.assertIn("没有报告", text)


# ---------------------------------------------------------------------------
# 共享的请求锁：主 agent 和子 agent 一次只发一份请求
# ---------------------------------------------------------------------------


class BlockingModel(model_module.Model):
    """把 HTTP 换成「进去就卡住」，好观察谁在等锁。"""

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__(CONFIG)
        self.entered = entered
        self.release = release
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def send(self, messages, system, tools):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.entered.set()
        self.release.wait(5)
        with self._guard:
            self.active -= 1
        return text_reply("ok")


class SharedLockTest(unittest.TestCase):
    def test_only_one_request_is_in_flight(self):
        entered = threading.Event()
        release = threading.Event()
        shared = BlockingModel(entered, release)

        first = threading.Thread(target=lambda: shared.call([], "sys", []))
        first.start()
        self.assertTrue(entered.wait(5))

        second_done = threading.Event()
        second_ready = threading.Event()

        def second():
            second_ready.set()
            shared.call([], "sys", [])
            second_done.set()

        thread = threading.Thread(target=second)
        thread.start()
        self.assertTrue(second_ready.wait(5))
        self.assertFalse(second_done.wait(0.05), "第二份请求必须等锁")

        release.set()
        self.assertTrue(second_done.wait(5))
        first.join(5)
        thread.join(5)
        self.assertEqual(shared.max_active, 1)

    def test_caller_can_be_cancelled_while_waiting_for_the_lock(self):
        entered = threading.Event()
        release = threading.Event()
        shared = BlockingModel(entered, release)

        first = threading.Thread(target=lambda: shared.call([], "sys", []))
        first.start()
        self.assertTrue(entered.wait(5))

        cancel = threading.Event()
        errors: list[Exception] = []
        finished = threading.Event()
        ready = threading.Event()

        def second():
            ready.set()
            try:
                shared.call([], "sys", [], cancel)
            except model_module.Closed as error:
                errors.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=second)
        thread.start()
        self.assertTrue(ready.wait(5))
        cancel.set()

        self.assertTrue(finished.wait(5), "等锁的时候应该能停下来")
        self.assertEqual(len(errors), 1)
        self.assertEqual(shared.max_active, 1)

        release.set()
        first.join(5)

    def test_a_cancelled_request_is_never_sent(self):
        calls: list[object] = []

        class Counting(model_module.Model):
            def send(self, messages, system, tools):
                calls.append(messages)
                return text_reply("不该发出去")

        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(model_module.Closed):
            Counting(CONFIG).call([], "sys", [], cancel)
        self.assertEqual(calls, [])


# ---------------------------------------------------------------------------
# 主回合收尾：自动收报告、综合、失败兜底、Ctrl-C、/new
# ---------------------------------------------------------------------------


class TurnFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.permissions = Permissions(mode="auto", workspace=Path.cwd())
        self.plan = PlanManager()
        update_plan.bind(self.plan)
        self.stats = SessionStats(model=CONFIG["model"], workspace=str(Path.cwd()))
        self.events: list[str] = []

    def tearDown(self) -> None:
        update_plan.bind(None)
        bind_agents(None)

    def make_session(self, main_model, worker_model):
        subagents = SubAgentManager(
            model=worker_model,
            permissions=self.permissions,
            workspace=Path.cwd(),
            stats=self.stats,
            emit=self.events.append,
        )
        bind_agents(subagents)
        return cli.Session(
            config=CONFIG,
            model=main_model,
            permissions=self.permissions,
            plan=self.plan,
            subagents=subagents,
            stats=self.stats,
        )

    def run_turn(self, session: cli.Session, task: str = "调查一下") -> dict:
        messages: list = []
        shown: list[str] = []
        out, err = io.StringIO(), io.StringIO()

        def fake_render(text, stream=None):
            shown.append(text)

        with (
            patch.object(cli, "render_markdown", fake_render),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.run_turn(messages, task, session)
        return {
            "code": code,
            "messages": messages,
            "shown": shown,
            "stdout": out.getvalue(),
            "stderr": err.getvalue(),
        }


class WrapUpTest(TurnFixture):
    def test_reports_are_collected_and_synthesized_before_the_answer(self):
        worker = FakeModel(scripted(text_reply("worker 报告：tests 里 9 个文件")))
        main = FakeModel(
            scripted(
                call_reply("spawn_agent", {"task": "看 tests 目录"}),
                text_reply("我先派了一个 agent 去查。"),
                text_reply("综合结论：tests 里有 9 个测试文件。"),
            )
        )
        session = self.make_session(main, worker)
        result = self.run_turn(session)

        self.assertEqual(result["code"], 0)
        self.assertEqual(len(main.calls), 3, "第三次请求是收尾综合")
        self.assertEqual(main.calls[2]["tools"], [], "综合请求不带工具")
        self.assertIn("worker 报告", task_text(main.calls[2]["messages"]))

        self.assertEqual(result["shown"], ["综合结论：tests 里有 9 个测试文件。"])
        self.assertNotIn("我先派了一个 agent 去查。", result["shown"])
        self.assertIn(
            "我先派了一个 agent 去查。",
            json.dumps(result["messages"], ensure_ascii=False),
        )

        records = session.subagents.records()
        self.assertEqual(records[0]["status"], STATUS_COMPLETED)
        self.assertFalse(session.subagents.has_pending(1), "报告已经交付")
        self.assertEqual(self.stats.requests, 4, "主 agent 三次 + worker 一次")

    def test_step_limit_still_gets_one_extra_synthesis_request(self):
        worker = FakeModel(scripted(text_reply("worker 报告：查完了")))
        replies = [call_reply("spawn_agent", {"task": "查"})]
        replies += [call_reply("list_agents", {}) for _ in range(cli.MAX_STEPS - 1)]
        replies.append(text_reply("综合结论：报告有了，但步数用完了。"))
        main = FakeModel(scripted(*replies))
        session = self.make_session(main, worker)
        result = self.run_turn(session)

        self.assertEqual(result["code"], 0)
        self.assertEqual(len(main.calls), cli.MAX_STEPS + 1, "用满步数后额外给一次综合")
        self.assertEqual(main.calls[cli.MAX_STEPS]["tools"], [])
        self.assertEqual(result["shown"], ["综合结论：报告有了，但步数用完了。"])

    def test_synthesis_failure_shows_raw_reports_and_marks_the_turn_unfinished(self):
        worker = FakeModel(scripted(text_reply("worker 报告：路径 A 有问题")))

        def main_respond(messages, system, tools, cancel, index):
            if index == 0:
                return call_reply("spawn_agent", {"task": "看路径 A"})
            if index == 1:
                return text_reply("等我派出去的那个 agent。")
            raise RuntimeError("连不上 https://example.invalid")

        main = FakeModel(main_respond)
        session = self.make_session(main, worker)
        result = self.run_turn(session)

        self.assertEqual(result["code"], 1)
        self.assertIn("本回合未完成", result["stderr"])
        self.assertIn("综合请求失败", result["stderr"])
        self.assertTrue(any("worker 报告" in text for text in result["shown"]))
        self.assertTrue(any("等我派出去的那个 agent" in text for text in result["shown"]))

    def test_synthesis_that_asks_for_tools_does_not_fake_success(self):
        worker = FakeModel(scripted(text_reply("worker 报告：查完了")))
        main = FakeModel(
            scripted(
                call_reply("spawn_agent", {"task": "查"}),
                text_reply("等我派出去的那个 agent。"),
                call_reply("glob", {"pattern": "*"}),
            )
        )
        session = self.make_session(main, worker)
        result = self.run_turn(session)

        self.assertEqual(result["code"], 1)
        self.assertIn("模型没有给出有效答复", result["stderr"])
        self.assertIn("worker 报告", result["stderr"] + "".join(result["shown"]))
        last = result["messages"][-1]
        self.assertEqual(last["role"], "user", "没有配对的 tool_use 不许进历史")

    def test_truncated_synthesis_is_not_presented_as_the_final_answer(self):
        worker = FakeModel(scripted(text_reply("worker 报告：查完了")))
        main = FakeModel(
            scripted(
                call_reply("spawn_agent", {"task": "查"}),
                text_reply("等我派出去的那个 agent。"),
                {"content": [{"type": "text", "text": "半截结论"}], "stop_reason": "max_tokens"},
            )
        )
        session = self.make_session(main, worker)
        result = self.run_turn(session)

        self.assertEqual(result["code"], 1)
        self.assertIn("max_tokens", result["stderr"])
        self.assertNotIn("半截结论", result["shown"])
        self.assertTrue(any("worker 报告" in text for text in result["shown"]))

    def test_ctrl_c_rolls_back_the_turn_and_requests_close(self):
        entered = threading.Event()
        release = threading.Event()

        def worker_respond(messages, system, tools, cancel, index):
            entered.set()
            release.wait(5)
            return text_reply("迟到报告")

        worker = FakeModel(worker_respond)

        def main_respond(messages, system, tools, cancel, index):
            if index == 0:
                return call_reply("spawn_agent", {"task": "查"})
            raise KeyboardInterrupt()

        session = self.make_session(FakeModel(main_respond), worker)
        messages: list = []
        with (
            patch.object(cli, "render_markdown", lambda text, stream=None: None),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                cli.run_turn(messages, "调查", session)

        self.assertEqual(messages, [], "历史回到这一轮开始前")
        self.assertTrue(entered.wait(5))
        release.set()
        session.subagents.close_all()
        self.assertEqual(session.subagents.records()[0]["status"], STATUS_CLOSED)


class NewSessionTest(TurnFixture):
    def test_new_closes_workers_and_clears_records(self):
        entered = threading.Event()

        def worker_respond(messages, system, tools, cancel, index):
            entered.set()
            cancel.wait(5)
            raise model_module.Closed("已请求关闭")

        session = self.make_session(FakeModel(scripted()), FakeModel(worker_respond))
        session.subagents.spawn("查一件事")
        self.assertTrue(entered.wait(5))
        session.plan.update([{"title": "读入口", "status": "pending"}])

        out = io.StringIO()
        with (
            patch("builtins.input", side_effect=["/new", "/exit"]),
            patch.object(cli, "render_markdown", lambda text, stream=None: None),
            contextlib.redirect_stdout(out),
        ):
            code = cli.repl(session)

        self.assertEqual(code, 0)
        self.assertEqual(session.subagents.records(), [], "记录清空了")
        self.assertTrue(session.plan.is_empty)
        self.assertIn("子 agent", out.getvalue())


if __name__ == "__main__":
    unittest.main()
