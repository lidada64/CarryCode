"""dotenv 解析的单元测试。用标准库 unittest，不引入 pytest。"""

from __future__ import annotations

import unittest

from carrycode.dotenv import load_dotenv, parse_dotenv

from .tmpdirs import cleanup, make_temp_dir


class _TempDirTestCase(unittest.TestCase):
    """给用例一个可写的临时目录（原因见 tests/tmpdirs.py）。"""

    def setUp(self) -> None:
        self.tmp = make_temp_dir("dotenv")

    def tearDown(self) -> None:
        cleanup(self.tmp)


class ParseDotenvTests(unittest.TestCase):
    def test_basic_pairs(self) -> None:
        self.assertEqual(parse_dotenv("A=1\nB=two\n"), {"A": "1", "B": "two"})

    def test_comments_and_blank_lines(self) -> None:
        text = "# 顶部注释\n\nA=1\n   # 缩进注释\nB=2\n"
        self.assertEqual(parse_dotenv(text), {"A": "1", "B": "2"})

    def test_quotes_are_stripped(self) -> None:
        text = 'A="quoted value"\nB=\'single\'\n'
        self.assertEqual(parse_dotenv(text), {"A": "quoted value", "B": "single"})

    def test_export_prefix(self) -> None:
        self.assertEqual(parse_dotenv("export A=1\n"), {"A": "1"})

    def test_hash_inside_value_is_kept(self) -> None:
        # 只有整行才是注释，值里的 # 不能被截断
        self.assertEqual(parse_dotenv("URL=https://x/#frag\n"), {"URL": "https://x/#frag"})

    def test_invalid_lines_are_skipped(self) -> None:
        text = "no equals here\n1BAD=x\n=empty\n密钥=中文键\nA=1\n"
        self.assertEqual(parse_dotenv(text), {"A": "1"})

    def test_empty_value_is_allowed(self) -> None:
        self.assertEqual(parse_dotenv("A=\n"), {"A": ""})

    def test_duplicate_key_last_wins(self) -> None:
        self.assertEqual(parse_dotenv("A=1\nA=2\n"), {"A": "2"})

    def test_crlf_text(self) -> None:
        self.assertEqual(parse_dotenv("A=1\r\nB=2\r\n"), {"A": "1", "B": "2"})


class LoadDotenvTests(_TempDirTestCase):
    def test_missing_file_returns_empty_dict(self) -> None:
        self.assertEqual(load_dotenv(self.tmp / "nope.env"), {})

    def test_reads_utf8_content(self) -> None:
        path = self.tmp / ".env"
        path.write_text("KEY=值\n", encoding="utf-8")
        self.assertEqual(load_dotenv(path), {"KEY": "值"})


if __name__ == "__main__":
    unittest.main()
