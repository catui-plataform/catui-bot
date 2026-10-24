import asyncio
import json
import logging
import re
import time
import sys
from typing import Optional
import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from .config import Config, CORE_IDS, CORE_NAMES
from .http import HTTP, RemoteError
from .llm import Assistant, AI_ERRORS
from .security import authorized, safe_text
from .service import Service
from .state import State
from .upstream import Upstream
from .discord_io import DiscordIO, DiscordRequestFailed

log = logging.getLogger("catui")


STATUS_METADATA = {
    "source_missing": {
        "title": "Source Required", "tag": "[SOURCE REQUIRED]", "color": 0x95A5A6,
        "summary": "Commit comparison is disabled until this core has a source snapshot and an exact upstream base in the store."
    },
    "monitor_only": {
        "title": "Update Available", "tag": "[UPDATE AVAILABLE]", "color": 0xFEE75C,
        "summary": "New upstream commits are available. Automatic builds are not configured for this core yet."
    },
    "up_to_date": {
        "title": "Up to Date",
        "tag": "[UP TO DATE]",
        "color": 0x57F287,
        "summary": "The store's recorded source base matches the latest upstream commit."
    },
    "baseline_ahead": {
        "title": "Up to Date",
        "tag": "[UP TO DATE]",
        "color": 0x57F287,
        "summary": "The store's recorded source base already includes the latest upstream commit."
    },
    "pending": {
        "title": "Update Available",
        "tag": "[UPDATE READY]",
        "color": 0xFEE75C,
        "summary": "New upstream commits are available. Review the changes before building."
    },
    "running": {
        "title": "Building Update",
        "tag": "[BUILDING]",
        "color": 0x5865F2,
        "summary": "GitHub Actions is compiling and validating this core."
    },
    "dispatching": {
        "title": "Starting Build",
        "tag": "[DISPATCHING]",
        "color": 0x5865F2,
        "summary": "Dispatching build workflow to GitHub Actions runner."
    },
    "uncertain": {
        "title": "Checking Status",
        "tag": "[CHECKING]",
        "color": 0xE8A05B,
        "summary": "Confirming workflow status with GitHub."
    },
    "published": {
        "title": "Published",
        "tag": "[PUBLISHED]",
        "color": 0x57F287,
        "summary": "Built, validated, and published to the store."
    },
    "failed": {
        "title": "Build Failed",
        "tag": "[FAILED]",
        "color": 0xED4245,
        "summary": "Build or test validation did not pass."
    },
    "rejected": {
        "title": "Skipped",
        "tag": "[SKIPPED]",
        "color": 0x95A5A6,
        "summary": "Update was skipped / ignored."
    },
    "cancelled": {
        "title": "Cancelled", "tag": "[CANCELLED]", "color": 0x95A5A6,
        "summary": "GitHub confirmed workflow cancellation."
    },
    "review": {
        "title": "Review Required",
        "tag": "[REVIEW NEEDED]",
        "color": 0x9B59B6,
        "summary": "Upstream and the store integration use different source histories."
    },
    "error": {
        "title": "Check Error",
        "tag": "[ERROR]",
        "color": 0xED4245,
        "summary": "Could not inspect upstream or store."
    }
}


def card(title, body, color=0x5865F2):
    embed = discord.Embed(title=title, description=safe_text(body, 3800), color=color)
    return embed


def split_release_announcement(content, limit=1900):
    if len(content) <= limit:
        return [content]
    opening = content.find("```yaml\n")
    closing = content.find("\n```", opening + 7) if opening >= 0 else -1
    if opening < 0 or closing < 0:
        return [content[i:i + limit] for i in range(0, len(content), limit)]

    yaml_body = content[opening + len("```yaml\n"):closing]
    footer = content[closing + 4:].strip()
    chunks = []
    current = []
    for line in yaml_body.splitlines():
        candidate = "\n".join(current + [line])
        if current and len("```yaml\n" + candidate + "\n```") > limit:
            chunks.append("```yaml\n" + "\n".join(current) + "\n```")
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("```yaml\n" + "\n".join(current) + "\n```")

    if footer:
        footer_chunks = []
        current = []
        for line in footer.splitlines():
            candidate = "\n".join(current + [line])
            if current and len(candidate) > limit:
                footer_chunks.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            footer_chunks.append("\n".join(current))
        chunks.extend(footer_chunks)
    return chunks


async def send_release_announcement(channel, discord_io, content):
    if len(content) <= 1950:
        await discord_io.call(channel.send, content)
        return
    if len(content) <= 4096:
        embed = discord.Embed(description=content, color=0x5865F2)
        await discord_io.call(channel.send, embed=embed)
        return

    embed_parts = split_release_announcement(content, limit=3800)
    if len(embed_parts) <= 10 and sum(map(len, embed_parts)) <= 6000:
        embeds = [discord.Embed(description=part, color=0x5865F2) for part in embed_parts]
        await discord_io.call(channel.send, embeds=embeds)
        return

    # Discord caps ordinary message content at 2,000 characters and all embeds
    # on one message at 6,000 characters, so only exceptionally long notes need
    # multiple messages to preserve every detail.
    for part in split_release_announcement(content):
        await discord_io.call(channel.send, part)


def stage_percentage(stage_text):
    text = (stage_text or "").lower()
    if not text:
        return 10
    if "store publication complete" in text or "publication complete" in text or text == "complete" or "published" in text:
        return 98
    if ("store publication" in text or "publish" in text) and "access" not in text:
        return 92
    if "build validated" in text or "preparing store" in text:
        return 85
    if "retry" in text:
        return 80
    if "repair" in text or "adapt" in text:
        return 75
    if any(w in text for w in ("compil", "validat", "building")):
        return 70
    if "build worker" in text:
        return 60
    if any(w in text for w in ("static", "compatibility", "frontend")):
        return 55
    if "build runner" in text:
        return 50
    if "review" in text:
        return 45
    if "access" in text:
        return 30
    if "approval" in text or "signature" in text or ("preflight" in text and "waiting" not in text):
        return 20
    if any(w in text for w in ("dispatch", "starting", "queued", "waiting")):
        return 10
    return 15


def ascii_pipeline(stage_text, is_done=False, is_failed=False, min_pct=0):
    if is_done:
        active = 5
        pct = 100
    else:
        raw_pct = stage_percentage(stage_text)
        pct = max(raw_pct, int(min_pct or 0))
        if pct >= 90:
            active = 4
        elif pct >= 50:
            active = 3
        elif pct >= 25:
            active = 2
        else:
            active = 1

    steps = [
        ("Approval & signature", 1),
        ("Upstream code review", 2),
        ("Compile & validate (Ninja x64)", 3),
        ("Store publication (CATui main)", 4),
    ]
    lines = []
    for name, step_num in steps:
        if is_done:
            mark = "[x]"
        elif is_failed and step_num == active:
            mark = "[!]"
        elif step_num < active:
            mark = "[x]"
        elif step_num == active:
            mark = "[>]"
        else:
            mark = "[ ]"
        lines.append(f"`{mark}` {name}")

    filled = int(round((pct / 100.0) * 16))
    bar = f"[{'=' * filled}{'.' * (16 - filled)}] {pct}%"
    return bar, "\n".join(lines)


def updating_card(core_id, stage, sha, run_url=None, min_pct=0):
    core_name = CORE_NAMES.get(core_id, core_id.upper())
    bar, pipeline = ascii_pipeline(stage, min_pct=min_pct)
    body = (
        f"**Stage:** {stage}\n"
        f"**Progress:** `{bar}`\n\n"
        f"**Pipeline:**\n{pipeline}\n\n"
        f"**Commit:** `{sha[:12] if sha else '--'}`"
    )
    embed = card(f"{core_name} [updating]", body, color=0x5865F2)
    if run_url:
        embed.add_field(name="Build Report", value=f"[GitHub Actions]({run_url})", inline=False)
    return embed


def finished_card(job, ok):
    core_name = CORE_NAMES.get(job["core"], job["core"])
    color = 0x57F287 if ok else 0xED4245
    title = f"{core_name} [published]" if ok else f"{core_name} [failed]"
    cancelled = job.get("status") == "cancelled"
    if cancelled:
        title = f"{core_name} [cancelled]"
    stage_text = job.get("stage") or ("Complete" if ok else "Compile & validate")
    min_pct = 100 if ok else (job.get("stage_pct") or 0)
    bar, pipeline = ascii_pipeline(stage_text, is_done=ok, is_failed=not ok, min_pct=min_pct)
    status_summary = (
        "Successfully published to CATui store. Build, native checks and CATui frontend validation passed."
        if ok else
        "Update was not published. Workflow build or validation failed."
    )
    if cancelled:
        status_summary = "GitHub confirmed workflow cancellation."
    parts = [
        f"**Status:** {status_summary}",
        f"**Progress:** `{bar}`\n",
        f"**Pipeline:**\n{pipeline}\n",
        f"**Commit:** `{job['sha'][:12] if job.get('sha') else '--'}`"
    ]
    if job.get("notes"):
        parts.append(f"\n{job['notes']}")
    body = "\n".join(parts)
    embed = card(title, body, color=color)
    if job.get("run_url"):
        embed.add_field(name="GitHub Actions", value=f"[View Build Report]({job['run_url']})", inline=False)
    if job.get("result"):
        field_name = "Publication Details" if ok else "Diagnostics"
        embed.add_field(name=field_name, value=safe_text(job["result"], 1000), inline=False)
    return embed


def format_notes(notes, max_len=1000):
    if not notes:
        return ""
    text = re.sub(r"^\s*\+\d+\s+commits\s+ahead\s*", "", notes, flags=re.IGNORECASE).strip()
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    formatted = []
    for line in lines:
        cleaned = re.sub(r"^[\s•\-\*>]+", "", line).strip()
        if any(bad in cleaned.lower() for bad in [
            "resumo (", "não há evidência", "para ação", "somente mudanças", "mudanças upstream",
            "catui):", "nada aqui foi testado"
        ]):
            continue
        if cleaned:
            formatted.append(f"> {cleaned}")
    if not formatted:
        return ""
    result = "\n".join(formatted)
    if len(result) > max_len:
        cut = result[:max_len]
        last_nl = cut.rfind("\n")
        if last_nl > 0:
            result = cut[:last_nl]
        else:
            result = cut
    return safe_text(result, max_len)


def add_release_field(embed, release):
    if not release:
        return
    status = release.get("status")
    if status == "missing":
        return
    if status != "available":
        text = "Release notes could not be loaded." if status == "unavailable" else "No published release was found."
        embed.add_field(name="Latest Release", value=text, inline=False)
        return
    tag = safe_text(release["tag"], 100)
    text = release.get("summary")
    if not text:
        if not release.get("has_description"):
            text = "The project did not provide release notes for this version."
        else:
            text = AI_ERRORS.get(release.get("summary_status"), AI_ERRORS["provider_error"])
    embed.add_field(name=f"Release Notes · {tag}", value=safe_text(text, 1000), inline=False)


def core_card(item, state, config):
    core_id = item["core"]
    core_name = CORE_NAMES.get(core_id, core_id.upper())
    job = state.get(item["job_id"]) if item.get("job_id") else None
    status = job["status"] if job else item.get("status", "error")
    meta = STATUS_METADATA.get(status, STATUS_METADATA["error"])

    embed = discord.Embed(
        title=f"{core_name} {meta['tag'].lower()}",
        description=safe_text(item.get("notes") or meta['summary'], 900) if status in {"review", "baseline_ahead"} else meta['summary'],
        color=meta["color"]
    )
    if status == "source_missing":
        return embed

    store_ver = safe_text(item.get("store_version") or "unknown", 60)
    upstream_ver = safe_text(item.get("version") or "unknown", 60)

    commits_ahead = item.get("commits_ahead")
    if status == "baseline_ahead":
        count = item.get("source_commits_ahead")
        delta_str = f"`{count} commits beyond upstream`" if count else "`Upstream included in source base`"
    elif commits_ahead is not None:
        if commits_ahead == 0:
            delta_str = "`0 commits (synced)`"
        else:
            delta_str = f"`+{commits_ahead} commits ahead`"
    elif status in ("up_to_date", "published"):
        delta_str = "`0 commits (synced)`"
    elif status in ("running", "dispatching", "uncertain"):
        delta_str = "`Build in progress`"
    else:
        delta_str = "`--`"

    embed.add_field(name="Store Source Base", value=f"`{store_ver}`", inline=True)
    embed.add_field(name="Latest Commit", value=f"`{upstream_ver}`", inline=True)
    embed.add_field(name="Revision Delta", value=delta_str, inline=True)

    if status == "pending":
        embed.add_field(name="Action Required", value="Choose **Approve** to build these changes, or **Skip** to dismiss them.", inline=False)
    elif status == "failed":
        embed.add_field(name="Diagnostic", value="Previous build or validation failed. You can retry or skip below.", inline=False)

    notes = job["notes"] if job and job.get("notes") else item.get("notes")
    if notes and status in ("pending", "failed"):
        formatted = format_notes(notes, 1000)
        if formatted:
            embed.add_field(name="Changes / Summary", value=formatted, inline=False)

    add_release_field(embed, item.get("release"))

    return embed


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
DISCORD_MSG_LINK_RE = re.compile(r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")


def parse_message_inputs(raw):
    """
    Parses a string containing any combination of:
    - Text notes (e.g. 'another test', 'o usuario relatou outro problema')
    - Discord message URLs (https://discord.com/channels/<guild>/<channel>/<id>)
    - Discord message snowflake IDs (123456789012345678)
    - Direct image URLs (https://.../photo.png, discord CDN links)
    Returns:
    - msg_refs: list of dicts with {"guild_id": Optional[int], "channel_id": Optional[int], "message_id": int}
    - direct_images: list of dicts with {"filename": str, "url": str}
    - text_notes: str (the remaining text after stripping out IDs and links)
    """
    if not raw:
        return [], [], ""
    msg_refs = []
    direct_images = []
    text_tokens = []
    tokens = re.split(r"[\s,;]+", str(raw).strip())
    seen_ids = set()
    seen_urls = set()

    for t in tokens:
        t = t.strip()
        if not t:
            continue
        lower = t.lower()
        if any(lower.endswith(ext) for ext in IMAGE_EXTS) or ("cdn.discordapp.com/attachments/" in lower) or ("media.discordapp.net/attachments/" in lower):
            if t not in seen_urls:
                seen_urls.add(t)
                fn = t.split("/")[-1].split("?")[0] or "Screenshot"
                direct_images.append({"filename": fn, "url": t})
            continue

        match = DISCORD_MSG_LINK_RE.search(t)
        if match:
            mid = int(match.group(3))
            if mid not in seen_ids:
                seen_ids.add(mid)
                msg_refs.append({
                    "guild_id": int(match.group(1)),
                    "channel_id": int(match.group(2)),
                    "message_id": mid
                })
            continue

        if "/" in t:
            last = t.rstrip("/").split("/")[-1]
            if re.fullmatch(r"\d{15,22}", last):
                mid = int(last)
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    msg_refs.append({"guild_id": None, "channel_id": None, "message_id": mid})
                continue

        if re.fullmatch(r"\d{15,22}", t):
            mid = int(t)
            if mid not in seen_ids:
                seen_ids.add(mid)
                msg_refs.append({"guild_id": None, "channel_id": None, "message_id": mid})
            continue

        text_tokens.append(t)

    text_notes = " ".join(text_tokens).strip()
    return msg_refs, direct_images, text_notes


def extract_message_ids(raw):
    refs, _, _ = parse_message_inputs(raw)
    return [r["message_id"] for r in refs]


async def find_existing_issue_in_thread(target, target_repo=None):
    if not target or not hasattr(target, "history"):
        return None
    try:
        async for msg in target.history(limit=50):
            for emb in getattr(msg, "embeds", []):
                if emb.title:
                    m = re.search(r"GitHub Issue #(\d+)", emb.title, re.IGNORECASE)
                    if m:
                        num = int(m.group(1))
                        url = emb.url or (f"https://github.com/{target_repo}/issues/{num}" if target_repo else "")
                        return {"repo": target_repo or "", "issue_number": num, "issue_url": url}
                if emb.url:
                    m = re.search(r"github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)", emb.url)
                    if m:
                        r = m.group(1)
                        if not target_repo or r.lower() == target_repo.lower():
                            return {"repo": r, "issue_number": int(m.group(2)), "issue_url": emb.url}
                if emb.description:
                    m = re.search(r"github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)", emb.description)
                    if m:
                        r = m.group(1)
                        if not target_repo or r.lower() == target_repo.lower():
                            url = f"https://github.com/{r}/issues/{m.group(2)}"
                            return {"repo": r, "issue_number": int(m.group(2)), "issue_url": url}

            for comp in getattr(msg, "components", []):
                for child in getattr(comp, "children", []):
                    url = getattr(child, "url", None)
                    if url:
                        m = re.search(r"github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)", url)
                        if m:
                            r = m.group(1)
                            if not target_repo or r.lower() == target_repo.lower():
                                return {"repo": r, "issue_number": int(m.group(2)), "issue_url": url}

            if msg.content:
                m = re.search(r"github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)", msg.content)
                if m:
                    r = m.group(1)
                    if not target_repo or r.lower() == target_repo.lower():
                        url = f"https://github.com/{r}/issues/{m.group(2)}"
                        return {"repo": r, "issue_number": int(m.group(2)), "issue_url": url}
    except Exception as exc:
        log.warning("Scanning thread history for existing issue failed: %s", exc)
    return None


class OwnerTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        if not self.client.discord_io.ready:
            return False

        cmd_name = getattr(getattr(interaction, "command", None), "name", "")

        # Owner-exclusive admin management
        if cmd_name in {"admin-add", "admin-remove"}:
            if interaction.user.id != self.client.config.owner_id:
                await self.client.discord_io.call(
                    interaction.response.send_message,
                    "Only the bot owner can manage administrators.",
                    ephemeral=True
                )
                return False
            return True

        # All other commands require being an admin (owner or added admin)
        if not self.client.state.is_admin(interaction.user.id, self.client.config.owner_id):
            await self.client.discord_io.call(interaction.response.send_message, "Access denied.", ephemeral=True)
            return False
        if interaction.guild_id != self.client.config.guild_id:
            await self.client.discord_io.call(interaction.response.send_message, "Access denied.", ephemeral=True)
            return False

        if cmd_name == "admins":
            return True

        if cmd_name == "issue":
            chan = interaction.channel
            parent_id = getattr(chan, "parent_id", None)
            is_issue_channel = (
                interaction.channel_id in (self.client.config.issues_channel_id, self.client.config.channel_id)
                or parent_id == self.client.config.issues_channel_id
            )
            if not is_issue_channel:
                await self.client.discord_io.call(
                    interaction.response.send_message,
                    f"The /issue command is only allowed in the issues gallery channel (<#{self.client.config.issues_channel_id}>) or its posts.",
                    ephemeral=True
                )
                return False
            return True

        if cmd_name == "release":
            return True

        if self.client.allowed(interaction.user.id, interaction.guild_id, interaction.channel_id):
            return True
        await self.client.discord_io.call(interaction.response.send_message, "Access denied.", ephemeral=True)
        return False

    async def on_error(self, interaction, error):
        cause = getattr(error, "original", error)
        cmd_name = getattr(getattr(interaction, "command", None), "name", "unknown")
        log.warning("Command '%s' failed: %s: %s", cmd_name, type(cause).__name__, cause)
        if isinstance(cause, (DiscordRequestFailed, discord.HTTPException)):
            return  # Never report a failed Discord request through the same failed endpoint.
        cause_type = type(cause).__name__
        if "TransformerError" in cause_type:
            detail = "Invalid parameter option. Please select a valid core from the list."
        elif isinstance(cause, RemoteError):
            detail = f"Could not contact external service ({f'HTTP {cause.status}' if cause.status else 'connection error'}). Check GITHUB_TOKEN or permissions."
        elif isinstance(cause, TimeoutError):
            detail = "The request timed out. Please try again in a few moments."
        else:
            detail = f"Command failed ({cause_type}). Check bot logs for details."
        message = f"**Operation Failed**\n{detail}"
        try:
            if interaction.response.is_done():
                await self.client.discord_io.call(interaction.followup.send, message, ephemeral=True)
            else:
                await self.client.discord_io.call(interaction.response.send_message, message, ephemeral=True)
        except (DiscordRequestFailed, discord.HTTPException):
            pass


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        log.warning("Control failed: %s", type(error).__name__)


async def cancel_update(bot, job_id, interaction):
    if not bot or not job_id:
        return
    if not bot.allowed(interaction.user.id, interaction.guild_id, interaction.channel_id):
        await bot.discord_io.call(interaction.response.send_message, "Access denied.", ephemeral=True)
        return
    await bot.discord_io.call(interaction.response.defer, ephemeral=True)
    answer = await bot.service.cancel(job_id)
    job = bot.state.get(job_id)
    if job and job.get("stage") == "Cancellation requested on GitHub":
        view = UpdatingView(run_url=job.get("run_url"))
        embed = updating_card(job["core"], job["stage"], job["sha"], job.get("run_url"), job.get("stage_pct", 0))
        await bot.discord_io.call(interaction.message.edit, embed=embed, view=view)
    await bot.discord_io.call(interaction.followup.send, answer, ephemeral=True)


class UpdatingView(SafeView):
    def __init__(self, run_url=None, bot=None, job_id=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.job_id = job_id
        self.add_item(discord.ui.Button(label="Updating...", style=discord.ButtonStyle.secondary, disabled=True))
        job = bot.state.get(job_id) if bot and job_id else None
        if bot and job_id and (not job or job.get("stage") != "Cancellation requested on GitHub"):
            cancel_btn = discord.ui.Button(
                label="Cancel update",
                style=discord.ButtonStyle.danger,
                custom_id=f"catui:cancel:{job_id}"
            )
            cancel_btn.callback = self.on_cancel
            self.add_item(cancel_btn)
        if run_url:
            self.add_item(discord.ui.Button(label="Build Report", url=run_url))

    async def on_cancel(self, interaction: discord.Interaction):
        await cancel_update(self.bot, self.job_id, interaction)


class FinishedView(SafeView):
    def __init__(self, bot, job, ok):
        super().__init__(timeout=None)
        self.bot = bot
        self.job_id = job["id"]
        self.core = job["core"]
        self.core_name = CORE_NAMES.get(self.core, self.core.upper())
        label = "Published" if ok else "Cancelled" if job.get("status") == "cancelled" else "Failed"
        style = discord.ButtonStyle.success if ok else discord.ButtonStyle.danger
        self.add_item(discord.ui.Button(label=f"{self.core_name}: {label}", style=style, disabled=True))
        if job.get("run_url"):
            self.add_item(discord.ui.Button(label="Build Report", url=job["run_url"]))



async def approve_update(bot, job_id, interaction):
    await bot.discord_io.call(interaction.response.defer, ephemeral=True)
    if job_id:
        bot.state.update(job_id, message_id=str(interaction.message.id), stage_pct=10)
    job = bot.state.get(job_id) if job_id else None
    sha = job["sha"] if job else ""
    initial_embed = updating_card(job["core"] if job else "pcsx2", "Dispatching workflow to Windows runner", sha, min_pct=10)
    initial_view = UpdatingView(run_url=None, bot=bot, job_id=job_id)
    try:
        await bot.discord_io.call(interaction.message.edit, embed=initial_embed, view=initial_view)
    except Exception:
        pass

    res = await bot.service.approve(job_id)
    job = bot.state.get(job_id) if job_id else None
    if job and job["status"] in ("running", "dispatching", "uncertain"):
        current_stage = job.get("stage") or "Dispatching workflow to Windows runner"
        min_pct = max(10, job.get("stage_pct") or 0, stage_percentage(current_stage))
        if job_id:
            bot.state.update(job_id, stage_pct=min_pct)
        embed = updating_card(job["core"] if job else "pcsx2", current_stage, job["sha"], job.get("run_url"), min_pct=min_pct)
        view = UpdatingView(run_url=job.get("run_url"), bot=bot, job_id=job_id)
        try:
            await bot.discord_io.call(interaction.message.edit, embed=embed, view=view)
        except Exception:
            pass
    elif job and job["status"] == "failed":
        embed = finished_card(job, ok=False)
        view = FinishedView(bot, job, ok=False)
        try:
            await bot.discord_io.call(interaction.message.edit, embed=embed, view=view)
        except Exception:
            pass
    else:
        await bot.discord_io.call(interaction.message.edit, embed=card("Update not started", res), view=None)
    await bot.discord_io.call(interaction.followup.send, res, ephemeral=True)



class Proposal(SafeView):
    def __init__(self, bot, job_id):
        super().__init__(timeout=None)
        self.bot, self.job_id = bot, job_id
        for action, label, style in [
            ("approve", "Approve update", discord.ButtonStyle.success),
            ("skip", "Skip", discord.ButtonStyle.secondary)
        ]:
            button = discord.ui.Button(label=label, style=style, custom_id=f"catui:{action}:{job_id}")

            def bind(act):
                async def callback(interaction: discord.Interaction):
                    if not self.bot.allowed(interaction.user.id, interaction.guild_id, interaction.channel_id):
                        await self.bot.discord_io.call(interaction.response.send_message, "Access denied.", ephemeral=True)
                        return
                    if act == "approve":
                        await approve_update(self.bot, self.job_id, interaction)
                        return
                    else:
                        await self.bot.discord_io.call(interaction.response.defer, ephemeral=True)
                        changed = self.bot.state.transition(self.job_id, "pending", "rejected")
                        answer = "Update skipped." if changed else "This proposal has already been handled."
                        for child in self.children:
                            child.disabled = True
                        try:
                            await self.bot.discord_io.call(interaction.message.edit, view=self)
                        except Exception:
                            pass
                    await self.bot.discord_io.call(interaction.followup.send, answer, ephemeral=True)
                return callback

            button.callback = bind(action)
            self.add_item(button)

    async def interaction_check(self, interaction):
        allowed = self.bot.allowed(interaction.user.id, interaction.guild_id, interaction.channel_id)
        if not allowed:
            await self.bot.discord_io.call(interaction.response.send_message, "Access denied.", ephemeral=True)
        return allowed


def core_controls(bot, item):
    job = bot.state.get(item["job_id"]) if item.get("job_id") else None
    status = job["status"] if job else item.get("status", "error")
    if job and status == "pending":
        return Proposal(bot, job["id"])
    if job and status in {"running", "dispatching", "uncertain"}:
        return UpdatingView(run_url=job.get("run_url"), bot=bot, job_id=job["id"])
    if job and status in {"published", "failed", "cancelled"}:
        return FinishedView(bot, job, status == "published")
    view = SafeView(timeout=300)
    name = CORE_NAMES.get(item["core"], item["core"])
    label = STATUS_METADATA.get(status, STATUS_METADATA["error"])["title"]
    view.add_item(discord.ui.Button(label=f"{name}: {label}", style=discord.ButtonStyle.secondary, disabled=True))
    return view


class WatchSelect(discord.ui.Select):
    def __init__(self, bot):
        self.bot = bot
        watches = bot.state.watches()
        super().__init__(placeholder="Select cores for background monitoring", min_values=0, max_values=len(CORE_IDS),
                         options=[discord.SelectOption(label=CORE_NAMES[x], value=x, default=watches[x]) for x in CORE_IDS])

    async def callback(self, interaction):
        if not self.bot.allowed(interaction.user.id, interaction.guild_id, interaction.channel_id):
            await self.bot.discord_io.call(interaction.response.send_message, "Access denied.", ephemeral=True)
            return
        for core in CORE_IDS:
            self.bot.state.set_watch(core, core in self.values)
        await self.bot.discord_io.call(interaction.response.edit_message, content="Background monitoring saved: " + (", ".join(CORE_NAMES[x] for x in self.values) or "none"), embed=None, view=None)


class Bot(discord.Client):
    def __init__(self, config):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(), max_messages=50)
        self.config = config
        self.tree = OwnerTree(self)
        self.state = State(config.state_path)
        self.private_ready = False
        self.last_recovery_check = 0
        self.chat_lock = asyncio.Lock()
        self.notification_lock = asyncio.Lock()
        self.discord_io = DiscordIO()
        self.last_chat = 0.0
        self.last_scan = float("-inf")
        self.last_release_scan = float("-inf")
        self.release_channel = None

    def allowed(self, user, guild, channel):
        return self.private_ready and authorized(user, guild, channel, self.config, self.state)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        http = HTTP(self.session, self.config.github_token)
        self.assistant = Assistant(http, self.config)
        self.service = Service(self.state, Upstream(http), self.assistant, self.config)
        guild = discord.Object(id=self.config.guild_id)

        @self.tree.command(name="cores-panel", description="Choose which emulator cores to monitor in the background", guild=guild)
        async def panel(interaction: discord.Interaction):
            jobs = self.state.jobs("pending")[-5:]
            view = SafeView(timeout=180)
            view.add_item(WatchSelect(self))
            embed = discord.Embed(
                title="Core Monitoring",
                description="Select which emulator cores CATui should monitor in the background for automatic update alerts.",
                color=0x5865F2
            )
            embed.add_field(name="Builds", value="[ENABLED]" if self.config.updates_enabled else "[DISABLED] (setup mode)", inline=True)
            if jobs:
                pending = [f"* **{CORE_NAMES[j['core']]}** -- `{json.loads(j['payload'])['version']}`" for j in jobs]
                embed.add_field(name="Awaiting Approval", value=safe_text("\n".join(pending), 950), inline=False)
            else:
                embed.add_field(name="Awaiting Approval", value="None", inline=False)
            await self.discord_io.call(interaction.response.send_message, embed=embed, view=view, ephemeral=True)

        @self.tree.command(name="cores-check", description="Check emulator cores and show available updates", guild=guild)
        @app_commands.describe(core="Optional: check a specific core instead of all cores")
        @app_commands.choices(core=[
            app_commands.Choice(name=CORE_NAMES[c], value=c) for c in CORE_IDS
        ])
        async def check(interaction: discord.Interaction, core: str = None):
            await self.discord_io.call(interaction.response.defer, ephemeral=False)
            core_val = core.value if hasattr(core, "value") else core
            target_cores = [core_val] if core_val else list(CORE_IDS)
            report = await self.service.scan(cores=target_cores, manual=True)

            if report.busy:
                embed = card("Core Check", "A check is already running. Try /cores-check again in a moment.", 0xE8A05B)
                await self.discord_io.call(interaction.followup.send, embed=embed, ephemeral=False)
                return

            if not report.results:
                embed = card("Core Check", "No cores could be checked. Try /cores-check again in a moment.", 0xE8A05B)
                await self.discord_io.call(interaction.followup.send, embed=embed, ephemeral=False)
                return

            # Send each core as its own separate card with its own dedicated action buttons directly underneath
            for item in report.results:
                async with self.notification_lock:
                    job = self.state.get(item["job_id"]) if item.get("job_id") else None
                    embed = core_card(item, self.state, self.config)
                    view = core_controls(self, item)
                    message = await self.discord_io.call(interaction.followup.send, embed=embed, view=view, ephemeral=False, wait=True)
                    if job and job["status"] == "pending" and not job["message_id"]:
                        self.state.update(job["id"], message_id=str(message.id))

            if report.errors:
                log.warning("Core checks incomplete: %s", "; ".join(report.errors))

        @self.tree.command(name="chat", description="Ask about CATui status", guild=guild)
        @app_commands.describe(message="Your question about CATui or core updates")
        async def talk(interaction: discord.Interaction, message: str):
            await self.discord_io.call(interaction.response.defer, ephemeral=True)
            answer = await self.chat(message)
            await self.discord_io.call(interaction.followup.send, embed=card("CATui > assistant", answer), ephemeral=True)

        @self.tree.command(name="admin-add", description="Add a new administrator to the bot (Owner only)", guild=guild)
        @app_commands.describe(user="The Discord user to promote to bot administrator")
        async def admin_add_cmd(interaction: discord.Interaction, user: discord.User):
            if user.bot:
                await self.discord_io.call(interaction.response.send_message, "Bots cannot be administrators.", ephemeral=True)
                return
            if user.id == self.config.owner_id:
                await self.discord_io.call(interaction.response.send_message, f"{user.mention} is already the bot owner.", ephemeral=True)
                return
            self.state.add_admin(user.id)
            await self.discord_io.call(interaction.response.send_message, f"Successfully added {user.mention} (`{user.id}`) as an administrator.", ephemeral=True)

        @self.tree.command(name="admin-remove", description="Remove an administrator from the bot (Owner only)", guild=guild)
        @app_commands.describe(user="The Discord user to remove from administrators")
        async def admin_remove_cmd(interaction: discord.Interaction, user: discord.User):
            if user.id == self.config.owner_id:
                await self.discord_io.call(interaction.response.send_message, "Cannot remove the bot owner.", ephemeral=True)
                return
            self.state.remove_admin(user.id)
            await self.discord_io.call(interaction.response.send_message, f"Removed {user.mention} (`{user.id}`) from administrators.", ephemeral=True)

        @self.tree.command(name="admins", description="List all bot administrators", guild=guild)
        async def admins_cmd(interaction: discord.Interaction):
            admin_ids = self.state.admins()
            lines = [f"* **Owner:** <@{self.config.owner_id}> (`{self.config.owner_id}`)"]
            for aid in admin_ids:
                lines.append(f"* **Admin:** <@{aid}> (`{aid}`)")
            embed = discord.Embed(
                title="Bot Administrators",
                description="\n".join(lines),
                color=0x5865F2
            )
            await self.discord_io.call(interaction.response.send_message, embed=embed, ephemeral=True)

        @self.tree.command(name="issue", description="Create, update, or delete a GitHub issue in catui-plataform/CATui from this gallery post", guild=guild)
        @app_commands.describe(
            message="Optional text note, message IDs, or links (space-separated, e.g. 'another test 1562184884800192664')",
            new="Set to True to create a new issue instead of updating the existing one",
            solved="Set to True to mark as solved/closed, or False to reopen the issue and thread",
            delete="Set to True to permanently delete the issue on GitHub and delete this Discord thread"
        )
        async def issue_cmd(
            interaction: discord.Interaction,
            message: str = None,
            new: bool = False,
            solved: Optional[bool] = None,
            delete: bool = False
        ):
            await self.discord_io.call(interaction.response.defer, ephemeral=False)
            target = interaction.channel
            is_thread = isinstance(target, discord.Thread) or getattr(target, "type", None) in (
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
                discord.ChannelType.news_thread
            )
            if not is_thread:
                await self.discord_io.call(
                    interaction.followup.send,
                    f"Please run this command inside an issue post/thread in <#{self.config.issues_channel_id}>.",
                    ephemeral=True
                )
                return

            starter_msg = getattr(target, "starter_message", None)
            if starter_msg is None:
                try:
                    starter_msg = await self.discord_io.call(target.fetch_message, target.id)
                except Exception:
                    try:
                        async for msg in target.history(limit=1, oldest_first=True):
                            starter_msg = msg
                            break
                    except Exception:
                        starter_msg = None

            starter_imgs = []
            starter_files = []
            if starter_msg and starter_msg.attachments:
                for att in starter_msg.attachments:
                    fn = att.filename.lower()
                    if any(fn.endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")) or (att.content_type and att.content_type.startswith("image/")):
                        starter_imgs.append({"filename": att.filename, "url": att.url})
                    else:
                        starter_files.append({"filename": att.filename, "url": att.url})

            starter_data = {
                "author": starter_msg.author.display_name if starter_msg else "",
                "is_admin": self.state.is_admin(starter_msg.author.id, self.config.owner_id) if starter_msg else False,
                "content": starter_msg.content if starter_msg else "",
                "images": starter_imgs,
                "files": starter_files
            }

            # Check existing issue for this thread (SQLite first, then scan thread history fallback)
            target_repo = self.config.issues_repo.strip()
            existing = None
            if not new:
                existing = self.state.get_thread_issue(target.id)
                if not existing:
                    existing = await find_existing_issue_in_thread(target, target_repo)
                    if existing:
                        self.state.save_thread_issue(target.id, existing["repo"], existing["issue_number"], existing["issue_url"])

            # Parse message input (separates message IDs/links, direct images, and text notes)
            msg_refs, parsed_images, note_text = parse_message_inputs(message)
            all_direct_attachments = list(parsed_images)

            # Extract tags from the Discord thread (forum post)
            thread_tag_objects = getattr(target, "applied_tags", []) or []
            thread_tags = [t.name for t in thread_tag_objects if hasattr(t, "name")]
            thread_tags_lower = [t.lower().strip() for t in thread_tags]

            is_delete = bool(delete)
            is_solved = False
            is_reopen = False

            if not is_delete:
                if solved is True:
                    is_solved = True
                elif solved is False:
                    is_reopen = True

            if note_text:
                note_lower = note_text.lower().strip()
                if note_lower in ("delete", "--delete", "destroy", "remove"):
                    is_delete = True
                    note_text = ""
                elif note_lower.endswith(" delete") or note_lower.endswith(" --delete"):
                    is_delete = True
                    note_text = note_text.rsplit(" ", 1)[0].strip()
                elif note_lower.startswith("delete ") or note_lower.startswith("--delete "):
                    is_delete = True
                    note_text = note_text.split(" ", 1)[1].strip()
                elif not is_delete:
                    if note_lower in ("reopen", "--reopen", "unsolve", "unsolved"):
                        is_reopen = True
                        is_solved = False
                        note_text = ""
                    elif note_lower.endswith(" reopen") or note_lower.endswith(" unsolved"):
                        is_reopen = True
                        is_solved = False
                        note_text = note_text.rsplit(" ", 1)[0].strip()
                    elif note_lower.startswith("reopen ") or note_lower.startswith("unsolve "):
                        is_reopen = True
                        is_solved = False
                        note_text = note_text.split(" ", 1)[1].strip()
                    elif not is_reopen and not is_solved and solved is None:
                        if note_lower in ("solved", "--solved"):
                            is_solved = True
                            note_text = ""
                        elif note_lower.endswith(" solved"):
                            is_solved = True
                            note_text = note_text[:-7].strip()
                        elif note_lower.startswith("solved "):
                            is_solved = True
                            note_text = note_text[7:].strip()

            if is_delete:
                target_repo = self.config.issues_repo.strip()
                existing = self.state.get_thread_issue(target.id)
                if not existing:
                    existing = await find_existing_issue_in_thread(target, target_repo)

                issue_num = existing.get("issue_number") if existing else None
                repo_name = existing.get("repo", target_repo) if existing else target_repo

                try:
                    await self.service.delete_github_issue(
                        thread_id=target.id,
                        repo=repo_name,
                        issue_number=issue_num
                    )
                except Exception as exc:
                    log.warning("Failed to delete issue on GitHub: %s", exc)

                issue_info = f"GitHub Issue #{issue_num}" if issue_num else "unlinked issue"
                await self.discord_io.call(
                    interaction.followup.send,
                    f"Deleted {issue_info} and deleting Discord thread..."
                )
                try:
                    await self.discord_io.call(target.delete)
                except Exception as exc:
                    log.warning("Failed to delete Discord thread %s: %s", target.id, exc)
                return

            if not is_solved and not is_reopen and solved is None:
                if "solved" in thread_tags_lower or "resolvido" in thread_tags_lower:
                    is_solved = True

            # Process extra messages
            extra_messages = []
            for ref in msg_refs:
                mid = ref["message_id"]
                cid = ref.get("channel_id")
                m = None
                if cid:
                    try:
                        ch = self.get_channel(cid) or await self.discord_io.call(self.fetch_channel, cid)
                        if ch:
                            m = await self.discord_io.call(ch.fetch_message, mid)
                    except Exception:
                        m = None
                if not m:
                    try:
                        m = await self.discord_io.call(target.fetch_message, mid)
                    except Exception:
                        parent = getattr(target, "parent", None)
                        if parent:
                            try:
                                m = await self.discord_io.call(parent.fetch_message, mid)
                            except Exception:
                                m = None
                if not m:
                    continue

                m_imgs = []
                m_files = []
                for att in m.attachments:
                    fn = att.filename.lower()
                    if any(fn.endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")) or (att.content_type and att.content_type.startswith("image/")):
                        m_imgs.append({"filename": att.filename, "url": att.url})
                    else:
                        m_files.append({"filename": att.filename, "url": att.url})

                # Check text for embedded image URLs
                for url in re.findall(r"https?://\S+", m.content or ""):
                    cleaned = url.rstrip(")>],.;")
                    lower = cleaned.lower()
                    if any(lower.endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")) or ("cdn.discordapp.com/attachments/" in lower) or ("media.discordapp.net/attachments/" in lower):
                        if not any(i["url"] == cleaned for i in m_imgs):
                            fn = cleaned.split("/")[-1].split("?")[0] or "Screenshot"
                            m_imgs.append({"filename": fn, "url": cleaned})

                extra_messages.append({
                    "id": m.id,
                    "author": m.author.display_name,
                    "is_admin": self.state.is_admin(m.author.id, self.config.owner_id),
                    "content": m.content,
                    "images": m_imgs,
                    "files": m_files
                })

            try:
                created = await self.service.create_github_issue(
                    thread_title=target.name,
                    starter=starter_data,
                    extra_messages=extra_messages,
                    attachments=all_direct_attachments,
                    description=note_text,
                    thread_id=target.id,
                    new=new,
                    existing_issue=existing,
                    solved=is_solved,
                    reopen=is_reopen,
                    thread_tags=thread_tags
                )
            except RemoteError as exc:
                if exc.status == 410:
                    err_msg = (
                        f"GitHub returned HTTP 410 (Gone): Issues are disabled on repository `{self.config.issues_repo}`.\n"
                        f"Please enable Issues in GitHub: https://github.com/{self.config.issues_repo}/settings -> Features -> Check 'Issues'."
                    )
                elif exc.status in {401, 403, 404}:
                    err_msg = (
                        f"GitHub returned HTTP {exc.status}. "
                        f"Please ensure the bot's GITHUB_TOKEN has write access to `{self.config.issues_repo}`."
                    )
                else:
                    err_msg = f"GitHub API error: {exc}"
                await self.discord_io.call(interaction.followup.send, err_msg, ephemeral=True)
                return
            except Exception as exc:
                await self.discord_io.call(interaction.followup.send, f"Failed to create issue: {exc}", ephemeral=True)
                return

            action = created.get("action", "")
            is_action_solved = action == "solved"
            is_action_reopened = action == "reopened"
            is_updated = action == "updated"
            labels_str = ", ".join(created["labels"]) if created["labels"] else "none"

            if is_action_solved:
                title_text = f"GitHub Issue #{created['number']} (Solved)"
                desc_text = f"**[{created['title']}]({created['url']})**\n*(Issue marked as solved and closed on GitHub)*"
                embed_color = 0x2ecc71
            elif is_action_reopened:
                title_text = f"GitHub Issue #{created['number']} (Reopened)"
                desc_text = f"**[{created['title']}]({created['url']})**\n*(Issue reopened on GitHub and thread unlocked)*"
                embed_color = 0xFEE75C
            elif is_updated:
                title_text = f"GitHub Issue #{created['number']} (Updated)"
                desc_text = f"**[{created['title']}]({created['url']})**\n*(Updated existing issue on GitHub with latest notes & comments)*"
                embed_color = 0x5865F2
            else:
                title_text = f"GitHub Issue #{created['number']}"
                desc_text = f"**[{created['title']}]({created['url']})**"
                embed_color = 0x57F287

            embed = discord.Embed(
                title=title_text,
                url=created["url"],
                description=desc_text,
                color=embed_color
            )
            embed.add_field(name="Repository", value=f"`{created['repo']}`", inline=True)
            embed.add_field(name="Labels", value=f"`{labels_str}`", inline=True)
            if is_action_solved:
                embed.add_field(name="Status", value="`Closed (Solved)`", inline=True)
            elif is_action_reopened:
                embed.add_field(name="Status", value="`Open (Reopened)`", inline=True)
            elif starter_data["author"]:
                embed.add_field(name="Reported by", value=starter_data["author"], inline=True)

            if extra_messages or all_direct_attachments:
                total_imgs = sum(len(m["images"]) for m in extra_messages) + len([a for a in all_direct_attachments if any(str(a["url"]).lower().endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))])
                embed.add_field(
                    name="Extra Context Included",
                    value=f"{len(extra_messages)} message(s) ({total_imgs} screenshot(s)/file(s))",
                    inline=False
                )
            if (is_updated or is_action_solved or is_action_reopened) and note_text:
                if is_action_solved:
                    field_title = "Maintainer Resolution Note"
                elif is_action_reopened:
                    field_title = "Maintainer Reopen Note"
                else:
                    field_title = "Maintainer Update Note"
                embed.add_field(name=field_title, value=note_text[:1000], inline=False)

            view = SafeView(timeout=180)
            view.add_item(discord.ui.Button(label="View Issue", url=created["url"]))
            await self.discord_io.call(interaction.followup.send, embed=embed, view=view)

            if is_action_solved and is_thread:
                try:
                    edit_kwargs = {"locked": True, "archived": True}
                    if hasattr(target, "applied_tags"):
                        parent_channel = getattr(target, "parent", None)
                        if not parent_channel and hasattr(target, "parent_id") and target.parent_id:
                            parent_channel = self.get_channel(target.parent_id)
                            if not parent_channel:
                                parent_channel = await self.discord_io.call(self.fetch_channel, target.parent_id)
                        if parent_channel and hasattr(parent_channel, "available_tags"):
                            solved_tag = next((t for t in parent_channel.available_tags if t.name.lower().strip() in ("solved", "resolvido", "fechado")), None)
                            if solved_tag and solved_tag not in target.applied_tags:
                                new_tags = list(target.applied_tags) + [solved_tag]
                                if len(new_tags) > 5:
                                    new_tags = new_tags[-5:]
                                edit_kwargs["applied_tags"] = new_tags
                    await self.discord_io.call(target.edit, **edit_kwargs)
                except Exception as exc:
                    log.warning("Could not close/lock solved Discord thread %s: %s", getattr(target, "id", None), exc)

            elif is_action_reopened and is_thread:
                try:
                    edit_kwargs = {"locked": False, "archived": False}
                    if hasattr(target, "applied_tags"):
                        remaining_tags = [
                            t for t in target.applied_tags
                            if getattr(t, "name", "").lower().strip() not in ("solved", "resolvido", "fechado")
                        ]
                        if len(remaining_tags) != len(target.applied_tags):
                            edit_kwargs["applied_tags"] = remaining_tags
                    await self.discord_io.call(target.edit, **edit_kwargs)
                except Exception as exc:
                    log.warning("Could not unlock/unarchive reopened Discord thread %s: %s", getattr(target, "id", None), exc)

        @self.tree.command(name="release", description="Format the latest CATui release into the official announcement template", guild=guild)
        @app_commands.describe(
            tag="Optional: release tag to format (defaults to the latest release)",
            preview="Set to True to preview privately before sending"
        )
        async def release_cmd(interaction: discord.Interaction, tag: str = None, preview: bool = False):
            await self.discord_io.call(interaction.response.defer, ephemeral=preview)
            try:
                res = await self.service.get_release_announcement(tag=tag)
            except Exception as exc:
                await self.discord_io.call(interaction.followup.send, f"Could not format release: {exc}", ephemeral=True)
                return

            content = res["announcement"]
            if preview:
                view = SafeView(timeout=180)
                publish_btn = discord.ui.Button(label="Publish to Channel", style=discord.ButtonStyle.success)

                async def publish_callback(btn_interaction: discord.Interaction):
                    if not self.state.is_admin(btn_interaction.user.id, self.config.owner_id):
                        await self.discord_io.call(btn_interaction.response.send_message, "Access denied.", ephemeral=True)
                        return
                    if len(content) > 1950 and "```\n" in content:
                        split_idx = content.rfind("```\n") + 3
                        await self.discord_io.call(interaction.channel.send, content[:split_idx])
                        await self.discord_io.call(interaction.channel.send, content[split_idx:].strip())
                    else:
                        await self.discord_io.call(interaction.channel.send, content)
                    publish_btn.disabled = True
                    publish_btn.label = "Published"
                    await self.discord_io.call(btn_interaction.response.edit_message, view=view)

                publish_btn.callback = publish_callback
                view.add_item(publish_btn)
                if len(content) > 1950 and "```\n" in content:
                    split_idx = content.rfind("```\n") + 3
                    await self.discord_io.call(interaction.followup.send, content[:split_idx], ephemeral=True)
                    await self.discord_io.call(interaction.followup.send, content[split_idx:].strip(), view=view, ephemeral=True)
                else:
                    await self.discord_io.call(interaction.followup.send, content, view=view, ephemeral=True)
            else:
                if len(content) > 1950 and "```\n" in content:
                    split_idx = content.rfind("```\n") + 3
                    await self.discord_io.call(interaction.followup.send, content[:split_idx], ephemeral=False)
                    await self.discord_io.call(interaction.followup.send, content[split_idx:].strip(), ephemeral=False)
                else:
                    await self.discord_io.call(interaction.followup.send, content, ephemeral=False)

        await self.discord_io.call(self.tree.sync, guild=guild)
        for job in self.state.jobs("pending"):
            self.add_view(Proposal(self, job["id"]))
        for job in self.state.jobs("running", "dispatching", "uncertain"):
            self.add_view(UpdatingView(run_url=job.get("run_url"), bot=self, job_id=job["id"]))
        self.monitor.start()

    async def on_ready(self):
        self.private_ready = False
        channel = await self.discord_io.call(self.fetch_channel, self.config.channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.config.guild_id:
            raise RuntimeError("Configured channel must be a guild text channel")
        if channel.permissions_for(channel.guild.default_role).view_channel:
            log.error("Refusing to operate: configured channel is public")
            await self.close()
            return
        for role in channel.guild.roles:
            if role.is_default() or role.permissions.administrator:
                continue
            if role.is_bot_managed() and role.tags and role.tags.bot_id == self.user.id:
                continue
            if channel.permissions_for(role).view_channel:
                log.error("Refusing to operate: another role can read the control channel")
                await self.close()
                return
        for member, overwrite in channel.overwrites.items():
            if isinstance(member, discord.Member) and member.id not in {self.config.owner_id, self.user.id} and overwrite.view_channel:
                log.error("Refusing to operate: another member can read the control channel")
                await self.close()
                return
        self.control_channel = channel
        try:
            release_channel = await self.discord_io.call(self.fetch_channel, self.config.release_channel_id)
            if (not isinstance(release_channel, discord.TextChannel)
                    or release_channel.guild.id != self.config.guild_id):
                raise RuntimeError("Configured release destination must be a text channel in the configured server")
            bot_member = release_channel.guild.me
            if bot_member is None or not release_channel.permissions_for(bot_member).send_messages:
                raise RuntimeError("Bot does not have permission to send messages in the release channel")
            self.release_channel = release_channel
            log.info("CATui release announcements enabled for channel %s", self.config.release_channel_id)
        except Exception as exc:
            self.release_channel = None
            log.warning("CATui release announcements unavailable: %s", type(exc).__name__)
        owner = await self.discord_io.call(channel.guild.fetch_member, self.config.owner_id)
        if owner.bot or not channel.permissions_for(owner).view_channel:
            log.error("DISCORD_OWNER_ID must identify the human owner with access to the channel")
            await self.close()
            return
        self.private_ready = True
        log.info("Private control channel ready")

    async def on_guild_channel_update(self, before, after):
        if after.id == self.config.channel_id:
            self.private_ready = False
            await self.on_ready()

    async def on_guild_role_update(self, before, after):
        if after.guild.id == self.config.guild_id:
            self.private_ready = False
            await self.on_ready()

    async def chat(self, question):
        if self.chat_lock.locked() or time.monotonic() - self.last_chat < 5:
            return "I am still responding. Please wait a few seconds."
        async with self.chat_lock:
            self.last_chat = time.monotonic()
            jobs = [{"core": j["core"], "status": j["status"], "version": json.loads(j["payload"])["version"],
                     "changes": j["notes"], "run": j["run_url"]} for j in self.state.jobs("pending", "running", "published", "failed")[-8:]]
            return await self.assistant.answer(question, {"watchlist": self.state.watches(), "jobs": jobs})

    async def on_message(self, message):
        if not self.discord_io.ready or message.author.bot or not self.allowed(message.author.id, message.guild.id if message.guild else None, message.channel.id):
            return
        if message.reference and message.content.strip().lower() in {"yes", "update", "approve", "go ahead", "sim"}:
            job = next((j for j in self.state.jobs("pending") if j["message_id"] == str(message.reference.message_id)), None)
            if job:
                await self.discord_io.call(message.reply, await self.service.approve(job["id"]), mention_author=False)
                return
        try:
            await self.discord_io.call(message.reply, embed=card("CATui > assistant", await self.chat(message.content)), mention_author=False)
        except (DiscordRequestFailed, discord.HTTPException):
            return
        except Exception as exc:
            log.warning("Chat failed: %s", type(exc).__name__)
            await self.discord_io.call(message.reply, "The AI assistant did not respond. Commands and buttons are still available.", mention_author=False)

    async def send_updates(self):
        async with self.notification_lock:
            await self._send_updates()

    async def _send_updates(self):
        for job in self.state.jobs("running", "uncertain", "dispatching"):
            if not job.get("stage") or job["stage"] == job.get("reported_stage") or not job["message_id"]:
                continue
            min_pct = job.get("stage_pct") or 0
            cur_pct = stage_percentage(job["stage"])
            new_pct = max(min_pct, cur_pct)
            embed = updating_card(job["core"], job["stage"], job["sha"], job.get("run_url"), min_pct=new_pct)
            view = UpdatingView(run_url=job.get("run_url"), bot=self, job_id=job["id"])
            message = self.control_channel.get_partial_message(int(job["message_id"]))
            try:
                await self.discord_io.call(message.edit, embed=embed, view=view)
            except Exception as exc:
                log.warning("Could not edit progress card %s: %s", job["message_id"], exc)
                continue
            self.state.update(job["id"], reported_stage=job["stage"], stage_pct=new_pct)
        for job in self.state.jobs("pending"):
            if job["message_id"]:
                continue
            plan = json.loads(job["payload"])
            if not self.state.watches().get(plan["core"]):
                continue
            core_name = CORE_NAMES.get(plan["core"], plan["core"])
            embed = discord.Embed(
                title=f"{core_name} [update ready]",
                description=safe_text(job["notes"], 1800),
                color=0xFEE75C
            )
            embed.add_field(name="Commit", value=f"`{plan['sha'][:12]}`", inline=True)
            embed.add_field(name="Expires", value=f"<t:{plan['expires']}:R>", inline=True)
            add_release_field(embed, self.state.release_summary(plan["core"]))
            embed.add_field(
                name="Action Required",
                value="Click **Approve update** to build and publish, or **Skip** to ignore this version.\nYou can also reply **yes** to this message to approve.",
                inline=False
            )
            message = await self.discord_io.call(self.control_channel.send, embed=embed, view=Proposal(self, job["id"]))
            self.state.update(job["id"], message_id=str(message.id))
        for job in self.state.unnotified():
            ok = job["status"] == "published"
            embed = finished_card(job, ok)
            view = FinishedView(self, job, ok)
            edited = False
            if job.get("message_id"):
                try:
                    message = self.control_channel.get_partial_message(int(job["message_id"]))
                    await self.discord_io.call(message.edit, embed=embed, view=view)
                    edited = True
                except Exception as exc:
                    log.warning("Could not edit card %s on finish: %s", job["message_id"], exc)
            if not edited:
                await self.discord_io.call(self.control_channel.send, embed=embed, view=view)
            self.state.update(job["id"], notified=1)

    async def check_catui_releases(self):
        releases = await self.service.upstream.catui_releases()
        repository = "catui-plataform/CATui"
        cursor = self.state.release_announcement_cursor(repository)
        if cursor is None:
            if releases:
                releases.sort(key=lambda release: (release["published_at"], release["id"]))
                latest = releases[-1]
                self.state.save_release_announcement_cursor(
                    repository, latest["id"], latest["published_at"], latest["tag"]
                )
                log.info("Initialized CATui release watcher at %s; existing releases will not be reposted", latest["tag"])
            else:
                self.state.save_release_announcement_cursor(repository, 0, "", "")
                log.info("Initialized CATui release watcher with no existing releases")
            return
        if not releases:
            return
        releases.sort(key=lambda release: (release["published_at"], release["id"]))

        cursor_key = (cursor["published_at"], int(cursor["release_id"]))
        new_releases = [
            release for release in releases
            if (release["published_at"], release["id"]) > cursor_key
        ]
        for release in new_releases:
            if self.release_channel is None:
                try:
                    channel = await self.discord_io.call(self.fetch_channel, self.config.release_channel_id)
                    if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.config.guild_id:
                        raise RuntimeError("Invalid release channel")
                    bot_member = channel.guild.me
                    if bot_member is None or not channel.permissions_for(bot_member).send_messages:
                        raise RuntimeError("Missing send permission")
                    self.release_channel = channel
                except Exception as exc:
                    log.warning("Could not resolve CATui release channel: %s", type(exc).__name__)
                    return
            try:
                announcement = await self.assistant.format_release(
                    tag_name=release["tag"],
                    release_name=release["name"],
                    release_body=release["body"],
                    release_url=release["url"],
                )
            except Exception:
                log.exception("Could not format CATui release announcement for %s", release["tag"])
                return
            try:
                await send_release_announcement(self.release_channel, self.discord_io, announcement)
            except Exception:
                log.exception("Could not publish CATui release announcement for %s", release["tag"])
                return
            self.state.save_release_announcement_cursor(
                repository, release["id"], release["published_at"], release["tag"]
            )
            log.info("Published CATui release announcement for %s", release["tag"])

    @tasks.loop(seconds=10)
    async def monitor(self):
        if not self.discord_io.ready:
            return
        try:
            if not self.private_ready:
                await self.on_ready()
                if not self.private_ready:
                    return
            if time.monotonic() - self.last_recovery_check >= 60:
                await self.service.resume_after_update()
                self.last_recovery_check = time.monotonic()
            if time.monotonic() - self.last_scan >= self.config.interval:
                self.last_scan = time.monotonic()
                report = await self.service.scan()
                if report.errors:
                    log.warning("Core checks incomplete: %s", "; ".join(report.errors))
                await self.send_updates()
            if time.monotonic() - self.last_release_scan >= 300:
                self.last_release_scan = time.monotonic()
                await self.check_catui_releases()
            active = self.state.jobs("running", "uncertain", "dispatching")
            if active or self.state.unnotified():
                await self.service.reconcile()
                await self.send_updates()
        except Exception as exc:
            log.warning("Monitor failed: %s", type(exc).__name__)

    @monitor.before_loop
    async def before_monitor(self):
        await self.wait_until_ready()

    async def on_error(self, event_method, *args, **kwargs):
        log.warning("Discord event %s failed: %s", event_method, type(sys.exception()).__name__)

    async def close(self):
        self.monitor.cancel()
        if hasattr(self, "session"):
            await self.session.close()
        self.state.close()
        await super().close()


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Do not enable HTTP/debug loggers: they may expose credentials.
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    bot = Bot(Config.load())
    try:
        bot.run(bot.config.discord_token, log_handler=None)
    except DiscordRequestFailed:
        log.error("Discord connection is temporarily unavailable. Check the hosting network before restarting.")
