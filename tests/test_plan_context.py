"""注入：空计划只在那一份请求里提一句；有计划时每份请求都带最新清单。"""

from __future__ import annotations

import unittest

from carrycode.plan import PlanManager
from carrycode.plan_context import HINT_NO_PLAN, PLAN_RULES, with_plan_context


class EmptyPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = PlanManager()

    def test_no_hint_leaves_system_untouched(self):
        self.assertEqual(with_plan_context("基础提示", self.plan), "基础提示")

    def test_hint_appends_one_sentence(self):
        text = with_plan_context("基础提示", self.plan, hint=True)
        self.assertTrue(text.startswith("基础提示"))
        self.assertIn(HINT_NO_PLAN, text)

    def test_hint_disappears_once_a_plan_exists(self):
        self.plan.update([{"title": "读入口", "status": "pending"}])
        text = with_plan_context("基础提示", self.plan, hint=True)
        self.assertNotIn(HINT_NO_PLAN, text)
        self.assertIn("读入口", text)


class FilledPlanTest(unittest.TestCase):
    def test_latest_snapshot_is_read_on_every_call(self):
        plan = PlanManager()
        todo = plan.update([{"title": "读入口", "status": "in_progress"}])[0]

        first = with_plan_context("基础提示", plan)
        plan.update([{**todo, "status": "completed"}])
        second = with_plan_context("基础提示", plan)

        self.assertIn("[>] 读入口", first)
        self.assertIn("[x] 读入口", second)
        self.assertIn(todo["id"], second)

    def test_rules_ride_along_with_the_list(self):
        plan = PlanManager()
        plan.update([{"title": "a", "status": "pending"}])
        self.assertIn(PLAN_RULES, with_plan_context("基础提示", plan))

    def test_plan_block_sits_after_the_base_system(self):
        plan = PlanManager()
        plan.update([{"title": "a", "status": "pending"}])
        text = with_plan_context("基础提示", plan)
        self.assertEqual(text.index("基础提示"), 0)
        self.assertLess(text.index("基础提示"), text.index("当前计划"))


if __name__ == "__main__":
    unittest.main()
