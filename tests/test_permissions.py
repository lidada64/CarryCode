"""审批的单元测试：全程没有终端。

两个替身：
    假 asker   记录"问了什么"，按预设顺序回答 y / a / n / d
    真工具     用注册表里现成的 spec，不另造——测的就是真账本

跑法（在 CarryCode 仓库根）：

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import unittest
from pathlib import Path

from carrycode.permissions import (
    MSG_NO_TTY,
    MSG_OUTSIDE,
    MSG_READONLY,
    MSG_REMEMBERED,
    Permissions,
    Request,
)
from carrycode.tools import TOOLS, dispatch

WORKSPACE = Path(__file__).resolve().parent.parent


class FakeAsker:
    """假 asker：按顺序发答案，答案用完就一律拒绝。"""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> str:
        self.requests.append(request)
        return self.answers.pop(0) if self.answers else "n"


def make_spec(risk: str = "write", paths=("path",), preview=None, inspect=None):
    return {"risk": risk, "paths": paths, "preview": preview, "inspect": inspect}


class Base(unittest.TestCase):
    def make(self, *answers: str, mode: str = "ask", allow_outside: bool = False):
        asker = FakeAsker(*answers)
        permissions = Permissions(
            mode=mode,
            asker=asker,
            workspace=WORKSPACE,
            allow_outside=allow_outside,
        )
        return permissions, asker

    def inside(self, name: str = "carrycode/tools/base.py") -> dict:
        return {"path": name}

    def outside(self, name: str = "outside.txt") -> dict:
        return {"path": str(WORKSPACE.parent / name)}


class ReadToolTest(Base):
    def test_read_inside_never_asks(self):
        permissions, asker = self.make("y")
        decision = permissions.check("read_file", self.inside(), make_spec("read"))
        self.assertTrue(decision.allowed)
        self.assertEqual(asker.requests, [])

    def test_read_outside_asks_with_only_yes_and_no(self):
        permissions, asker = self.make("y")
        decision = permissions.check("read_file", self.outside(), make_spec("read"))
        self.assertTrue(decision.allowed)
        self.assertEqual(len(asker.requests), 1)
        self.assertEqual(asker.requests[0].options, ("y", "n"))

    def test_read_outside_still_asks_in_auto_mode(self):
        permissions, asker = self.make(mode="auto")
        decision = permissions.check("read_file", self.outside(), make_spec("read"))
        self.assertFalse(decision.allowed)  # 假 asker 没答案 → 一律拒绝
        self.assertEqual(len(asker.requests), 1)


class WriteToolTest(Base):
    def test_write_asks_and_yes_allows(self):
        permissions, asker = self.make("y")
        decision = permissions.check("write_file", self.inside(), make_spec("write"))
        self.assertTrue(decision.allowed)
        self.assertEqual(len(asker.requests), 1)
        self.assertEqual(asker.requests[0].options, ("y", "a", "n", "d"))

    def test_no_denies(self):
        permissions, asker = self.make("n")
        decision = permissions.check("write_file", self.inside(), make_spec("write"))
        self.assertFalse(decision.allowed)
        self.assertIn("denied", decision.message)

    def test_a_remembers_for_the_session(self):
        permissions, asker = self.make("a")
        permissions.check("write_file", self.inside(), make_spec("write"))
        decision = permissions.check("write_file", self.inside(), make_spec("write"))
        self.assertTrue(decision.allowed)
        self.assertEqual(len(asker.requests), 1, "按过 a 之后不该再问")

    def test_d_remembers_the_refusal(self):
        permissions, asker = self.make("d")
        permissions.check("write_file", self.inside(), make_spec("write"))
        decision = permissions.check("write_file", self.inside(), make_spec("write"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.message, MSG_REMEMBERED)
        self.assertEqual(len(asker.requests), 1, "按过 d 之后不该再问")

    def test_memory_does_not_leak_across_tools(self):
        permissions, asker = self.make("a", "y")
        permissions.check("write_file", self.inside(), make_spec("write"))
        permissions.check("edit_file", self.inside(), make_spec("write"))
        self.assertEqual(len(asker.requests), 2, "write_file 的 a 不该让 edit_file 也免问")


class ModeTest(Base):
    def test_readonly_denies_without_asking(self):
        permissions, asker = self.make("y", mode="readonly")
        decision = permissions.check("write_file", self.inside(), make_spec("write"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.message, MSG_READONLY)
        self.assertEqual(asker.requests, [])

    def test_readonly_still_allows_reading(self):
        permissions, asker = self.make(mode="readonly")
        decision = permissions.check("read_file", self.inside(), make_spec("read"))
        self.assertTrue(decision.allowed)

    def test_auto_never_asks_for_normal_writes(self):
        permissions, asker = self.make(mode="auto")
        decision = permissions.check("write_file", self.inside(), make_spec("write"))
        self.assertTrue(decision.allowed)
        self.assertEqual(asker.requests, [])

    def test_no_asker_means_deny(self):
        permissions = Permissions(mode="ask", asker=None, workspace=WORKSPACE)
        decision = permissions.check("write_file", self.inside(), make_spec("write"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.message, MSG_NO_TTY)


class BoundaryTest(Base):
    def test_write_outside_is_a_hard_wall(self):
        for mode in ("ask", "auto"):
            permissions, asker = self.make("y", mode=mode)
            decision = permissions.check("write_file", self.outside(), make_spec("write"))
            self.assertFalse(decision.allowed, f"{mode} 模式下写越界也该拒")
            self.assertEqual(decision.message, MSG_OUTSIDE)
            self.assertEqual(asker.requests, [], "硬边界不该问人")

    def test_allow_outside_opens_the_wall(self):
        permissions, asker = self.make("y", allow_outside=True)
        decision = permissions.check("write_file", self.outside(), make_spec("write"))
        self.assertTrue(decision.allowed)

    def test_secret_file_is_never_readable(self):
        permissions, asker = self.make("y")
        for name in (".env", ".env.local", ".ssh/id_rsa"):
            decision = permissions.check("read_file", {"path": name}, make_spec("read"))
            self.assertFalse(decision.allowed, f"{name} 不该读得到")
        self.assertEqual(asker.requests, [], "禁区不该问人")

    def test_env_example_is_readable(self):
        permissions, asker = self.make("y")
        decision = permissions.check("read_file", {"path": ".env.example"}, make_spec("read"))
        self.assertTrue(decision.allowed)

    def test_inspect_cannot_lower_the_declared_risk(self):
        permissions, asker = self.make("y")
        spec = make_spec("write", inspect=lambda args: ("read", []))
        decision = permissions.check("fake", self.inside(), spec)
        self.assertFalse(decision.allowed)


class BashTest(Base):
    def test_banned_command_is_refused_without_asking(self):
        for command in ("sudo rm -rf /", "rm -rf build", "format D:", "shutdown /s"):
            for mode in ("ask", "auto"):
                permissions, asker = self.make("y", mode=mode)
                decision = permissions.check("bash", {"command": command}, TOOLS["bash"])
                self.assertFalse(decision.allowed, f"{command} 在 {mode} 下也该拒")
                self.assertEqual(asker.requests, [], f"{command} 不该问人")

    def test_irreversible_asks_even_in_auto_mode(self):
        permissions, asker = self.make("n", mode="auto")
        decision = permissions.check("bash", {"command": "git push"}, TOOLS["bash"])
        self.assertFalse(decision.allowed)
        self.assertEqual(len(asker.requests), 1, "不可逆在 --yes 下也要问一次")
        self.assertEqual(asker.requests[0].options, ("y", "n"))

    def test_irreversible_ignores_session_memory(self):
        permissions, asker = self.make("a", "n")
        permissions.check("bash", {"command": "git status"}, TOOLS["bash"])
        permissions.check("bash", {"command": "git push"}, TOOLS["bash"])
        self.assertEqual(len(asker.requests), 2, "按过 a 之后，不可逆命令仍然要问")

    def test_memory_groups_bash_by_first_word(self):
        permissions, asker = self.make("a", "y")
        permissions.check("bash", {"command": "git status"}, TOOLS["bash"])
        permissions.check("bash", {"command": "git log --oneline"}, TOOLS["bash"])
        self.assertEqual(len(asker.requests), 1, "git 已免问，git log 不该再问")
        permissions.check("bash", {"command": "npm --version"}, TOOLS["bash"])
        self.assertEqual(len(asker.requests), 2, "npm 没免问，仍然要问")

    def test_env_in_command_is_refused(self):
        permissions, asker = self.make("y")
        for command in ("type .env", "type C:\\work\\.env", "cat id_rsa"):
            decision = permissions.check("bash", {"command": command}, TOOLS["bash"])
            self.assertFalse(decision.allowed, f"{command} 碰到了禁区")
        self.assertEqual(asker.requests, [])

    def test_commit_message_mentioning_env_is_fine(self):
        permissions, asker = self.make("y")
        decision = permissions.check(
            "bash", {"command": 'git commit -m "docs: explain .env"'}, TOOLS["bash"]
        )
        self.assertTrue(decision.allowed, "提交信息里出现 .env 不该被当成读禁区")
        self.assertEqual(len(asker.requests), 1)

    def test_quoted_sudo_is_still_caught(self):
        permissions, asker = self.make("y")
        decision = permissions.check("bash", {"command": '"sudo" -u root ls'}, TOOLS["bash"])
        self.assertFalse(decision.allowed)


class DispatchTest(Base):
    def test_denial_becomes_a_normal_tool_result(self):
        permissions = Permissions(mode="readonly", asker=FakeAsker(), workspace=WORKSPACE)
        result = dispatch(
            {"name": "write_file", "input": {"path": "x.txt", "content": "hi"}, "id": "t1"},
            permissions,
        )
        self.assertEqual(result["tool_use_id"], "t1")
        self.assertTrue(result["is_error"])
        self.assertIn("read-only", result["content"])

    def test_unknown_tool_still_reported(self):
        permissions = Permissions(mode="auto", asker=FakeAsker(), workspace=WORKSPACE)
        result = dispatch({"name": "nope", "input": {}, "id": "t2"}, permissions)
        self.assertTrue(result["is_error"])

    def test_allowed_call_actually_runs(self):
        permissions = Permissions(mode="auto", asker=FakeAsker(), workspace=WORKSPACE)
        result = dispatch(
            {"name": "read_file", "input": {"path": "carrycode/__init__.py"}, "id": "t3"},
            permissions,
        )
        self.assertFalse(result["is_error"], result["content"])
        self.assertIn("__version__", result["content"])


if __name__ == "__main__":
    unittest.main()
