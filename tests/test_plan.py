"""计划状态：整表替换、id 规则、校验失败不动状态、订阅、重置。

跑法（在 CarryCode 仓库根）：

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import unittest

from carrycode.plan import MAX_TODOS, PlanError, PlanManager, format_plan


class UpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = PlanManager()

    def test_starts_empty(self):
        self.assertTrue(self.plan.is_empty)
        self.assertEqual(self.plan.snapshot(), [])

    def test_update_replaces_the_whole_list_and_strips_titles(self):
        first = self.plan.update(
            [
                {"title": "  读入口  ", "status": "pending"},
                {"title": "写测试", "status": "in_progress"},
            ]
        )
        self.assertEqual([todo["title"] for todo in first], ["读入口", "写测试"])
        self.assertEqual(len({todo["id"] for todo in first}), 2)

        second = self.plan.update(
            [
                {"id": first[1]["id"], "title": "写测试并从失败开始", "status": "completed"},
                {"id": first[0]["id"], "title": "读入口", "status": "pending"},
            ]
        )
        # 整表替换：顺序按提交的来，id 原样保留
        self.assertEqual([todo["status"] for todo in second], ["completed", "pending"])
        self.assertEqual(second[0]["id"], first[1]["id"])
        self.assertEqual(second[0]["title"], "写测试并从失败开始")

    def test_new_item_without_id_gets_a_fresh_one(self):
        first = self.plan.update([{"title": "a", "status": "pending"}])
        second = self.plan.update([*first, {"title": "b", "status": "pending"}])
        self.assertEqual(second[0]["id"], first[0]["id"])
        self.assertTrue(second[1]["id"])
        self.assertNotEqual(second[1]["id"], first[0]["id"])

    def test_empty_list_clears_the_plan(self):
        self.plan.update([{"title": "a", "status": "pending"}])
        self.assertEqual(self.plan.update([]), [])
        self.assertTrue(self.plan.is_empty)

    def test_unknown_id_is_rejected_with_the_reason(self):
        with self.assertRaises(PlanError) as caught:
            self.plan.update([{"id": "stale", "title": "旧条目", "status": "pending"}])
        self.assertIn("stale", str(caught.exception))

    def test_duplicate_id_is_rejected(self):
        todo = self.plan.update([{"title": "a", "status": "pending"}])[0]
        with self.assertRaises(PlanError):
            self.plan.update([todo, todo])

    def test_only_one_in_progress(self):
        with self.assertRaises(PlanError) as caught:
            self.plan.update(
                [
                    {"title": "a", "status": "in_progress"},
                    {"title": "b", "status": "in_progress"},
                ]
            )
        self.assertIn("in_progress", str(caught.exception))

    def test_bad_shapes_are_rejected(self):
        cases = {
            "不是数组": "不是数组",
            "元素不是对象": [1],
            "缺 title": [{"status": "pending"}],
            "title 空白": [{"title": "   ", "status": "pending"}],
            "status 不认识": [{"title": "a", "status": "active"}],
            "id 是空串": [{"title": "a", "status": "pending", "id": ""}],
            "超过上限": [{"title": "a", "status": "pending"}] * (MAX_TODOS + 1),
        }
        for name, todos in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(PlanError):
                    self.plan.update(todos)

    def test_rejected_update_changes_nothing_and_does_not_notify(self):
        good = self.plan.update([{"title": "保留我", "status": "pending"}])
        calls: list[int] = []
        unsubscribe = self.plan.subscribe(lambda: calls.append(1))
        with self.assertRaises(PlanError):
            self.plan.update([{"id": "nope", "title": "x", "status": "pending"}])
        self.assertEqual(self.plan.snapshot(), good)
        self.assertEqual(calls, [])
        unsubscribe()

    def test_snapshot_is_a_copy(self):
        self.plan.update([{"title": "原始", "status": "pending"}])
        snapshot = self.plan.snapshot()
        snapshot[0]["title"] = "改坏了"
        snapshot.clear()
        self.assertEqual(self.plan.snapshot()[0]["title"], "原始")


class SubscribeTest(unittest.TestCase):
    def test_listeners_fire_on_update_and_reset(self):
        plan = PlanManager()
        calls: list[int] = []
        unsubscribe = plan.subscribe(lambda: calls.append(len(plan.snapshot())))
        plan.update([{"title": "a", "status": "pending"}])
        plan.reset()
        unsubscribe()
        plan.update([{"title": "b", "status": "pending"}])
        self.assertEqual(calls, [1, 0])

    def test_reset_on_empty_plan_still_notifies(self):
        # 规则简单一点：写操作一定通知，界面不用猜“这次要不要刷新”
        plan = PlanManager()
        calls: list[int] = []
        plan.subscribe(lambda: calls.append(1))
        plan.reset()
        self.assertEqual(calls, [1])

    def test_new_session_has_no_todos_and_no_old_ids(self):
        plan = PlanManager()
        old = plan.update([{"title": "上一场会话的", "status": "in_progress"}])[0]
        plan.reset()  # 新会话 = 新清单
        self.assertEqual(plan.snapshot(), [])
        with self.assertRaises(PlanError):
            plan.update([{**old, "status": "pending"}])


class FormatTest(unittest.TestCase):
    def test_empty_plan_says_so(self):
        self.assertEqual(format_plan([]), "当前没有计划。")

    def test_three_markers_and_progress(self):
        todos = [
            {"id": "a1", "title": "读入口", "status": "pending"},
            {"id": "b2", "title": "写测试", "status": "in_progress"},
            {"id": "c3", "title": "跑一遍", "status": "completed"},
        ]
        text = format_plan(todos)
        self.assertIn("[ ] 读入口 (a1)", text)
        self.assertIn("[>] 写测试 (b2)", text)
        self.assertIn("[x] 跑一遍 (c3)", text)
        self.assertIn("1/3", text)


if __name__ == "__main__":
    unittest.main()
