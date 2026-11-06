"""markdown 渲染：有 rich 就排版，没 rich 走纯文本，两条路都不能崩、不能出乱码。"""

from __future__ import annotations

import io
import unittest

from carrycode.render import render_markdown, render_plain, rich_available


class RichAvailabilityTest(unittest.TestCase):
    def test_reports_a_boolean(self):
        self.assertIsInstance(rich_available(), bool)


class PlainRenderTest(unittest.TestCase):
    def test_heading_gets_an_ascii_underline(self):
        buffer = io.StringIO()
        render_plain("# Report\n\n## Usage", buffer)

        self.assertIn("Report\n======", buffer.getvalue())
        self.assertIn("Usage\n-----", buffer.getvalue())

    def test_inline_markers_are_stripped(self):
        buffer = io.StringIO()
        render_plain("这是 **加粗** 和 `code`。", buffer)

        self.assertEqual(buffer.getvalue().strip(), "这是 加粗 和 code。")

    def test_code_fence_is_indented_and_loose_fences_removed(self):
        buffer = io.StringIO()
        render_plain("```python\nprint(1)\n```", buffer)

        self.assertIn("    print(1)", buffer.getvalue())
        self.assertNotIn("```", buffer.getvalue())

    def test_output_has_no_ansi_escapes(self):
        buffer = io.StringIO()
        render_plain("# 标题\n**粗**\n`码`", buffer)

        self.assertNotIn("\x1b", buffer.getvalue())

    def test_empty_text_prints_nothing(self):
        buffer = io.StringIO()
        render_markdown("", buffer)

        self.assertEqual(buffer.getvalue(), "")


class RenderMarkdownTest(unittest.TestCase):
    def test_renders_into_a_given_stream(self):
        buffer = io.StringIO()
        render_markdown("# 标题\n\n正文", buffer)

        value = buffer.getvalue()
        self.assertIn("标题", value)
        self.assertIn("正文", value)


if __name__ == "__main__":
    unittest.main()
