"""Bound Discord traffic and stop immediately on an upstream rate-limit block."""
import asyncio
import logging
import math
import time

import discord

log = logging.getLogger("catui")


class DiscordRequestFailed(RuntimeError):
    """A transport failure without response bodies, URLs, or credentials."""


class DiscordUnavailable(DiscordRequestFailed):
    pass


class DiscordIO:
    def __init__(self, *, clock=time.monotonic, interval=0.5):
        self.clock = clock
        self.interval = interval
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.lock = asyncio.Lock()

    @property
    def ready(self):
        return self.clock() >= self.blocked_until

    def block(self, retry_after=None):
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = 300.0
        if not math.isfinite(delay) or delay < 0:
            delay = 300.0
        delay = max(30.0, delay)
        self.blocked_until = max(self.blocked_until, self.clock() + delay)
        log.warning("Discord rate limited this connection. Outbound requests paused for %.0f seconds; no immediate retry.", delay)

    async def call(self, operation, *args, **kwargs):
        async with self.lock:
            if not self.ready:
                raise DiscordUnavailable("Discord requests are paused after a rate limit")
            wait = self.next_request - self.clock()
            if wait > 0:
                await asyncio.sleep(min(wait, self.interval))
            if not self.ready:
                raise DiscordUnavailable("Discord requests are paused after a rate limit")
            self.next_request = self.clock() + self.interval
        try:
            return await operation(*args, **kwargs)
        except discord.HTTPException as exc:
            if exc.status == 429:
                self.block(getattr(exc.response, "headers", {}).get("Retry-After"))
                raise DiscordUnavailable("Discord rate limit; outbound requests paused") from None
            log.warning("Discord request failed: HTTP %s, code %s", exc.status, exc.code)
            raise DiscordRequestFailed("Discord request failed") from None
        except discord.RateLimited as exc:
            self.block(exc.retry_after)
            raise DiscordUnavailable("Discord rate limit; outbound requests paused") from None
