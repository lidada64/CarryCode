"""配置优先级与脱敏的单元测试。

所有路径与 env 都显式注入，所以测试既不依赖你的家目录，也不依赖真实 .env。
"""

from __future__ import annotations

import json
import unittest

from carrycode.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    mask,
    resolve,
)

from .tmpdirs import cleanup, make_temp_dir


class ConfigResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        # 临时目录的建/删见 tests/tmpdirs.py：不用 mkdtemp，避免沙箱权限问题
        self.root = make_temp_dir("config")
        self.env_file = self.root / ".env"
        self.user_settings = self.root / "user.json"
        self.compat_settings = self.root / "compat.json"

    def tearDown(self) -> None:
        cleanup(self.root)

    def _write_env(self, text: str) -> None:
        self.env_file.write_text(text, encoding="utf-8")

    def _write_settings(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")

    def _resolve(self, overrides: dict | None = None, env: dict | None = None):
        return resolve(
            overrides=overrides or {},
            env=env or {},
            repo_root=self.root,
            env_paths=[self.env_file],
            settings_paths=[self.user_settings, self.compat_settings],
        )

    def test_defaults_when_nothing_configured(self) -> None:
        resolved = self._resolve()
        self.assertEqual(resolved.settings.model, DEFAULT_MODEL)
        self.assertEqual(resolved.settings.base_url, DEFAULT_BASE_URL)
        self.assertIsNone(resolved.settings.api_key)
        self.assertEqual(resolved.sources["model"], "默认值")

    def test_dotenv_beats_settings_files(self) -> None:
        self._write_env("ANTHROPIC_API_KEY=sk-from-dotenv\nANTHROPIC_MODEL=from-dotenv\n")
        self._write_settings(
            self.user_settings,
            {"model": "from-settings", "env": {"ANTHROPIC_API_KEY": "sk-from-settings"}},
        )
        resolved = self._resolve()
        self.assertEqual(resolved.settings.model, "from-dotenv")
        self.assertEqual(resolved.settings.api_key, "sk-from-dotenv")
        self.assertEqual(resolved.sources["api_key"], ".env:ANTHROPIC_API_KEY")

    def test_process_env_beats_dotenv(self) -> None:
        self._write_env("ANTHROPIC_API_KEY=sk-dotenv\n")
        resolved = self._resolve(env={"ANTHROPIC_API_KEY": "sk-env"})
        self.assertEqual(resolved.settings.api_key, "sk-env")
        self.assertEqual(resolved.sources["api_key"], "env:ANTHROPIC_API_KEY")

    def test_overrides_beat_everything(self) -> None:
        self._write_env("ANTHROPIC_MODEL=from-dotenv\n")
        resolved = self._resolve(
            overrides={"model": "from-cli"},
            env={"ANTHROPIC_MODEL": "from-env"},
        )
        self.assertEqual(resolved.settings.model, "from-cli")
        self.assertEqual(resolved.sources["model"], "命令行")

    def test_user_settings_win_over_compat(self) -> None:
        self._write_settings(self.user_settings, {"model": "user-model"})
        self._write_settings(self.compat_settings, {"model": "compat-model"})
        resolved = self._resolve()
        self.assertEqual(resolved.settings.model, "user-model")
        self.assertEqual(resolved.sources["model"], str(self.user_settings))

    def test_compat_settings_used_as_fallback(self) -> None:
        self._write_settings(
            self.compat_settings,
            {"model": "compat-model", "env": {"ANTHROPIC_API_KEY": "sk-compat"}},
        )
        resolved = self._resolve()
        self.assertEqual(resolved.settings.api_key, "sk-compat")
        self.assertEqual(resolved.sources["api_key"], str(self.compat_settings))

    def test_broken_settings_json_is_ignored(self) -> None:
        self.user_settings.write_text("{not json", encoding="utf-8")
        self.assertEqual(self._resolve().settings.model, DEFAULT_MODEL)

    def test_max_tokens_parsed_as_int(self) -> None:
        self._write_env("CARRYCODE_MAX_TOKENS=1234\n")
        self.assertEqual(self._resolve().settings.max_tokens, 1234)

    def test_invalid_max_tokens_falls_back(self) -> None:
        self._write_env("CARRYCODE_MAX_TOKENS=abc\n")
        self.assertEqual(self._resolve().settings.max_tokens, DEFAULT_MAX_TOKENS)

    def test_env_file_listing_is_recorded(self) -> None:
        self._write_env("A=1\n")
        self.assertEqual(self._resolve().env_files, [self.env_file])

    def test_mask_hides_secret(self) -> None:
        self.assertEqual(mask(None), "(未设置)")
        self.assertEqual(mask("short"), "*****")
        # 用明显的假 key，绝不把真实密钥写进测试（仓库是公开的）
        masked = mask("sk-test-0123456789abcdef")
        self.assertTrue(masked.startswith("sk-t"))
        self.assertTrue(masked.endswith("cdef"))
        self.assertNotIn("0123456789abcdef", masked)

    def test_missing_credentials_are_reported(self) -> None:
        problems = self._resolve().settings.problems()
        self.assertEqual(len(problems), 1)
        self.assertIn(".env", problems[0])

    def test_credentials_clear_problems(self) -> None:
        self._write_env("ANTHROPIC_API_KEY=sk-x\n")
        self.assertEqual(self._resolve().settings.problems(), [])

    def test_auth_token_also_counts_as_credentials(self) -> None:
        self._write_env("ANTHROPIC_AUTH_TOKEN=token-x\n")
        self.assertEqual(self._resolve().settings.problems(), [])


if __name__ == "__main__":
    unittest.main()
