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
from catui_bot.discord_app import Bot, OwnerTree, extract_message_ids


class IssueAndReleaseLLMTests(unittest.TestCase):
    def test_extract_message_ids(self):
        raw = "1460650067139756165, 1460650067139756166; https://discord.com/channels/123456789012345678/1460650067139756165/1460650067139756167"
        ids = extract_message_ids(raw)
        self.assertEqual(ids, [1460650067139756165, 1460650067139756166, 1460650067139756167])
        self.assertEqual(extract_message_ids(""), [])
        self.assertEqual(extract_message_ids(None), [])

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

    def test_fallback_issue_with_multi_messages_and_roles(self):
        starter = {
            "author": "PlayerOne",
            "is_admin": False,
            "content": "When playing PS2 games, rumble stays on forever. 🛑",
            "images": [{"filename": "crash.png", "url": "https://cdn.discord.com/crash.png"}],
            "files": [{"filename": "log.txt", "url": "https://cdn.discord.com/log.txt"}]
        }
        extra_messages = [
            {
                "id": 111,
                "author": "GamerTwo",
                "is_admin": False,
                "content": "I also reproduced this on Windows 11 with DualSense!",
                "images": [{"filename": "controller.jpg", "url": "https://cdn.discord.com/controller.jpg"}],
                "files": []
            },
            {
                "id": 222,
                "author": "AdminDev",
                "is_admin": True,
                "content": "Confirmed in pcsx2 input handler, mutex is not unlocked.",
                "images": [],
                "files": []
            }
        ]
        issue = Assistant.fallback_issue(
            thread_title="🎮 Controller rumble not stopping",
            starter=starter,
            extra_messages=extra_messages,
            description="High priority fix needed."
        )
        self.assertNotIn("🎮", issue["title"])
        self.assertNotIn("🛑", issue["body"])
        # Check user report section
        self.assertIn("## User Reports & Comments", issue["body"])
        self.assertIn("@GamerTwo", issue["body"])
        self.assertIn("I also reproduced this", issue["body"])
        # Check maintainer notes section
        self.assertIn("## Maintainer Notes", issue["body"])
        self.assertIn("@AdminDev", issue["body"])
        self.assertIn("Confirmed in pcsx2 input handler", issue["body"])
        self.assertIn("High priority fix needed.", issue["body"])
        # Check image embeds
        self.assertIn("![crash.png](https://cdn.discord.com/crash.png)", issue["body"])
        self.assertIn("![controller.jpg](https://cdn.discord.com/controller.jpg)", issue["body"])
        self.assertIn("[log.txt](https://cdn.discord.com/log.txt)", issue["body"])
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

    async def test_state_admin_management(self):
        # Owner is always an admin
        self.assertTrue(self.state.is_admin(self.config.owner_id, self.config.owner_id))
        self.assertFalse(self.state.is_admin(999888777666555444, self.config.owner_id))

        # Add admin
        self.state.add_admin(999888777666555444)
        self.assertIn(999888777666555444, self.state.admins())
        self.assertTrue(self.state.is_admin(999888777666555444, self.config.owner_id))

        # Remove admin
        self.state.remove_admin(999888777666555444)
        self.assertNotIn(999888777666555444, self.state.admins())
        self.assertFalse(self.state.is_admin(999888777666555444, self.config.owner_id))

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
            starter="Crashes immediately on bluetooth",
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
            starter={"author": "User123", "content": "Raw starter text", "is_admin": False, "images": [], "files": []}
        )
        self.assertEqual(res["number"], 99)
        self.assertEqual(res["repo"], "arthiee4/catui-main")
        self.assertEqual(res["labels"], ["bug", "ui"])

    async def test_service_sync_github_issue_updates_existing_and_force_new(self):
        self.assistant.format_issue = AsyncMock(return_value={
            "title": "Initial Title",
            "body": "Initial Body",
            "labels": ["bug"]
        })
        self.upstream.create_issue = AsyncMock(return_value={
            "url": "https://github.com/arthiee4/catui-main/issues/101",
            "number": 101,
            "labels": ["bug"]
        })
        self.upstream.update_issue = AsyncMock(return_value={
            "url": "https://github.com/arthiee4/catui-main/issues/101",
            "number": 101,
            "labels": ["bug", "ui"]
        })
        self.upstream.add_issue_comment = AsyncMock(return_value={"id": 1, "url": ""})

        # 1. First run creates issue
        created = await self.service.create_github_issue(
            thread_title="Bug Report",
            starter={"author": "User1", "content": "Initial text", "is_admin": False, "images": [], "files": []},
            thread_id=555666777
        )
        self.assertEqual(created["action"], "created")
        self.assertEqual(created["number"], 101)
        self.upstream.create_issue.assert_awaited_once()

        # 2. Second run in the same thread UPDATES the existing issue
        self.assistant.format_issue.return_value = {
            "title": "Updated Title",
            "body": "Updated Body with Extra Notes",
            "labels": ["bug", "ui"]
        }
        updated = await self.service.create_github_issue(
            thread_title="Bug Report",
            starter={"author": "User1", "content": "Initial text", "is_admin": False, "images": [], "files": []},
            description="Additional maintainer info",
            thread_id=555666777
        )
        self.assertEqual(updated["action"], "updated")
        self.assertEqual(updated["number"], 101)
        self.upstream.update_issue.assert_awaited_once()
        self.upstream.add_issue_comment.assert_awaited_once()

        # 3. Running with force_new creates a brand new issue
        self.upstream.create_issue.reset_mock()
        self.upstream.create_issue.return_value = {
            "url": "https://github.com/arthiee4/catui-main/issues/102",
            "number": 102,
            "labels": ["bug"]
        }
        forced = await self.service.create_github_issue(
            thread_title="Bug Report",
            starter={"author": "User1", "content": "Initial text", "is_admin": False, "images": [], "files": []},
            thread_id=555666777,
            force_new=True
        )
        self.assertEqual(forced["action"], "created")
        self.assertEqual(forced["number"], 102)
        self.upstream.create_issue.assert_awaited_once()

    async def test_owner_tree_permissions_and_admin_delegation(self):
        bot = Bot(self.config)
        bot.discord_io = MagicMock(ready=True, call=AsyncMock())
        bot.private_ready = True
        tree = bot.tree

        try:
            # Add a secondary admin
            secondary_admin_id = 444555666777888999
            bot.state.add_admin(secondary_admin_id)

            # 1. Non-admin gets denied on /issue
            interaction_non_admin = SimpleNamespace(
                user=SimpleNamespace(id=999999999999999999),
                guild_id=self.config.guild_id,
                channel_id=DEFAULT_ISSUES_CHANNEL_ID,
                channel=SimpleNamespace(id=DEFAULT_ISSUES_CHANNEL_ID, parent_id=None),
                command=SimpleNamespace(name="issue"),
                response=SimpleNamespace(send_message=AsyncMock())
            )
            self.assertFalse(await tree.interaction_check(interaction_non_admin))

            # 2. Secondary admin is allowed on /issue in gallery channel
            thread_channel = SimpleNamespace(id=555555555555555555, parent_id=DEFAULT_ISSUES_CHANNEL_ID)
            interaction_sec_admin = SimpleNamespace(
                user=SimpleNamespace(id=secondary_admin_id),
                guild_id=self.config.guild_id,
                channel_id=555555555555555555,
                channel=thread_channel,
                command=SimpleNamespace(name="issue"),
                response=SimpleNamespace(send_message=AsyncMock())
            )
            self.assertTrue(await tree.interaction_check(interaction_sec_admin))

            # 3. Secondary admin is allowed on /release
            interaction_sec_release = SimpleNamespace(
                user=SimpleNamespace(id=secondary_admin_id),
                guild_id=self.config.guild_id,
                channel_id=777777777777777777,
                channel=SimpleNamespace(id=777777777777777777, parent_id=888888888888888888),
                command=SimpleNamespace(name="release"),
                response=SimpleNamespace(send_message=AsyncMock())
            )
            self.assertTrue(await tree.interaction_check(interaction_sec_release))

            # 4. Secondary admin CANNOT add/remove other admins (Owner only)
            interaction_sec_admin_add = SimpleNamespace(
                user=SimpleNamespace(id=secondary_admin_id),
                guild_id=self.config.guild_id,
                channel_id=self.config.channel_id,
                command=SimpleNamespace(name="admin-add"),
                response=SimpleNamespace(send_message=AsyncMock())
            )
            self.assertFalse(await tree.interaction_check(interaction_sec_admin_add))

            # 5. Owner CAN add admins
            interaction_owner_admin_add = SimpleNamespace(
                user=SimpleNamespace(id=self.config.owner_id),
                guild_id=self.config.guild_id,
                channel_id=self.config.channel_id,
                command=SimpleNamespace(name="admin-add"),
                response=SimpleNamespace(send_message=AsyncMock())
            )
            self.assertTrue(await tree.interaction_check(interaction_owner_admin_add))

        finally:
            bot.state.close()


if __name__ == "__main__":
    unittest.main()
