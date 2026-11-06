"""update_plan 工具：接参、交给 PlanManager、出错变成 is_error 结果。"""

from __future__ import annotations

import json
import unittest

from carrycode.plan import PlanManager
from carrycode.tools import TOOLS, update_plan


class SchemaTest(unittest.TestCase):
    def test_registered_as_a_read_only_tool(self):
        spec = TOOLS["update_plan"]
        self.assertEqual(spec["risk"], "read")
        self.assertEqual(spec["paths"], ())

    def test_schema_states_the_whole_list_contract(self):
        schema = update_plan.SCHEMA["input_schema"]
        self.assertEqual(schema["required"], ["todos"])
        item = schema["properties"]["todos"]["items"]
        self.assertEqual(
            item["properties"]["status"]["enum"], ["pending", "in_progress", "completed"]
        )
        self.assertIn("完整", update_plan.SCHEMA["description"])
        self.assertIn("不要带 id", update_plan.SCHEMA["description"])


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = PlanManager()
        update_plan.bind(self.plan)

    def tearDown(self) -> None:
        update_plan.bind(None)

    def test_returns_the_list_with_ids(self):
        text, is_error = update_plan.run({"todos": [{"title": "读入口", "status": "pending"}]})
        self.assertFalse(is_error)
        document = json.loads(text)
        self.assertEqual(document[0]["title"], "读入口")
        self.assertTrue(document[0]["id"])
        self.assertEqual(self.plan.snapshot(), document)

    def test_invalid_input_becomes_an_error_result(self):
        text, is_error = update_plan.run(
            {
                "todos": [
                    {"title": "a", "status": "in_progress"},
                    {"title": "b", "status": "in_progress"},
                ]
            }
        )
        self.assertTrue(is_error)
        self.assertIn("in_progress", text)
        self.assertTrue(self.plan.is_empty, "校验失败不该改到计划")

    def test_missing_todos_is_an_error_result(self):
        text, is_error = update_plan.run({})
        self.assertTrue(is_error)
        self.assertIn("todos", text)

    def test_unbound_tool_tells_the_model_to_move_on(self):
        update_plan.bind(None)
        text, is_error = update_plan.run({"todos": []})
        self.assertTrue(is_error)
        self.assertIn("没接上", text)


if __name__ == "__main__":
    unittest.main()
