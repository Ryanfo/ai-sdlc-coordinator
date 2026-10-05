"""Alerts about the coordinator itself: a Slack-compatible webhook and macOS notifications.

Ticket comments are for the people working on the ticket. What concerns the person running the
coordinator goes to the coordinator window and, with ``[notifications]``, to a webhook and the
desktop as well: it stopped unexpectedly or hit an internal error, Claude cannot be used, Claude
Code changed and failed its sandbox probe, the machine is short of room. With
``operational = "operator"`` the notices about Claude and internal errors are not commented on
tickets at all, so a client's tickets carry nothing about the developer's machine.

Alerts never fail or delay the work: a failed post is logged and dropped. The same alert (by
key) is sent at most once an hour.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from delivery.config import Config
from delivery.proc import ProcessStartError, base_child_env, run_process

log = logging.getLogger("delivery")
REPEAT_SECONDS = 3600.0

Post = Callable[[str, str], Awaitable[None]]
Notify = Callable[[str, str], Awaitable[None]]


async def post_webhook(url: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json={"text": text})
        r.raise_for_status()


async def desktop_notification(title: str, text: str) -> None:
    """A macOS notification. The text is passed as an argument, never inside the script."""
    if sys.platform != "darwin":
        return
    script = ["-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv)"]
    script += ["-e", "end run"]
    argv = ["osascript", *script, text[:240], title[:80]]
    await run_process(argv, cwd=Path.home(), env=base_child_env(), timeout=10)


class Alerts:
    def __init__(
        self,
        cfg: Config,
        *,
        post: Post | None = None,
        notify: Notify | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.post = post or post_webhook
        self.notify = notify or desktop_notification
        self.clock = clock
        self._sent: dict[str, float] = {}

    @property
    def on_tickets(self) -> bool:
        """Whether notices about this machine are also commented on the affected ticket."""
        return self.cfg.notifications.operational == "jira"

    def webhook_url(self) -> str | None:
        env = self.cfg.notifications.webhook_env
        return os.environ.get(env) or None if env else None

    async def send(self, key: str, title: str, text: str) -> bool:
        """Send one alert, unless the same ``key`` was sent within the last hour."""
        now = self.clock()
        if key in self._sent and now - self._sent[key] < REPEAT_SECONDS:
            return False
        self._sent[key] = now
        worker = self.cfg.identity.worker_id
        jobs = []
        url = self.webhook_url()
        if url:
            jobs.append(self._safe(self.post(url, f"*Delivery coordinator on {worker}: {title}*\n{text}")))
        if self.cfg.notifications.desktop:
            jobs.append(self._safe(self.notify(f"Delivery coordinator: {title}", text)))
        if jobs:
            await asyncio.gather(*jobs)
        return True

    @staticmethod
    async def _safe(job: Awaitable[None]) -> None:
        try:
            await job
        except (httpx.HTTPError, ProcessStartError, OSError) as exc:
            log.warning("alert not delivered: %s", exc)


__all__ = ["Alerts", "desktop_notification", "post_webhook"]
