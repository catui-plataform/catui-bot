from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from catui_bot.http import RemoteError
from catui_bot.service import Service
from catui_bot.state import State
from catui_bot.upstream import Upstream
from catui_bot.discord_app import add_release_field, card


def release():
    return {"tag_name": "v1.2.3", "name": "Hotfix", "body": "Fix crashes. Performance is unchanged.",
            "published_at": "2026-10-15T00:00:00Z", "prerelease": False}


class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_branch_target_is_independent_of_optional_release_metadata(self):
        http = SimpleNamespace(json=AsyncMock(side_effect=[
            {"sha": "b" * 40}, release()]))
        upstream = Upstream(http)
        target = await upstream.latest("pcsx2")
        self.assertEqual(target["sha"], "b" * 40)
        self.assertEqual(target["version"], "b" * 12)
        self.assertTrue(http.json.call_args_list[0].args[0].endswith("/commits/master"))
        self.assertEqual(http.json.await_count, 1)
        notes = await upstream.latest_release("pcsx2")
        self.assertEqual(notes["tag"], "v1.2.3")
        self.assertEqual(notes["url"], "https://github.com/PCSX2/pcsx2/releases/tag/v1.2.3")
        self.assertEqual(await upstream.latest_release("pcsx2"), notes)
        self.assertEqual(http.json.await_count, 2)

    async def test_eden_uses_canonical_releases_not_empty_mirror(self):
        preview = dict(release(), tag_name="v2.0", prerelease=True)
        http = SimpleNamespace(json=AsyncMock(return_value=[preview, release()]))
        notes = await Upstream(http).latest_release("eden")
        self.assertEqual(notes["tag"], "v1.2.3")
        self.assertIn("git.eden-emu.dev/api/", http.json.call_args.args[0])

    async def test_no_release_is_different_from_unavailable_api(self):
        http = SimpleNamespace(json=AsyncMock(side_effect=RemoteError("not found", 404)))
        self.assertIsNone(await Upstream(http).latest_release("pcsx2"))
        http.json.side_effect = RemoteError("unavailable", 403)
        with self.assertRaises(RemoteError):
            await Upstream(http).latest_release("pcsx2")

    async def test_summary_survives_restart_and_changes_only_with_release(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            try:
                upstream = SimpleNamespace(latest_release=AsyncMock(return_value=Upstream.release_metadata(release(),
                    {"repository": "https://github.com/PCSX2/pcsx2.git"})))
                assistant = SimpleNamespace(summarize_release=AsyncMock(return_value="Fixes crashes; performance is unchanged."))
                config = SimpleNamespace(llm_key="fixture-key", llm_model="deepseek-flash")
                service = Service(state, upstream, assistant, config)
                first = await service.release_context("pcsx2", {})
                state.close()
                state = State(Path(directory) / "state.db")
                service = Service(state, upstream, assistant, config)
                self.assertEqual(await service.release_context("pcsx2", {}), first)
                assistant.summarize_release.assert_awaited_once()
                upstream.latest_release.return_value["body"] += " Fix sound."
                await service.release_context("pcsx2", {})
                self.assertEqual(assistant.summarize_release.await_count, 2)
                embed = card("Core", "Status")
                add_release_field(embed, first)
                self.assertIn("performance is unchanged", embed.fields[-1].value)
                self.assertIn("v1.2.3", embed.fields[-1].name)
            finally:
                state.close()

    async def test_provider_outage_reports_summary_unavailable_without_read_link(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            try:
                data = Upstream.release_metadata(release(), {"repository": "https://github.com/PCSX2/pcsx2.git"})
                upstream = SimpleNamespace(latest_release=AsyncMock(return_value=data))
                assistant = SimpleNamespace(summarize_release=AsyncMock(side_effect=TimeoutError()))
                service = Service(state, upstream, assistant, SimpleNamespace(llm_key="fixture-key"))
                result = await service.release_context("pcsx2", {})
                self.assertEqual(result["status"], "available")
                self.assertEqual(result["summary"], "")
                embed = card("Core", "Status")
                add_release_field(embed, result)
                self.assertIn("DeepSeek could not complete", embed.fields[-1].value)
                self.assertNotIn(data["url"], embed.fields[-1].value)
            finally:
                state.close()

    async def test_all_watchers_resolve_branch_commits_without_requiring_release(self):
        from catui_bot.config import CORE_IDS
        for core in CORE_IDS:
            with self.subTest(core=core):
                http = SimpleNamespace(json=AsyncMock(return_value={"sha": "a" * 40}))
                upstream = Upstream(http)
                target = await upstream.latest(core)
                self.assertEqual(upstream.registry[core]["mode"], "commit")
                self.assertEqual(target["version"], "a" * 12)
                self.assertNotIn("release", target)
                self.assertTrue(http.json.call_args.args[0].endswith("/commits/" + upstream.registry[core]["branch"]))
                self.assertEqual(http.json.await_count, 1)

    async def test_draft_or_prerelease_are_not_shown_as_latest_release(self):
        for flag in ("draft", "prerelease"):
            http = SimpleNamespace(json=AsyncMock(return_value={**release(), flag: True}))
            self.assertIsNone(await Upstream(http).latest_release("pcsx2"))
            self.assertEqual(http.json.await_count, 1)
