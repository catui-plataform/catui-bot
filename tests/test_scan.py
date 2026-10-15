from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from catui_bot.service import Service
from catui_bot.state import State
from catui_bot.discord_app import core_card
from catui_bot.config import CORE_IDS


def source_base(commit):
    return {"commit": commit, "snapshot": {"archive": "source.tar.gz", "sha256": "f" * 64}}


class ScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / "db")
        for core in CORE_IDS:
            if core == "pcsx2":
                continue
            self.state.set_watch(core, False)
        self.upstream = SimpleNamespace(store_head=AsyncMock(return_value="a" * 40),
            latest=AsyncMock(return_value={"sha": "b" * 40, "version": "2.7", "notes": "notes"}),
            content=AsyncMock(side_effect=[{}, source_base("c" * 40)]),
            compare=AsyncMock(return_value={"status": "ahead", "commits": [{"subject": "Fix renderer"}]}),
            latest_release=AsyncMock(return_value=None),
            dispatch=AsyncMock())
        self.service = Service(self.state, self.upstream, None,
                               SimpleNamespace(owner_id=123456789012345678, llm_key=""))

    async def asyncTearDown(self):
        self.state.close()
        self.temp.cleanup()

    async def test_scan_only_proposes_never_dispatches(self):
        report = await self.service.scan()
        self.assertEqual(report.errors, [])
        self.assertEqual(len(self.state.jobs("pending")), 1)
        self.upstream.dispatch.assert_not_awaited()

    async def test_missing_source_disables_comparison_without_warning(self):
        self.upstream.content.side_effect = [{}, {"commit": "c" * 40}]
        report = await self.service.scan(manual=True)
        self.assertEqual(report.results[0]["status"], "source_missing")
        self.assertFalse(report.errors)
        self.assertFalse(self.state.jobs("pending"))
        self.upstream.latest.assert_not_awaited()
        self.upstream.compare.assert_not_awaited()

    async def test_saved_source_can_be_compared_without_old_build_receipt(self):
        source = source_base("b" * 40)
        source["packageBaselineVerified"] = False
        self.upstream.content.side_effect = [{}, source]
        report = await self.service.scan(manual=True)
        self.assertEqual(report.results[0]["status"], "up_to_date")
        self.assertIn("source base", core_card(report.results[0], self.state, self.service.config).description)
        self.assertFalse(report.errors)
        self.upstream.latest.assert_awaited_once()
        self.upstream.compare.assert_not_awaited()

    async def test_release_outage_does_not_block_commit_comparison(self):
        self.upstream.latest_release.side_effect = TimeoutError()
        report = await self.service.scan(manual=True)
        self.assertEqual(report.results[0]["status"], "pending")
        self.assertEqual(report.results[0]["release"]["status"], "unavailable")
        self.assertFalse(report.errors)
        self.upstream.dispatch.assert_not_awaited()

    async def test_monitor_only_core_never_creates_build_proposal(self):
        self.upstream.registry = {"pcsx2": {"repository": "https://example.test/core.git", "build_enabled": False}}
        source = source_base("c" * 40)
        source["repository"] = "https://example.test/core.git"
        self.upstream.content.side_effect = [{}, source]
        report = await self.service.scan(manual=True)
        self.assertEqual(report.results[0]["status"], "monitor_only")
        self.assertFalse(self.state.jobs("pending"))
        self.upstream.dispatch.assert_not_awaited()

    async def test_no_downgrade_or_divergent_publication(self):
        for status in ("behind", "identical", "diverged"):
            self.upstream.content.side_effect = [{}, source_base("c" * 40)]
            self.upstream.compare.return_value["status"] = status
            await self.service.scan()
            self.assertEqual(self.state.jobs("pending"), [])
        self.upstream.dispatch.assert_not_awaited()

    async def test_no_duplicate_proposal(self):
        await self.service.scan()
        self.upstream.content.side_effect = [{}, source_base("c" * 40)]
        report = await self.service.scan(manual=True)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.results[0]["status"], "pending")
        self.assertEqual(len(self.state.jobs("pending")), 1)

    async def test_one_failure_does_not_dispatch_or_enable_cores(self):
        self.upstream.latest.side_effect = TimeoutError()
        report = await self.service.scan()
        self.assertEqual(report.errors, ["pcsx2: TimeoutError"])
        self.assertEqual(self.state.jobs("pending"), [])
        self.upstream.dispatch.assert_not_awaited()

    async def test_legacy_notes_do_not_reset_an_uncertain_update(self):
        await self.service.scan()
        job = self.state.jobs("pending")[0]
        self.state.update(job["id"], notes="resumo (legacy text)")
        self.state.transition(job["id"], "pending", "uncertain")
        self.upstream.content.side_effect = [{}, source_base("c" * 40)]
        report = await self.service.scan(manual=True)
        self.assertEqual(report.results[0]["status"], "uncertain")
        self.assertFalse(self.state.jobs("pending"))
        self.upstream.dispatch.assert_not_awaited()

    async def test_current_core_still_has_release_notes(self):
        self.upstream.content.side_effect = [{"version": "2.7"}, source_base("b" * 40)]
        report = await self.service.scan(manual=True)
        self.assertEqual(report.results[0]["status"], "up_to_date")
        self.assertIn("release", report.results[0])
        self.upstream.latest_release.assert_awaited_once()

    async def test_skip_only_suppresses_automatic_notifications_until_manual_check(self):
        await self.service.scan()
        old = self.state.jobs("pending")[0]
        self.state.transition(old["id"], "pending", "rejected")
        self.upstream.content.side_effect = [{}, source_base("c" * 40)]
        automatic = await self.service.scan()
        self.assertEqual(automatic.results[0]["status"], "rejected")
        self.assertFalse(self.state.jobs("pending"))
        self.upstream.content.side_effect = [{}, source_base("c" * 40)]
        manual = await self.service.scan(manual=True)
        self.assertEqual(manual.results[0]["status"], "pending")
        self.assertNotEqual(manual.results[0]["job_id"], old["id"])
        self.assertEqual(self.state.get(old["id"])["status"], "stale")
        self.upstream.dispatch.assert_not_awaited()

    async def test_new_release_replaces_old_commit_proposal_without_dispatching(self):
        await self.service.scan()
        old = self.state.jobs("pending")[0]
        self.upstream.latest.return_value = {"sha": "d" * 40, "version": "2.8.2", "notes": "Release fixes",
            "release": {"tag": "v2.8.2", "title": "Release", "body": "Release fixes", "url": "https://github.com/PCSX2/pcsx2/releases/tag/v2.8.2", "prerelease": False}}
        self.upstream.content.side_effect = [{}, source_base("c" * 40)]
        report = await self.service.scan()
        self.assertEqual(report.results[0]["status"], "pending")
        self.assertEqual(self.state.get(old["id"])["status"], "stale")
        self.assertEqual(len(self.state.jobs("pending")), 1)
        self.assertNotEqual(report.results[0]["job_id"], old["id"])
        self.upstream.dispatch.assert_not_awaited()

    async def test_release_uses_one_cached_summary_without_a_second_model_call(self):
        self.service.config.llm_key = "fixture-key"
        self.service.assistant = SimpleNamespace(summarize_release=AsyncMock(return_value="This release fixes graphics issues."))
        self.upstream.latest.return_value["release"] = {"tag": "v2.7", "title": "Release", "body": "Graphics fixes", "prerelease": False}
        first = await self.service.scan()
        self.assertEqual(first.results[0]["release"]["summary"], "This release fixes graphics issues.")
        self.upstream.content.side_effect = [{}, source_base("c" * 40)]
        second = await self.service.scan(manual=True)
        self.assertEqual(second.results[0]["release"]["summary"], first.results[0]["release"]["summary"])
        self.service.assistant.summarize_release.assert_awaited_once()

    async def test_store_base_newer_than_release_is_not_reported_as_synced(self):
        self.upstream.compare.return_value = {"status": "behind", "behind_by": 2, "ahead_by": 0}
        report = await self.service.scan(manual=True)
        item = report.results[0]
        self.assertEqual(item["status"], "baseline_ahead")
        self.assertEqual(item["source_commits_ahead"], 2)
        self.assertFalse(report.errors)
        self.assertFalse(self.state.jobs("pending"))
        embed = core_card(item, self.state, self.service.config)
        text = str(embed.to_dict())
        self.assertIn("2 commits beyond upstream", text)
        self.assertNotIn("0 commits (synced)", text)
        self.upstream.dispatch.assert_not_awaited()

    async def test_diverged_source_is_a_review_result_not_a_check_error(self):
        self.upstream.compare.return_value = {"status": "diverged", "behind_by": 223, "ahead_by": 27}
        report = await self.service.scan(manual=True)
        self.assertEqual(report.results[0]["status"], "review")
        self.assertEqual(report.errors, [])
        self.assertFalse(self.state.jobs("pending"))
        embed = core_card(report.results[0], self.state, self.service.config)
        self.assertIn("[review needed]", embed.title)
        self.assertIn("different source histories", embed.description)
        self.assertFalse(any(field.name == "Attention" for field in embed.fields))
        self.upstream.dispatch.assert_not_awaited()

    async def test_scan_with_explicit_cores_and_empty_watchlist(self):
        for core in CORE_IDS:
            self.state.set_watch(core, False)
        self.upstream.content.side_effect = [{}, source_base("c" * 40)]
        report = await self.service.scan(cores=["pcsx2"], manual=True)
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0]["core"], "pcsx2")
        self.assertFalse(report.errors)
