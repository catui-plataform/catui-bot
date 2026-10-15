import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from catui_bot.security import authorized, sign, verify, validate_plan, safe_text
from catui_bot.state import State
from catui_bot.service import Service
from catui_bot.http import RemoteError


def plan():
    return {"id": "a" * 24, "core": "pcsx2", "sha": "b" * 40, "version": "2.7.1",
            "store_head": "c" * 40, "owner": "123456789012345678", "expires": int(time.time()) + 3600}


class SecurityTests(unittest.TestCase):
    def test_signed_plan(self):
        value = plan()
        self.assertEqual(verify(value, sign(value, "test-key"), "test-key", value["owner"]), value)

    def test_tampering_and_wrong_owner(self):
        value = plan()
        signature = sign(value, "test-key")
        with self.assertRaises(ValueError):
            verify(value, signature, "test-key", "987654321098765432")
        value["sha"] = "d" * 40
        with self.assertRaises(ValueError):
            verify(value, signature, "test-key")

    def test_invalid_plans(self):
        for key, replacement in [("core", "../../evil"), ("sha", "main"), ("version", "1;curl evil"),
                                 ("expires", int(time.time()) - 1), ("id", "not-an-id")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                value = plan()
                value[key] = replacement
                validate_plan(value)
        with self.assertRaises(ValueError):
            validate_plan({**plan(), "command": "shell"})

    def test_channel_guild_owner_all_required(self):
        config = SimpleNamespace(owner_id=1, guild_id=2, channel_id=3)
        self.assertTrue(authorized(1, 2, 3, config))
        for values in [(4, 2, 3), (1, 4, 3), (1, 2, 4), (1, None, 3)]:
            self.assertFalse(authorized(*values, config))

    def test_untrusted_text_cannot_ping_or_break_cli(self):
        result = safe_text("@everyone ``` fake")
        self.assertNotIn("@", result)
        self.assertNotIn("```", result)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / "state.db")
        self.config = SimpleNamespace(owner_id=123456789012345678, signing_key="unit-test-key", llm_key="", updates_enabled=True)
        self.upstream = SimpleNamespace(store_head=AsyncMock(return_value="c" * 40), dispatch=AsyncMock(),
                                        latest=AsyncMock(return_value={"sha": "b" * 40, "version": "2.7.1"}),
                                        runs=AsyncMock(return_value=[]))
        self.service = Service(self.state, self.upstream, None, self.config)
        self.state.insert(plan(), "upstream changes")

    async def asyncTearDown(self):
        self.state.close()
        self.temp.cleanup()

    async def test_double_approval_dispatches_once(self):
        await asyncio.gather(*(self.service.approve("a" * 24) for _ in range(10)))
        self.assertEqual(self.upstream.dispatch.await_count, 1)
        self.assertEqual(self.state.get("a" * 24)["status"], "running")

    async def test_first_test_cannot_start_builds(self):
        self.config.updates_enabled = False
        await self.service.approve("a" * 24)
        self.upstream.dispatch.assert_not_awaited()

    async def test_parallel_cores_wait_for_updated_store(self):
        other = plan()
        other.update(id="e" * 24, core="rpcs3", sha="f" * 40)
        self.state.insert(other, "other core")
        await asyncio.gather(self.service.approve("a" * 24), self.service.approve("e" * 24))
        self.assertEqual(self.upstream.dispatch.await_count, 1)

    async def test_definitive_rejection_is_retryable(self):
        self.upstream.dispatch.side_effect = RemoteError("GitHub HTTP 403", 403)
        await self.service.approve("a" * 24)
        self.assertEqual(self.state.get("a" * 24)["status"], "failed")

    async def test_store_change_requires_new_approval(self):
        self.upstream.store_head.return_value = "d" * 40
        await self.service.approve("a" * 24)
        self.upstream.dispatch.assert_not_awaited()
        self.assertEqual(self.state.get("a" * 24)["status"], "stale")

    async def test_old_proposal_cannot_run_after_upstream_branch_moves(self):
        self.upstream.latest.return_value = {"sha": "d" * 40, "version": "2.8.2"}
        answer = await self.service.approve("a" * 24)
        self.assertIn("Upstream changed", answer)
        self.upstream.dispatch.assert_not_awaited()
        self.assertEqual(self.state.get("a" * 24)["status"], "stale")

    async def test_disabled_core_cannot_run(self):
        self.state.set_watch("pcsx2", False)
        await self.service.approve("a" * 24)
        self.upstream.dispatch.assert_not_awaited()

    async def test_uncertain_delivery_is_never_resent(self):
        self.upstream.dispatch.side_effect = TimeoutError()
        await self.service.approve("a" * 24)
        await self.service.approve("a" * 24)
        self.assertEqual(self.upstream.dispatch.await_count, 1)
        self.assertEqual(self.state.get("a" * 24)["status"], "uncertain")

    async def test_reconcile_only_exact_workflow(self):
        await self.service.approve("a" * 24)
        self.upstream.runs.return_value = [{"id": 1, "display_title": "unrelated", "html_url": "https://github.com/ignored", "status": "completed", "conclusion": "success"}]
        await self.service.reconcile()
        self.assertEqual(self.state.get("a" * 24)["status"], "running")
        self.upstream.runs.return_value[0]["display_title"] = "catui/pcsx2/" + "a" * 24
        await self.service.reconcile()
        self.assertEqual(self.state.get("a" * 24)["status"], "published")

    async def test_restart_preserves_watchlist_and_dispatch_uncertainty(self):
        self.state.set_watch("eden", False)
        self.state.transition("a" * 24, "pending", "dispatching")
        self.state.close()
        self.state = State(Path(self.temp.name) / "state.db")
        self.assertFalse(self.state.watches()["eden"])
        self.assertEqual(self.state.get("a" * 24)["status"], "uncertain")

    async def test_cancel_calls_github_and_waits_for_confirmation(self):
        await self.service.approve("a" * 24)
        self.state.update("a" * 24, run_id="123")
        self.upstream.cancel = AsyncMock()
        answer = await self.service.cancel("a" * 24)
        self.upstream.cancel.assert_awaited_once_with("123")
        self.assertIn("requested", answer)
        await self.service.cancel("a" * 24)
        self.upstream.cancel.assert_awaited_once()
        self.assertEqual(self.state.get("a" * 24)["status"], "running")
        self.upstream.runs.return_value = [{"id": 123, "display_title": "catui/pcsx2/" + "a" * 24,
            "html_url": "https://github.com/run/123", "status": "completed", "conclusion": "cancelled"}]
        await self.service.reconcile()
        self.assertEqual(self.state.get("a" * 24)["status"], "cancelled")
        self.assertEqual(len(self.state.unnotified()), 1)

    async def test_cancel_cannot_claim_success_when_run_is_not_registered(self):
        await self.service.approve("a" * 24)
        self.upstream.cancel = AsyncMock()
        answer = await self.service.cancel("a" * 24)
        self.assertIn("not registered", answer)
        self.upstream.cancel.assert_not_awaited()
        self.assertEqual(self.state.get("a" * 24)["status"], "running")

    async def test_rejected_cancellation_keeps_tracking_the_update(self):
        await self.service.approve("a" * 24)
        self.state.update("a" * 24, run_id="123")
        self.upstream.cancel = AsyncMock(side_effect=RemoteError("GitHub HTTP 403", 403))
        with self.assertRaises(RemoteError):
            await self.service.cancel("a" * 24)
        self.assertEqual(self.state.get("a" * 24)["status"], "running")

    async def test_corrected_runner_resumes_the_same_unexpired_approval_once(self):
        self.state.transition("a" * 24, "pending", "failed")
        old = {"id": 123, "display_title": "catui/pcsx2/" + "a" * 24,
               "status": "completed", "conclusion": "failure", "head_sha": "e" * 40,
               "html_url": "https://github.com/run/123"}
        self.upstream.runs.return_value = [old]
        self.upstream.bot_head = AsyncMock(return_value="f" * 40)
        await self.service.resume_after_update()
        self.upstream.dispatch.assert_awaited_once()
        payload, signature = self.upstream.dispatch.call_args.args
        self.assertEqual(payload, json.loads(self.state.get("a" * 24)["payload"]))
        self.assertEqual(signature, sign(payload, self.config.signing_key))
        await self.service.reconcile()
        await self.service.resume_after_update()
        self.assertEqual(self.state.get("a" * 24)["status"], "running")
        self.upstream.dispatch.assert_awaited_once()

    async def test_corrected_runner_cannot_resume_against_changed_store_or_same_code(self):
        self.state.transition("a" * 24, "pending", "failed")
        self.upstream.runs.return_value = [{"id": 123, "display_title": "catui/pcsx2/" + "a" * 24,
            "status": "completed", "conclusion": "failure", "head_sha": "e" * 40}]
        self.upstream.bot_head = AsyncMock(return_value="e" * 40)
        await self.service.resume_after_update()
        self.upstream.dispatch.assert_not_awaited()
        self.upstream.bot_head.return_value = "f" * 40
        self.upstream.store_head.return_value = "d" * 40
        await self.service.resume_after_update()
        self.upstream.dispatch.assert_not_awaited()



if __name__ == "__main__":
    unittest.main()
