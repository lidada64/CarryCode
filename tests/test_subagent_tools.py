"""四个子 agent 管理工具：schema、参数校验、未绑定兜底、JSON 结果。

管理器用一个桩对象：这里测的是工具这一层，真正的线程/状态在 test_subagents.py 里测。
"""

from __future__ import annotations

import json
import unittest

from carrycode import __main__ as cli
from carrycode.subagents import MAX_CONCURRENT
from carrycode.tools import TOOLS, bind_agents, close_agent, list_agents, spawn_agent, wait_agent


class StubManager:
    """够四个工具用的假管理器：记下调用，按需要抛 ValueError。"""

    def __init__(self) -> None:
        self.agents: list[dict] = []
        self.spawned: list[str] = []
        self.wait_calls: list[tuple[list[str], int]] = []
        self.closed: list[str] = []
        self.timed_out = False
        self.failures: dict[str, str] = {}

    @property
    def max_concurrent(self) -> int:
        return 3

    @property
    def running_count(self) -> int:
        return sum(1 for agent in self.agents if agent["status"] == "running")

    def _maybe_fail(self, action: str) -> None:
        if action in self.failures:
            raise ValueError(self.failures[action])

    def spawn(self, task: str) -> dict:
        self._maybe_fail("spawn")
        record = {
            "agent_id": f"agent_{len(self.agents) + 1}",
            "task": task,
            "status": "running",
            "result": "",
            "error": "",
        }
        self.agents.append(record)
        self.spawned.append(task)
        return dict(record)

    def records(self) -> list[dict]:
        return [dict(agent) for agent in self.agents]

    def wait(self, agent_ids: list[str], timeout_ms: int) -> tuple[bool, list[dict]]:
        self._maybe_fail("wait")
        self.wait_calls.append((list(agent_ids), timeout_ms))
        return self.timed_out, [
            dict(agent) for agent in self.agents if agent["agent_id"] in agent_ids
        ]

    def close(self, agent_id: str) -> dict:
        self._maybe_fail("close")
        record = next((agent for agent in self.agents if agent["agent_id"] == agent_id), None)
        if record is None:
            raise ValueError(f"没有这个子 agent：{agent_id}")
        record["status"] = "closed"
        self.closed.append(agent_id)
        return dict(record)


class BindFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = StubManager()
        bind_agents(self.manager)

    def tearDown(self) -> None:
        bind_agents(None)


class SchemaTest(unittest.TestCase):
    def test_four_tools_are_registered_as_read_only(self):
        for name in ("spawn_agent", "list_agents", "wait_agent", "close_agent"):
            spec = TOOLS[name]
            self.assertEqual(spec["risk"], "read", f"{name} 不该要审批")
            self.assertEqual(spec["paths"], (), f"{name} 没有路径参数")

    def test_list_agents_is_the_only_no_arg_tool(self):
        schema = list_agents.SCHEMA["input_schema"]
        self.assertEqual(schema["properties"], {})
        self.assertNotIn("required", schema)
        self.assertTrue(spawn_agent.SCHEMA["input_schema"]["required"] == ["task"])
        self.assertTrue(wait_agent.SCHEMA["input_schema"]["required"] == ["agent_ids"])
        self.assertTrue(close_agent.SCHEMA["input_schema"]["required"] == ["agent_id"])

    def test_wait_agent_states_the_timeout_range(self):
        timeout = wait_agent.SCHEMA["input_schema"]["properties"]["timeout_ms"]
        self.assertEqual(timeout["type"], "integer")
        self.assertEqual(timeout["minimum"], 0)
        self.assertEqual(timeout["maximum"], 120_000)

    def test_descriptions_agree_with_the_concurrency_limit(self):
        """改了 MAX_CONCURRENT，给模型看的说明也得跟着改。"""
        self.assertIn(str(MAX_CONCURRENT), spawn_agent.SCHEMA["description"])
        self.assertIn(str(MAX_CONCURRENT), cli.system_prompt())


class UnboundTest(unittest.TestCase):
    def setUp(self) -> None:
        bind_agents(None)

    def test_every_tool_says_it_is_not_wired_up(self):
        for tool, args in (
            (spawn_agent, {"task": "查"}),
            (list_agents, {}),
            (wait_agent, {"agent_ids": ["agent_1"]}),
            (close_agent, {"agent_id": "agent_1"}),
        ):
            text, is_error = tool.run(args)
            self.assertTrue(is_error, tool.NAME)
            self.assertIn("没接上", text)


class SpawnToolTest(BindFixture):
    def test_empty_task_creates_nothing(self):
        text, is_error = spawn_agent.run({"task": "   "})
        self.assertTrue(is_error)
        self.assertIn("task", text)
        self.assertEqual(self.manager.spawned, [])

    def test_success_returns_the_id_and_running_state(self):
        text, is_error = spawn_agent.run({"task": "看 tests 目录"})
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(payload["agent_id"], "agent_1")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(self.manager.spawned, ["看 tests 目录"])

    def test_capacity_error_is_passed_to_the_model(self):
        self.manager.failures["spawn"] = "最多 3 个子 agent 同时跑"
        text, is_error = spawn_agent.run({"task": "查"})
        self.assertTrue(is_error)
        self.assertIn("最多", text)


class ListToolTest(BindFixture):
    def test_empty_list_still_reports_capacity(self):
        text, is_error = list_agents.run({})
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(payload["agents"], [])
        self.assertEqual(payload["running"], 0)
        self.assertEqual(payload["max_concurrent"], 3)

    def test_list_carries_status_and_error_but_not_the_report(self):
        self.manager.agents = [
            {
                "agent_id": "agent_1",
                "task": "查 tests",
                "status": "failed",
                "result": "很长的完整报告",
                "error": "HTTP 500",
            }
        ]
        text, is_error = list_agents.run({})
        payload = json.loads(text)
        self.assertFalse(is_error)
        self.assertEqual(payload["agents"][0]["status"], "failed")
        self.assertEqual(payload["agents"][0]["error"], "HTTP 500")
        self.assertNotIn("result", payload["agents"][0])
        self.assertNotIn("很长的完整报告", text)


class WaitToolTest(BindFixture):
    def _record(self, agent_id: str = "agent_1", status: str = "completed") -> None:
        self.manager.agents.append(
            {
                "agent_id": agent_id,
                "task": "查",
                "status": status,
                "result": "报告内容",
                "error": "",
            }
        )

    def test_default_timeout_is_thirty_seconds(self):
        self._record()
        text, is_error = wait_agent.run({"agent_ids": ["agent_1"]})
        self.assertFalse(is_error)
        self.assertEqual(self.manager.wait_calls[-1], (["agent_1"], 30_000))
        payload = json.loads(text)
        self.assertFalse(payload["timed_out"])
        self.assertEqual(payload["agents"][0]["result"], "报告内容")

    def test_timeout_accepts_both_ends_of_the_range(self):
        self._record()
        for value in (0, 120_000, "30000"):
            text, is_error = wait_agent.run({"agent_ids": ["agent_1"], "timeout_ms": value})
            self.assertFalse(is_error, text)

    def test_invalid_timeout_is_a_tool_error(self):
        self._record()
        for value in (-1, 120_001, "abc", 30_000.5, True):
            text, is_error = wait_agent.run({"agent_ids": ["agent_1"], "timeout_ms": value})
            self.assertTrue(is_error, f"{value!r} 该被拒")
            self.assertIn("timeout_ms", text)
        self.assertEqual(self.manager.wait_calls, [], "参数不合法就不该去等")

    def test_agent_ids_must_be_a_non_empty_list_of_strings(self):
        for raw in (None, [], "agent_1", [""], [1]):
            text, is_error = wait_agent.run({"agent_ids": raw})
            self.assertTrue(is_error, f"{raw!r} 该被拒")
            self.assertIn("agent_ids", text)
        self.assertEqual(self.manager.wait_calls, [])

    def test_unknown_id_becomes_a_tool_error(self):
        self.manager.failures["wait"] = "没有这个子 agent：agent_404"
        text, is_error = wait_agent.run({"agent_ids": ["agent_404"]})
        self.assertTrue(is_error)
        self.assertIn("agent_404", text)


class CloseToolTest(BindFixture):
    def test_missing_agent_id_is_an_error(self):
        text, is_error = close_agent.run({})
        self.assertTrue(is_error)
        self.assertIn("agent_id", text)

    def test_unknown_id_is_an_error(self):
        text, is_error = close_agent.run({"agent_id": "agent_404"})
        self.assertTrue(is_error)
        self.assertIn("agent_404", text)

    def test_success_returns_the_final_state(self):
        self.manager.agents = [
            {"agent_id": "agent_1", "task": "查", "status": "running", "result": "", "error": ""}
        ]
        text, is_error = close_agent.run({"agent_id": "agent_1"})
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(payload["agent_id"], "agent_1")
        self.assertEqual(payload["status"], "closed")
        self.assertEqual(self.manager.closed, ["agent_1"])


if __name__ == "__main__":
    unittest.main()
