from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import tasks

from catui_bot.discord_io import DiscordIO, DiscordRequestFailed, DiscordUnavailable
from catui_bot.discord_app import Bot, OwnerTree, UpdatingView
from catui_bot.service import ScanReport
from catui_bot.config import CORE_IDS


def limited(headers=None):
    response = SimpleNamespace(status=429, reason="Too Many Requests", headers=headers or {})
    return discord.HTTPException(response, "<html>Cloudflare 1015; untrusted response body</html>")


class DiscordIOTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_edit_is_retried_until_discord_accepts_it(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_path=Path(directory) / "state.db", guild_id=123)
            bot = Bot(config)
            bot.discord_io = DiscordIO(interval=0)
            try:
                plan = {"id": "a" * 24, "core": "eden", "sha": "b" * 40}
                bot.state.insert(plan, "Ready")
                bot.state.transition(plan["id"], "pending", "running")
                bot.state.update(plan["id"], message_id="123", stage="Compiling and validating the core")
                message = SimpleNamespace(edit=AsyncMock(side_effect=[RuntimeError("temporary"), None]))
                bot.control_channel = SimpleNamespace(get_partial_message=lambda _: message)
                with self.assertLogs("catui", level="WARNING"):
                    await bot._send_updates()
                self.assertEqual(bot.state.get(plan["id"])["reported_stage"], "")
                await bot._send_updates()
                self.assertEqual(message.edit.await_count, 2)
                self.assertEqual(bot.state.get(plan["id"])["reported_stage"], "Compiling and validating the core")
            finally:
                await bot.close()

    async def test_rate_limit_stops_all_requests_until_retry_after(self):
        now = [100.0]
        guard = DiscordIO(clock=lambda: now[0], interval=0)
        request = AsyncMock(side_effect=limited({"Retry-After": "600"}))
        with self.assertLogs("catui", level="WARNING") as logs:
            with self.assertRaises(DiscordUnavailable) as error:
                await guard.call(request)
        self.assertNotIn("<html>", str(error.exception))
        self.assertNotIn("untrusted response body", " ".join(logs.output))
        with self.assertRaises(DiscordUnavailable):
            await guard.call(request)
        self.assertEqual(request.await_count, 1)
        now[0] += 599
        self.assertFalse(guard.ready)
        now[0] += 1
        request.side_effect = None
        await guard.call(request)
        self.assertTrue(guard.ready)
        self.assertEqual(request.await_count, 2)

    async def test_html_rate_limit_without_headers_has_bounded_cooldown(self):
        now = [0.0]
        guard = DiscordIO(clock=lambda: now[0], interval=0)
        with self.assertLogs("catui", level="WARNING"):
            with self.assertRaises(DiscordUnavailable):
                await guard.call(AsyncMock(side_effect=limited()))
        self.assertEqual(guard.blocked_until, 300)

    async def test_command_error_handler_never_replies_to_transport_failure(self):
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        tree = SimpleNamespace(client=SimpleNamespace(discord_io=DiscordIO(interval=0)))
        for failure in (DiscordRequestFailed("safe"), limited()):
            with self.assertLogs("catui", level="WARNING"):
                await OwnerTree.on_error(tree, interaction, SimpleNamespace(original=failure))
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    async def test_error_reply_rate_limit_is_caught_without_a_second_reply(self):
        response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(side_effect=limited()))
        interaction = SimpleNamespace(response=response, followup=SimpleNamespace(send=AsyncMock()))
        tree = SimpleNamespace(client=SimpleNamespace(discord_io=DiscordIO(interval=0)))
        with self.assertLogs("catui", level="WARNING"):
            await OwnerTree.on_error(tree, interaction, ValueError("fixture"))
        response.send_message.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()

    async def test_manual_check_sends_only_core_cards_without_duplicate_announcements(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_path=Path(directory) / "state.db", guild_id=123456789012345678,
                                     github_token="fixture", owner_id=234567890123456789,
                                     channel_id=345678901234567890, updates_enabled=False)
            bot = Bot(config)
            bot.discord_io = DiscordIO(interval=0)
            try:
                with patch.object(bot.tree, "sync", new=AsyncMock()), patch.object(tasks.Loop, "start"):
                    await bot.setup_hook()
                bot.service.scan = AsyncMock(return_value=ScanReport(results=[
                    {"core": core, "status": "up_to_date", "version": "1.0", "store_version": "1.0"}
                    for core in ("pcsx2", "eden", "rpcs3", "shadps4")]))
                plan = {"id": "a" * 24, "core": "pcsx2", "sha": "b" * 40, "version": "1.0",
                        "store_head": "c" * 40, "owner": str(config.owner_id), "expires": int(time.time()) + 3600}
                bot.state.insert(plan, "Release changes")
                bot.service.scan.return_value.results[0].update(status="pending", job_id=plan["id"])
                bot.send_updates = AsyncMock()
                interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=56789))))
                commands = bot.tree.get_commands(guild=discord.Object(id=config.guild_id))
                self.assertEqual({c.name for c in commands}, {"cores-check", "cores-panel", "chat", "issue", "release", "admin-add", "admin-remove", "admins"})
                panel = next(c for c in commands if c.name == "cores-panel")
                panel_interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
                await panel.callback(panel_interaction)
                view = panel_interaction.response.send_message.call_args.kwargs["view"]
                selector = view.to_components()[0]["components"][0]
                self.assertEqual(selector["max_values"], len(CORE_IDS))
                self.assertEqual(len(selector["options"]), len(CORE_IDS))
                self.assertLessEqual(sum(option.get("default", False) for option in selector["options"]), selector["max_values"])
                command = next(c for c in commands if c.name == "cores-check")
                await command.callback(interaction)
                self.assertEqual(interaction.followup.send.await_count, 4)
                self.assertFalse(interaction.response.defer.call_args.kwargs["ephemeral"])
                self.assertTrue(all(call.kwargs["ephemeral"] is False for call in interaction.followup.send.call_args_list))
                bot.send_updates.assert_not_awaited()
                bot.service.scan.assert_awaited_once_with(cores=list(CORE_IDS), manual=True)
                self.assertEqual(bot.state.get(plan["id"])["message_id"], "56789")
                self.assertTrue(interaction.followup.send.call_args_list[0].kwargs["view"].is_persistent())
                bot.control_channel = SimpleNamespace(send=AsyncMock())
                await bot._send_updates()
                bot.control_channel.send.assert_not_awaited()
                progress_message = SimpleNamespace(edit=AsyncMock())
                bot.control_channel.get_partial_message = lambda message_id: progress_message
                bot.state.transition(plan["id"], "pending", "running")
                bot.state.update(plan["id"], stage="Compiling and checking the core")
                await bot._send_updates()
                await bot._send_updates()
                progress_message.edit.assert_awaited_once()
                self.assertEqual(bot.state.get(plan["id"])["reported_stage"], "Compiling and checking the core")
                # Transition to published and verify in-place edit instead of new message
                bot.state.transition(plan["id"], "running", "published")
                bot.state.update(plan["id"], result="SHA-256: dummy", run_url="https://github.com/run/123")
                await bot._send_updates()
                self.assertEqual(progress_message.edit.await_count, 2)
                bot.control_channel.send.assert_not_awaited()
                last_embed = progress_message.edit.call_args_list[-1].kwargs["embed"]
                self.assertIn("published", last_embed.title)
                self.assertIn("`[x]` Approval & signature", last_embed.description)
                self.assertIn("`[x]` Store publication", last_embed.description)
                self.assertIn("[================] 100%", last_embed.description)
            finally:
                await bot.close()

    async def test_proposal_approval_provides_instant_card_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_path=Path(directory) / "state.db", guild_id=123,
                                     github_token="fixture", owner_id=456,
                                     channel_id=789, updates_enabled=True, signing_key="test-key")
            bot = Bot(config)
            bot.discord_io = DiscordIO(interval=0)
            try:
                with patch.object(bot.tree, "sync", new=AsyncMock()), patch.object(tasks.Loop, "start"):
                    await bot.setup_hook()
                from catui_bot.discord_app import Proposal
                plan = {"id": "p" * 24, "core": "pcsx2", "sha": "c" * 40, "version": "2.8.1",
                        "store_head": "s" * 40, "owner": str(config.owner_id), "expires": int(time.time()) + 3600}
                bot.state.insert(plan, "Ready")
                proposal_view = Proposal(bot, plan["id"])
                approve_btn = next(item for item in proposal_view.children if getattr(item, "label", None) == "Approve update")
                msg = SimpleNamespace(id=99999, edit=AsyncMock())
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=config.owner_id),
                    guild_id=config.guild_id,
                    channel_id=config.channel_id,
                    message=msg,
                    response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock())
                )
                bot.private_ready = True
                bot.service.approve = AsyncMock(return_value="Approved: pcsx2 -> 2.8.1. Build dispatched; tracking progress on this card.")
                bot.state.transition(plan["id"], "pending", "running")
                await approve_btn.callback(interaction)
                self.assertEqual(bot.state.get(plan["id"])["message_id"], "99999")
                self.assertTrue(msg.edit.await_count >= 1)
                last_call_embed = msg.edit.call_args_list[-1].kwargs["embed"]
                self.assertIn("[updating]", last_call_embed.title)
                self.assertIn("Progress:", last_call_embed.description)
            finally:
                await bot.close()

    async def test_monotonic_pipeline_clamps_and_persists_stage_pct(self):
        from catui_bot.discord_app import ascii_pipeline, stage_percentage
        stages = [
            "Dispatching workflow to runner",
            "Waiting for runner to start preflight",
            "Verifying approval & signature",
            "Approval verified; checking store access",
            "Checking store publishing access",
            "Access verified; reviewing upstream code",
            "Reviewing upstream code changes",
            "Review complete; starting Windows build runner",
            "Preparing static dependencies",
            "Preparing CATui compatibility tools",
            "Running Windows build worker",
            "Compiling and validating the core",
            "Adapting integration with AI repair",
            "Retrying build with repair proposal",
            "Build validated; preparing store publication",
            "Publishing verified package to store",
            "Store publication complete"
        ]
        prev = 0
        for s in stages:
            pct = stage_percentage(s)
            self.assertGreaterEqual(pct, prev, f"Stage {s} ({pct}%) regressed from {prev}%")
            prev = pct

        bar, pipe = ascii_pipeline("Verifying approval & signature", min_pct=70)
        self.assertIn("70%", bar)
        self.assertIn("`[>]` Compile & validate", pipe)

        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_path=Path(directory) / "state.db", guild_id=123,
                                     github_token="fixture", owner_id=456,
                                     channel_id=789, updates_enabled=True)
            bot = Bot(config)
            bot.discord_io = DiscordIO(interval=0)
            try:
                plan = {"id": "p" * 24, "core": "pcsx2", "sha": "c" * 40, "version": "2.8.1",
                        "store_head": "s" * 40, "owner": str(config.owner_id), "expires": int(time.time()) + 3600}
                bot.state.insert(plan, "Ready")
                bot.state.transition(plan["id"], "pending", "running")
                bot.state.update(plan["id"], message_id="111", stage="Compiling and validating the core")
                progress_message = SimpleNamespace(edit=AsyncMock())
                bot.control_channel = SimpleNamespace(get_partial_message=lambda mid: progress_message)
                await bot._send_updates()
                job = bot.state.get(plan["id"])
                self.assertEqual(job["stage_pct"], 70)

                bot.state.update(plan["id"], stage="Waiting for runner")
                await bot._send_updates()
                job = bot.state.get(plan["id"])
                self.assertEqual(job["stage_pct"], 70)
                last_embed = progress_message.edit.call_args_list[-1].kwargs["embed"]
                self.assertIn("70%", last_embed.description)
            finally:
                await bot.close()

    async def test_cores_check_independent_from_monitoring_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_path=Path(directory) / "state.db", guild_id=123,
                                     github_token="fixture", owner_id=123456789012345678,
                                     channel_id=789, updates_enabled=True, signing_key="test-key-of-at-least-32-characters!!")
            bot = Bot(config)
            bot.discord_io = DiscordIO(interval=0)
            try:
                with patch.object(bot.tree, "sync", new=AsyncMock()), patch.object(tasks.Loop, "start"):
                    await bot.setup_hook()
                for core in CORE_IDS:
                    bot.state.set_watch(core, False)
                self.assertFalse(any(bot.state.watches().values()))

                commands = bot.tree.get_commands(guild=discord.Object(id=config.guild_id))
                check_cmd = next(c for c in commands if c.name == "cores-check")

                bg_report = await bot.service.scan(manual=False)
                self.assertEqual(bg_report.results, [])

                bot.service.scan = AsyncMock(return_value=ScanReport(results=[
                    {"core": core, "status": "up_to_date", "version": "1.0", "store_version": "1.0"}
                    for core in CORE_IDS
                ]))
                interaction = SimpleNamespace(
                    response=SimpleNamespace(defer=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
                )
                await check_cmd.callback(interaction)
                bot.service.scan.assert_awaited_once_with(cores=list(CORE_IDS), manual=True)
                self.assertEqual(interaction.followup.send.await_count, len(CORE_IDS))

                bot.service.scan.reset_mock()
                single_choice = SimpleNamespace(value="pcsx2")
                interaction2 = SimpleNamespace(
                    response=SimpleNamespace(defer=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=124)))
                )
                await check_cmd.callback(interaction2, core=single_choice)
                bot.service.scan.assert_awaited_once_with(cores=["pcsx2"], manual=True)

                plan = {"id": "f" * 24, "core": "pcsx2", "sha": "c" * 40, "version": "2.8.1",
                        "store_head": "d" * 40, "owner": str(config.owner_id), "expires": int(time.time()) + 3600}
                bot.state.insert(plan, "Ready")
                bot.service.upstream.latest = AsyncMock(return_value={"sha": plan["sha"], "version": plan["version"]})
                bot.service.upstream.store_head = AsyncMock(return_value=plan["store_head"])
                bot.service.upstream.dispatch = AsyncMock()
                answer = await bot.service.approve(plan["id"])
                self.assertIn("Approved:", answer)
                bot.service.upstream.dispatch.assert_awaited_once()
            finally:
                await bot.close()

    async def test_recovery_is_owned_by_actions_without_blind_redispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                state_path=Path(directory) / "state.db",
                guild_id=123,
                github_token="fixture",
                owner_id=123456789012345678,
                channel_id=789,
                updates_enabled=True,
                signing_key="test-key-of-at-least-32-characters!!",
                llm_key="",
            )
            bot = Bot(config)
            bot.discord_io = DiscordIO(interval=0)
            bot.private_ready = True
            try:
                with patch.object(bot.tree, "sync", new=AsyncMock()), patch.object(tasks.Loop, "start"):
                    await bot.setup_hook()
                plan = {
                    "id": "r" * 24,
                    "core": "pcsx2",
                    "sha": "d" * 40,
                    "version": "2.8.2",
                    "store_head": "e" * 40,
                    "owner": str(config.owner_id),
                    "expires": int(time.time()) + 3600,
                }
                bot.state.insert(plan, "Ready")
                bot.state.transition(plan["id"], "pending", "running")
                bot.state.update(plan["id"], message_id="555", stage="Compiling and validating the core")

                failed_run = {
                    "id": 99901,
                    "display_title": f"catui/pcsx2/{plan['id']}",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/test/runs/99901",
                }
                bot.service.upstream.runs = AsyncMock(return_value=[failed_run])
                bot.service.upstream.dispatch = AsyncMock()

                bot.service.upstream.diagnostics = AsyncMock(return_value=[{
                    "job": "build / worker", "result": "failure",
                    "failed_steps": ["Build and validate approved source"],
                }])
                await bot.service.reconcile()
                job = bot.state.get(plan["id"])
                self.assertEqual(job["status"], "failed")
                self.assertIn("build / worker", job["result"])
                bot.service.upstream.dispatch.assert_not_called()
                await bot.service.reconcile()
                bot.service.upstream.dispatch.assert_not_called()
            finally:
                await bot.close()

    async def test_updating_view_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                state_path=Path(directory) / "state.db",
                guild_id=123,
                github_token="fixture",
                owner_id=123456789012345678,
                channel_id=789,
                updates_enabled=True,
                signing_key="test-key-of-at-least-32-characters!!",
            )
            bot = Bot(config)
            bot.discord_io = DiscordIO(interval=0)
            bot.private_ready = True
            try:
                with patch.object(bot.tree, "sync", new=AsyncMock()), patch.object(tasks.Loop, "start"):
                    await bot.setup_hook()
                plan = {
                    "id": "c" * 24,
                    "core": "pcsx2",
                    "sha": "a" * 40,
                    "version": "2.8.0",
                    "store_head": "b" * 40,
                    "owner": str(config.owner_id),
                    "expires": int(time.time()) + 3600,
                }
                bot.state.insert(plan, "Ready")
                bot.state.transition(plan["id"], "pending", "running")

                view = UpdatingView(run_url="https://github.com/test/run", bot=bot, job_id=plan["id"])
                cancel_btns = [c for c in view.children if getattr(c, "label", None) == "Cancel update"]
                self.assertEqual(len(cancel_btns), 1)
                self.assertEqual(cancel_btns[0].style, discord.ButtonStyle.danger)

                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=config.owner_id),
                    guild_id=config.guild_id,
                    channel_id=config.channel_id,
                    response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()),
                    message=SimpleNamespace(edit=AsyncMock()),
                )
                bot.state.update(plan["id"], run_id="123")
                bot.service.upstream.cancel = AsyncMock()
                await view.on_cancel(interaction)
                job = bot.state.get(plan["id"])
                self.assertEqual(job["status"], "running")
                self.assertIn("Cancellation requested", job["result"])
                bot.service.upstream.cancel.assert_awaited_once_with("123")
                interaction.message.edit.assert_awaited_once()
                updated_view = interaction.message.edit.call_args.kwargs["view"]
                self.assertFalse(any(getattr(button, "label", "") == "Cancel update" for button in updated_view.children))
            finally:
                await bot.close()
