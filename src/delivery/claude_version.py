"""Prove Claude's sandbox again when Claude Code changes (it updates itself).

The worker profile (docs/security-boundary.md) relies on Claude Code's own sandbox, deny rules
and ``--restricted``, and their behaviour can change in any release. ``delivery doctor
--claude-probe`` records the version and session mode it passed on in
``<state_dir>/claude-probe.json``. Every few minutes the supervisor compares that with
``claude --version``. When they differ, new sessions wait while it runs the same probe in the
background, and start once it passes. A failed probe keeps them waiting, says why in the
coordinator window and alerts; it is tried again every half hour and whenever the version
changes again. A pass recorded since (``delivery doctor --claude-probe``) ends the wait at
once. Because the probe asks Claude to try things, a failure is checked with a second run
before anything waits for it. Sessions already running are never stopped.

``[claude] probe_on_version_change = false`` turns this off.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from delivery import console
from delivery.alerts import Alerts
from delivery.config import Config
from delivery.journal import atomic_write_json
from delivery.models import utcnow

log = logging.getLogger("delivery")
STAMP = "claude-probe.json"
CHECK_SECONDS = 300.0
RETRY_FAILED_SECONDS = 1800.0
# Runs of the probe before a failure holds new sessions.
PROBE_ATTEMPTS = 2

Version = Callable[[], Coroutine[Any, Any, str]]
Probe = Callable[[], Coroutine[Any, Any, tuple[bool, str]]]


def mode(cfg: Config) -> str:
    return "interactive" if cfg.claude.interactive.enabled else "print"


def read_stamp(state_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((state_dir / STAMP).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_stamp(cfg: Config, version: str) -> None:
    cfg.runtime.state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        cfg.runtime.state_dir / STAMP,
        {"version": version, "mode": mode(cfg), "passed_at": utcnow().isoformat()},
    )


def proven(cfg: Config, version: str) -> bool:
    """Whether the sandbox probe passed on this version, in this session mode, here."""
    stamp = read_stamp(cfg.runtime.state_dir) or {}
    return stamp.get("version") == version and stamp.get("mode") == mode(cfg)


async def cli_version(cfg: Config) -> str:
    from delivery.claude import worker_env
    from delivery.proc import run_process

    res = await run_process(
        [cfg.claude.executable, "--version"], cwd=Path.home(), env=worker_env({}), timeout=30
    )
    return res.stdout.strip()


async def run_probe(cfg: Config) -> tuple[bool, str]:
    """``delivery doctor --claude-probe``: (passed, what failed)."""
    from delivery.doctor import Report, claude_probe

    report = Report()
    await claude_probe(cfg, report)
    failed = [
        f"{c.name}: {c.detail}" + (f" ({c.action})" if c.action else "")
        for c in report.checks
        if c.area == "probe" and c.level == "fail"
    ]
    return not failed, "; ".join(failed)


class VersionGuard:
    """Holds new sessions while the installed Claude Code has not passed the sandbox probe."""

    def __init__(
        self,
        cfg: Config,
        alerts: Alerts,
        emit: Callable[[str], None],
        *,
        version: Version | None = None,
        probe: Probe | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.alerts = alerts
        self.emit = emit
        self.version = version or (lambda: cli_version(cfg))
        self.probe = probe or (lambda: run_probe(cfg))
        self.clock = clock
        self.enabled = cfg.claude.probe_on_version_change
        self.current = ""
        self.failed = ""
        self._failed_at = ""
        self._attempts = 0
        self._task: asyncio.Task[tuple[bool, str]] | None = None
        self._next_check = 0.0

    @property
    def hold(self) -> str | None:
        """Why new sessions wait, or None."""
        if not self.enabled:
            return None
        if self._task is not None:
            return (
                f"Claude Code changed to {self.current}; checking its sandbox "
                "(`delivery doctor --claude-probe`)"
            )
        if self.failed:
            return (
                f"Claude Code {self.current} failed the sandbox probe ({self.failed}); run "
                "`delivery doctor --claude-probe` for details"
            )
        return None

    async def tick(self) -> None:
        if not self.enabled:
            return
        if self._task is not None:
            if self._task.done():
                await self._finished(self._task)
            return
        if self.failed and self._passed_since_failure():
            self.failed = ""
            self.emit(
                console.line(
                    f"Claude Code {self.current} has since passed the sandbox probe "
                    "(`delivery doctor --claude-probe`); new sessions start again"
                )
            )
            return
        now = self.clock()
        if now < self._next_check:
            return
        self._next_check = now + (RETRY_FAILED_SECONDS if self.failed else CHECK_SECONDS)
        try:
            version = await self.version()
        except Exception as exc:  # never let this stop the supervisor
            log.warning("could not read the Claude Code version: %s", exc)
            return
        if not version:
            return
        if version != self.current:
            self.failed = ""
        self.current = version
        if proven(self.cfg, version) and not self.failed:
            return
        self.emit(
            console.line(
                f"Claude Code is now {version}, which has not passed the sandbox probe on this machine; "
                "new sessions wait while it runs (running sessions carry on)"
            )
        )
        self._attempts = 0
        self._start_probe()

    def _start_probe(self) -> None:
        self._attempts += 1
        self._task = asyncio.create_task(self.probe(), name="claude-probe")

    def _passed_since_failure(self) -> bool:
        stamp = read_stamp(self.cfg.runtime.state_dir) or {}
        return proven(self.cfg, self.current) and str(stamp.get("passed_at", "")) > self._failed_at

    async def _finished(self, task: asyncio.Task[tuple[bool, str]]) -> None:
        self._task = None
        try:
            ok, detail = task.result()
        except Exception as exc:
            ok, detail = False, f"the probe could not run: {exc}"
        if ok:
            write_stamp(self.cfg, self.current)
            self.failed = ""
            self.emit(
                console.line(f"Claude Code {self.current} passed the sandbox probe; new sessions start again")
            )
            return
        if self._attempts < PROBE_ATTEMPTS:
            self.emit(console.line(f"the sandbox probe failed ({detail[:200]}); running it once more"))
            self._start_probe()
            return
        self.failed = detail[:300] or "unknown failure"
        self._failed_at = utcnow().isoformat()
        self._next_check = self.clock() + RETRY_FAILED_SECONDS
        self.emit(console.line(f"New sessions wait: {self.hold}"))
        await self.alerts.send(
            f"probe:{self.current}",
            "Claude Code failed its sandbox probe",
            f"Claude Code {self.current} failed the sandbox probe ({self.failed}). New sessions wait; "
            "run `delivery doctor --claude-probe` on this machine for details.",
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None


__all__ = ["STAMP", "VersionGuard", "proven", "read_stamp", "run_probe", "write_stamp"]
