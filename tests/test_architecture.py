"""架构约束：工具层只能往下 import；清单顺序固定；每个工具三件套齐全。

跑法（在 CarryCode 仓库根）：

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import unittest
from pathlib import Path

from carrycode.tools import TOOL_SCHEMAS

TOOLS_DIR = Path(__file__).resolve().parent.parent / "carrycode" / "tools"

# 顺序 = 发给模型的顺序。改这里等于改请求开头，会让服务端记住的前缀失效，
# 所以顺序变化必须是一次明确的、有人盯着的变更。
EXPECTED_ORDER = ["bash", "read_file", "write_file", "edit_file", "glob", "grep"]

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
            self.assertTrue(schema.get("properties"), f"{name} 没有定义任何参数")


if __name__ == "__main__":
    unittest.main()
