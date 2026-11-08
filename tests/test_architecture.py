"""架构约束：工具层只能往下 import；清单顺序固定；每个工具三件套齐全；
每个工具自己声明风险档与受约束的路径入参（安全账本）。

跑法（在 CarryCode 仓库根）：

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import unittest
from pathlib import Path

from carrycode.tools import TOOL_SCHEMAS, TOOLS
from carrycode.tools.tool import RISK_LEVELS

TOOLS_DIR = Path(__file__).resolve().parent.parent / "carrycode" / "tools"

# 顺序 = 发给模型的顺序。改这里等于改请求开头，会让服务端记住的前缀失效，
# 所以顺序变化必须是一次明确的、有人盯着的变更。
EXPECTED_ORDER = [
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "update_plan",
    "spawn_agent",
    "list_agents",
    "wait_agent",
    "close_agent",
]

# 安全账本：每个工具说自己多危险、哪些入参受工作区约束。
# 这张表就是审批的基础，改它等于改安全边界，必须是一次有人盯着的变更。
#   read  只读；write 改文件；exec 执行命令；irreversible 不可撤销
EXPECTED_LEDGER = {
    "bash": ("exec", ()),
    "read_file": ("read", ("path",)),
    "write_file": ("write", ("path",)),
    "edit_file": ("write", ("path",)),
    "glob": ("read", ()),
    "grep": ("read", ("path",)),
    # 计划只在会话内存里，不碰文件也不执行命令：写 read，不该为它弹审批
    "update_plan": ("read", ()),
    # 四个管理工具只动内存里的调查线程：worker 自己只读，不该为它们弹审批
    "spawn_agent": ("read", ()),
    "list_agents": ("read", ()),
    "wait_agent": ("read", ()),
    "close_agent": ("read", ()),
}

# 这四个文件是册子、公共件和命令知识，不是具体工具，不要求写 RISK
NOT_TOOLS = {"__init__.py", "agents.py", "base.py", "tool.py", "commands.py"}

# 只有明确不需要参数的工具可以交一份空参数表；别的工具漏写参数表，
# 模型就永远不知道该传什么。这张名单要有人盯着，不能顺手加。
NO_ARG_TOOLS = {"list_agents"}

# 工具层往上指的两写法：相对导入超过一层（from ..），或者直接 import 包本身
UPWARD_IMPORTS = ("from ..", "import carrycode")


class ToolsLayeringTest(unittest.TestCase):
    def test_tools_never_import_upward(self):
        for path in sorted(TOOLS_DIR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for banned in UPWARD_IMPORTS:
                self.assertNotIn(banned, source, f"{path.name} 里出现了向上引用：{banned}")

    def test_base_imports_nothing_from_the_project(self):
        source = (TOOLS_DIR / "base.py").read_text(encoding="utf-8")
        self.assertNotIn("from .", source, "base.py 不该 import 同一层的模块")
        self.assertNotIn("import carrycode", source, "base.py 不该 import 项目里的东西")

    def test_tool_order_is_fixed(self):
        self.assertEqual([spec["name"] for spec in TOOL_SCHEMAS], EXPECTED_ORDER)

    def test_every_tool_has_description_and_schema(self):
        for spec in TOOL_SCHEMAS:
            name = spec["name"]
            self.assertTrue(spec.get("description"), f"{name} 缺 description")
            schema = spec.get("input_schema") or {}
            self.assertEqual(schema.get("type"), "object", f"{name} 的 input_schema 不是 object")
            if name in NO_ARG_TOOLS:
                self.assertEqual(
                    schema.get("properties"), {}, f"{name} 说好不要参数，就别偷偷加"
                )
            else:
                self.assertTrue(schema.get("properties"), f"{name} 没有定义任何参数")


class SecurityLedgerTest(unittest.TestCase):
    def test_ledger_matches_expectation(self):
        actual = {name: (spec["risk"], spec["paths"]) for name, spec in TOOLS.items()}
        self.assertEqual(actual, EXPECTED_LEDGER)

    def test_risk_levels_are_known(self):
        for name, spec in TOOLS.items():
            self.assertIn(spec["risk"], RISK_LEVELS, f"{name} 的 risk 不在允许值里")

    def test_every_tool_declares_risk_explicitly(self):
        """不许靠 register() 的缺省值过日子：缺省值是兜底，不是声明。"""
        for path in sorted(TOOLS_DIR.glob("*.py")):
            if path.name in NOT_TOOLS:
                continue
            source = path.read_text(encoding="utf-8")
            self.assertIn("RISK = ", source, f"{path.name} 没写 RISK")

    def test_preview_is_read_only(self):
        """审批块的内容由工具自己算，但它不许写文件：只查有没有写操作的调用。"""
        for name, spec in TOOLS.items():
            preview = spec["preview"]
            if preview is None:
                continue
            source = (TOOLS_DIR / f"{name}.py").read_text(encoding="utf-8")
            body = source.split("def preview", 1)[1]
            for banned in ("write_text(", ".write_bytes(", ".unlink(", ".mkdir(", "subprocess"):
                self.assertNotIn(banned, body, f"{name}.preview 里出现了写操作：{banned}")


if __name__ == "__main__":
    unittest.main()
