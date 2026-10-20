import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

import discord

from catui_bot.config import Config, DEFAULT_ISSUES_CHANNEL_ID, DEFAULT_ISSUES_REPO
from catui_bot.llm import Assistant, strip_emojis, sanitize_labels
from catui_bot.upstream import Upstream
from catui_bot.service import Service
from catui_bot.state import State
from catui_bot.http import RemoteError
from catui_bot.discord_app import Bot, OwnerTree


class IssueAndReleaseLLMTests(unittest.TestCase):
    def test_strip_emojis(self):
        text = "🔥 Bug with audio! 🎧 Crashes on start 💥"
        cleaned = strip_emojis(text)
        self.assertEqual(cleaned, "Bug with audio!  Crashes on start")
        self.assertEqual(strip_emojis(""), "")
        self.assertEqual(strip_emojis(None), "")

    def test_sanitize_labels(self):
        labels = ["🐛 Bug", "✨ Enhancement", "UI/UX", "audio_settings", "invalid!@#label"]
        cleaned = sanitize_labels(labels)
        self.assertIn("bug", cleaned)
        self.assertIn("enhancement", cleaned)
        self.assertIn("uiux", cleaned)
        self.assertIn("audio_settings", cleaned)
        self.assertIn("invalidlabel", cleaned)
        self.assertNotIn("🐛 Bug", cleaned)

        # Empty fallback
        self.assertEqual(sanitize_labels([]), ["bug"])
        self.assertEqual(sanitize_labels(None), ["bug"])

    def test_fallback_issue(self):
        issue = Assistant.fallback_issue(
            thread_title="🎮 Controller rumble not stopping",
            starter_content="When playing PS2 games, rumble stays on forever. 🛑",
            reporter="PlayerOne",
            attachments=["https://example.com/log.txt"],
            description="Reproduced on DualSense via USB."
        )
        self.assertNotIn("🎮", issue["title"])
        self.assertNotIn("🛑", issue["body"])
        self.assertIn("PlayerOne", issue["body"])
        self.assertIn("Reproduced on DualSense via USB.", issue["body"])
        self.assertIn("https://example.com/log.txt", issue["body"])
        self.assertEqual(issue["labels"], ["bug"])

    def test_fallback_release(self):
        body = """A new update for CATui Build ZERO is here, bringing visual improvements.

### What's new
• **Focus Customization:** Change the focus highlight color across the interface.
• **SteamDB:** Added as a new source for CATui Artwork.

Build ZERO is currently exclusive to Windows for closed testing.
Expect bugs and unfinished features. Feedback is welcome!

Website: https://catui.xyz/"""
        formatted = Assistant.fallback_release("build-zero(0.1.1)", "build-zero (0.1.1)", body)
        self.assertIn("```yaml", formatted)
        self.assertIn("# CATUI | BUILD-ZERO (0.1.1)", formatted)
        self.assertIn("# WHAT'S NEW", formatted)
        self.assertIn("- Focus Customization: Change the focus highlight color across the interface;", formatted)
        self.assertIn("- SteamDB: Added as a new source for CATui Artwork;", formatted)
        self.assertIn("# RELEASE INFO", formatted)
        self.assertIn("Build ZERO is currently exclusive to Windows for closed testing.", formatted)
        self.assertIn("```", formatted)
        self.assertIn("**Download:** https://catui.xyz/download/", formatted)


class IssueAndReleaseAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / "test.db")
        self.config = Config(
            discord_token="fake-token",
            owner_id=123456789012345678,
            guild_id=234567890123456789,
            channel_id=345678901234567890,
            github_token="fake-gh",
            signing_key="unit-test-signing-key-minimum-32-chars",
            llm_key="fake-deepseek",
            llm_model="deepseek-flash",
            interval=3600,
            state_path=Path(self.temp.name) / "test.db",
            updates_enabled=True,
            issues_channel_id=DEFAULT_ISSUES_CHANNEL_ID,
            issues_repo=DEFAULT_ISSUES_REPO
        )
        self.http = MagicMock()
        self.assistant = Assistant(self.http, self.config)
        self.upstream = Upstream(self.http)
        self.service = Service(self.state, self.upstream, self.assistant, self.config)

    async def asyncTearDown(self):
        self.state.close()
        self.temp.cleanup()

    async def test_assistant_format_issue_with_llm(self):
        mock_llm_response = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "title": "Audio settings crash with bluetooth headset",
                        "body": "## Summary\nCrashes immediately upon opening audio settings.",
                        "labels": ["bug", "audio", "crash"]
                    })
                }
            }]
        }
        self.http.json = AsyncMock(return_value=mock_llm_response)
        result = await self.assistant.format_issue(
            thread_title="💥 Audio settings crash with bluetooth headset",
            starter_content="Crashes immediately on bluetooth",
            reporter="TestUser"
        )
        self.assertEqual(result["title"], "Audio settings crash with bluetooth headset")
        self.assertEqual(result["labels"], ["bug", "audio", "crash"])
        self.assertIn("## Summary", result["body"])

    async def test_assistant_format_release_with_llm(self):
        expected_announcement = """```yaml
# CATUI | BUILD ZERO (0.1.1)

A new update for CATui Build ZERO is here.

# WHAT'S NEW
- Focus: Improved;

# RELEASE INFO
Closed testing on Windows.
```
**Download:** https://catui.xyz/download/"""
        self.http.json = AsyncMock(return_value={
            "choices": [{"message": {"content": expected_announcement}}]
        })
        result = await self.assistant.format_release("0.1.1", "BUILD ZERO (0.1.1)", "raw body")
        self.assertEqual(result, expected_announcement)

    async def test_upstream_create_issue_and_retry_on_422(self):
        call_count = 0
        async def mock_json(url, method="GET", payload=None, headers=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RemoteError("Unprocessable Entity", status=422)
            return {"html_url": "https://github.com/arthiee4/catui-main/issues/42", "number": 42, "labels": []}

        self.http.json = AsyncMock(side_effect=mock_json)
        issue = await self.upstream.create_issue("arthiee4/catui-main", "Test Issue", "Body text", labels=["custom-label"])
        self.assertEqual(issue["number"], 42)
        self.assertEqual(issue["url"], "https://github.com/arthiee4/catui-main/issues/42")
        self.assertEqual(call_count, 2)

    async def test_upstream_catui_release(self):
        self.http.json = AsyncMock(return_value={
            "tag_name": "build-zero(0.1.1)",
            "name": "build-zero (0.1.1)",
            "body": "Changelog content",
            "html_url": "https://github.com/catui-plataform/CATui/releases/tag/build-zero(0.1.1)",
            "published_at": "2026-10-15T00:00:00Z"
        })
        release = await self.upstream.catui_release()
        self.assertEqual(release["tag"], "build-zero(0.1.1)")
        self.assertEqual(release["body"], "Changelog content")

    async def test_service_create_github_issue(self):
        self.assistant.format_issue = AsyncMock(return_value={
            "title": "Clean Issue Title",
            "body": "Formatted issue body",
            "labels": ["bug", "ui"]
        })
        self.upstream.create_issue = AsyncMock(return_value={
            "url": "https://github.com/arthiee4/catui-main/issues/99",
            "number": 99,
            "labels": ["bug", "ui"]
        })
        res = await self.service.create_github_issue(
            thread_title="Raw thread title",
            starter_content="Raw starter text",
            reporter="User123"
        )
        self.assertEqual(res["number"], 99)
        self.assertEqual(res["repo"], "arthiee4/catui-main")
        self.assertEqual(res["labels"], ["bug", "ui"])

    async def test_owner_tree_permissions(self):
        bot = Bot(self.config)
        bot.discord_io = MagicMock(ready=True, call=AsyncMock())
        bot.private_ready = True
        tree = bot.tree

        try:
            # 1. Non-owner gets denied on /issue
            interaction_non_owner = SimpleNamespace(
                user=SimpleNamespace(id=999999999999999999),
                guild_id=self.config.guild_id,
                channel_id=DEFAULT_ISSUES_CHANNEL_ID,
                channel=SimpleNamespace(id=DEFAULT_ISSUES_CHANNEL_ID, parent_id=None),
                command=SimpleNamespace(name="issue"),
                response=SimpleNamespace(send_message=AsyncMock())
            )
            allowed = await tree.interaction_check(interaction_non_owner)
            self.assertFalse(allowed)

            # 2. Owner allowed on /issue in gallery channel or thread
            thread_channel = SimpleNamespace(id=555555555555555555, parent_id=DEFAULT_ISSUES_CHANNEL_ID)
            interaction_owner = SimpleNamespace(
                user=SimpleNamespace(id=self.config.owner_id),
                guild_id=self.config.guild_id,
                channel_id=555555555555555555,
                channel=thread_channel,
                command=SimpleNamespace(name="issue"),
                response=SimpleNamespace(send_message=AsyncMock())
            )
            allowed = await tree.interaction_check(interaction_owner)
            self.assertTrue(allowed)

            # 3. Owner denied on /issue in random channel
            random_channel = SimpleNamespace(id=777777777777777777, parent_id=888888888888888888)
            interaction_wrong_channel = SimpleNamespace(
                user=SimpleNamespace(id=self.config.owner_id),
                guild_id=self.config.guild_id,
                channel_id=777777777777777777,
                channel=random_channel,
                command=SimpleNamespace(name="issue"),
                response=SimpleNamespace(send_message=AsyncMock())
            )
            allowed = await tree.interaction_check(interaction_wrong_channel)
            self.assertFalse(allowed)

            # 4. Owner allowed on /release anywhere in the guild
            interaction_release = SimpleNamespace(
                user=SimpleNamespace(id=self.config.owner_id),
                guild_id=self.config.guild_id,
                channel_id=777777777777777777,
                channel=random_channel,
                command=SimpleNamespace(name="release"),
                response=SimpleNamespace(send_message=AsyncMock())
            )
            allowed = await tree.interaction_check(interaction_release)
            self.assertTrue(allowed)

            # 5. Non-owner denied on /release
            interaction_release_non_owner = SimpleNamespace(
                user=SimpleNamespace(id=999999999999999999),
                guild_id=self.config.guild_id,
                channel_id=777777777777777777,
                channel=random_channel,
                command=SimpleNamespace(name="release"),
                response=SimpleNamespace(send_message=AsyncMock())
            )
            allowed = await tree.interaction_check(interaction_release_non_owner)
            self.assertFalse(allowed)
        finally:
            bot.state.close()


if __name__ == "__main__":
    unittest.main()
