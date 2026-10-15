import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from catui_bot.config import Config

ENVIRONMENT = {
    "DISCORD_TOKEN": "fixture-token",
    "DISCORD_OWNER_ID": "123456789012345678",
    "DISCORD_GUILD_ID": "234567890123456789",
    "DISCORD_CHANNEL_ID": "345678901234567890",
    "GITHUB_TOKEN": "fixture-github",
    "APPROVAL_SIGNING_KEY": "fixture" * 8,
}


class ConfigTests(unittest.TestCase):
    def test_host_environment_is_the_only_configuration_source(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENVIRONMENT, clear=True):
            root = Path(directory)
            (root / ".env").write_text("DISCORD_TOKEN=unused-file-token\n")
            with patch("catui_bot.config.ROOT", root):
                config = Config.load()
            self.assertEqual(config.discord_token, "fixture-token")
            self.assertTrue(config.updates_enabled)

    def test_dotenv_does_not_supply_missing_host_configuration(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / ".env").write_text("\n".join(f"{key}={value}" for key, value in ENVIRONMENT.items()))
            with patch("catui_bot.config.ROOT", root), self.assertRaisesRegex(ValueError, "APPROVAL_SIGNING_KEY"):
                Config.load()

    def test_assistant_uses_flash_and_ignores_obsolete_settings(self):
        with patch.dict(os.environ, {**ENVIRONMENT, "DEEPSEEK_MODEL": "obsolete-model",
                                    "LLM_DAILY_REQUEST_LIMIT": "obsolete-setting-is-ignored"}, clear=True):
            config = Config.load()
            self.assertEqual(config.llm_model, "deepseek-flash")
