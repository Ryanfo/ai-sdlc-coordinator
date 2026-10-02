"""Session guardrails: stop a Claude session that is looping or has stalled.

Turn limits are a poor safety net on their own: set low, they stop good work part-way; set
high, a stuck session burns time and subscription usage. With generous turn limits, these
checks watch the session's live log (written as it runs) and stop it early when it is clearly
not making progress:

* Loop: the same tool call with the same result keeps recurring (for example the same failing
  command or the same rejected edit, `loop_repeats` times within the last `LOOP_WINDOW` steps).
  Running the tests again after changing code is not a loop: the result differs.
* Stall: nothing new in the log for `stall_minutes` (a single tool call is capped well below).

A stopped session is reported as Blocked with the reason and the last log lines, and its
unfinished changes are kept so that Resume continues from them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

LOOP_WINDOW = 30


def _key(name: str, inp: Any, result: Any) -> str:
    blob = json.dumps([name, inp, result], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class LoopDetector:
    def __init__(self, repeats: int, window: int = LOOP_WINDOW) -> None:
        self.repeats = repeats
        self.recent: deque[tuple[str, str]] = deque(maxlen=window)
        self.pending: dict[str, tuple[str, Any]] = {}

    def feed(self, ev: dict[str, Any]) -> str | None:
        """Consume one stream-json event; return a reason when a loop is detected."""
        msg = ev.get("message") or {}
        for c in msg.get("content") or []:
            if not isinstance(c, dict):
                continue
            if ev.get("type") == "assistant" and c.get("type") == "tool_use":
                self.pending[str(c.get("id"))] = (str(c.get("name")), c.get("input"))
            elif ev.get("type") == "user" and c.get("type") == "tool_result":
                call = self.pending.pop(str(c.get("tool_use_id")), None)
                if call is None:
                    continue
                name, inp = call
                self.recent.append((_key(name, inp, c.get("content")), _describe(name, inp)))
                key, count = Counter(k for k, _ in self.recent).most_common(1)[0]
                if count >= self.repeats:
                    what = next(d for k, d in self.recent if k == key)
                    return (
                        f"it repeated the same step with the same result {count} times in its last "
                        f"{len(self.recent)} steps ({what})"
                    )
        return None


def _describe(name: str, inp: Any) -> str:
    if isinstance(inp, dict):
        if "command" in inp:
            return f"{name}: {' '.join(str(inp['command']).split())[:120]}"
        if "file_path" in inp:
            return f"{name}: {inp['file_path']}"
    return name


class Watchdog:
    """Tails a session's live log and calls ``stop`` once if a guardrail trips."""

    def __init__(
        self,
        log: Path,
        *,
        loop_repeats: int,
        stall_seconds: float,
        stop: Callable[[], Awaitable[None]],
        poll: float = 1.0,
    ) -> None:
        self.log = log
        self.loops = LoopDetector(loop_repeats)
        self.stall_seconds = stall_seconds
        self.stop = stop
        self.poll = poll
        self.reason: str | None = None

    async def run(self) -> None:
        offset = 0
        buffer = ""
        last_activity = time.monotonic()
        while True:
            await asyncio.sleep(self.poll)
            try:
                size = self.log.stat().st_size
            except FileNotFoundError:
                size = 0
            if size > offset:
                with self.log.open() as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                offset = size
                last_activity = time.monotonic()
                buffer += chunk
                *lines, buffer = buffer.split("\n")
                for line in lines:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev, dict) and (why := self.loops.feed(ev)):
                        await self._trip(why)
                        return
            elif time.monotonic() - last_activity > self.stall_seconds:
                await self._trip(f"nothing happened for {self.stall_seconds / 60:.0f} minutes")
                return

    async def _trip(self, reason: str) -> None:
        self.reason = reason
        await self.stop()
