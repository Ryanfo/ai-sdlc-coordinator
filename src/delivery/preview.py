"""Run the app from a finished development session's worktree so the developer can try it.

With ``[preview]`` configured and interactive sessions kept open, once a development run has
ended and its session stays open, the coordinator runs the app there: ``preview.setup`` (by
default ``checks.setup``), then ``preview.command``, in the session's worktree, in its own tmux
session (``<ticket>-preview``) on a port of its own. When the app answers, the browser opens on
it. A dev server that reloads shows the changes asked for in the open session straight away.

Like the coordinator's checks, it runs outside Claude's sandbox with the minimal child
environment (no credentials). It stops when the development session closes. ``delivery
preview <ticket>`` opens it again, or asks the coordinator to restart it if it has stopped.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import sys
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from delivery import console
from delivery.models import Model, utcnow
from delivery.proc import base_child_env, run_process
from delivery.resources import ResourceExhausted
from delivery.tmux import TmuxError, session_name
from delivery.tmux import for_config as tmux_for

if TYPE_CHECKING:
    from delivery.open_sessions import OpenRecord
    from delivery.runtime import Deps

PROCEDURE = "preview"  # the tmux session is <ticket>-preview (delivery attach <ticket> --procedure preview)
LOG = "preview.log"
SCRIPT = "preview.sh"
# Written by `delivery preview <ticket>` into the open session's folder: start the app again.
RESTART = "preview-restart"


class PreviewState(Model):
    name: str
    port: int
    url: str
    started_at: datetime
    state: Literal["starting", "ready", "stopped"] = "starting"
    ready_at: datetime | None = None
    slow_noted: bool = False
    log: str = ""


def port_owner(key: str) -> str:
    return f"preview:{key}"


def expand(argv: list[str], port: int) -> list[str]:
    return [a.replace("{port}", str(port)) for a in argv]


def launcher(setup: list[str], command: list[str], worktree: Path, log: Path) -> str:
    """A shell script that runs setup then the app, showing output in tmux and keeping it in ``log``."""
    run = shlex.join(command)
    body = f"{shlex.join(setup)} && exec {run}" if setup else f"exec {run}"
    return (
        "#!/bin/sh\n"
        f"cd {shlex.quote(str(worktree))} || exit 1\n"
        f"{{ {body}; }} 2>&1 | tee -a {shlex.quote(str(log))}\n"
    )


def answers(url: str, timeout: float = 2.0) -> bool:
    """True when something answers HTTP at ``url`` (any status: the app is up)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


async def open_url(url: str) -> str | None:
    """Open ``url`` in the default browser. Returns a problem, if any."""
    if sys.platform == "darwin":
        argv = ["open", url]
    elif shutil.which("xdg-open"):
        argv = ["xdg-open", url]
    else:
        return "no way to open a browser on this system"
    extra = {k: os.environ[k] for k in ("DISPLAY", "WAYLAND_DISPLAY") if k in os.environ}
    res = await run_process(argv, cwd=Path.home(), env=base_child_env(extra), timeout=20)
    if res.returncode != 0:
        return (res.stderr or res.stdout).strip()[:300] or f"{argv[0]} exited {res.returncode}"
    return None


def log_tail(path: str, lines: int = 12) -> list[str]:
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    return [ln.rstrip()[: console.WIDTH - 2] for ln in text.splitlines() if ln.strip()][-lines:]


class Previews:
    """Starts, watches and stops the app previews of open development sessions."""

    def __init__(
        self,
        deps: Deps,
        emit: Callable[[str], None],
        opener: Callable[[str], Awaitable[str | None]] = open_url,
    ) -> None:
        self.deps = deps
        self.cfg = deps.cfg
        self.emit = emit
        self.opener = opener
        self.tmux = tmux_for(self.cfg.claude.interactive, self.cfg.runtime.state_dir)
        # Sessions whose app could not be started (not retried until asked again).
        self.gave_up: set[str] = set()

    async def tick(self, rec: OpenRecord, run_ended: Callable[[], bool]) -> bool:
        """Advance ``rec``'s preview; True when the record changed. The app starts once the run
        that opened the session has ended (``run_ended``)."""
        restart = Path(rec.session_dir) / RESTART
        asked = restart.exists()
        p = rec.preview
        if asked or (p is None and rec.name not in self.gave_up):
            if not Path(rec.worktree).is_dir() or not run_ended():
                return False  # a restart request waits until the app can start
            restart.unlink(missing_ok=True)
            self.gave_up.discard(rec.name)
            await self.stop(rec)
            rec.preview = await self.start(rec)
            if rec.preview is None:
                self.gave_up.add(rec.name)  # until `delivery preview` asks again
            return True
        if p is None or p.state == "stopped":
            return False
        self.deps.ports.adopt(port_owner(rec.ticket_key), {"app": p.port})  # after a restart
        if not await self.tmux.alive(p.name):
            what = "stopped before it answered" if p.state == "starting" else "stopped"
            p.state = "stopped"
            self.deps.ports.release(port_owner(rec.ticket_key))
            self.emit(
                console.preview_trouble(self.cfg, rec.ticket_key, f"The app {what}", p.log, log_tail(p.log))
            )
            return True
        if p.state == "starting":
            if await asyncio.to_thread(answers, p.url):
                p.state, p.ready_at = "ready", utcnow()
                await self._open(rec)
                self.emit(console.preview_ready(self.cfg, rec.ticket_key, p.url, rec.worktree, p.log))
                await self.tmux.message(rec.name, f"The app is running at {p.url}")
                return True
            waited = (utcnow() - p.started_at).total_seconds()
            if not p.slow_noted and waited > self.cfg.preview.ready_timeout_seconds:
                p.slow_noted = True
                self.emit(
                    console.preview_trouble(
                        self.cfg,
                        rec.ticket_key,
                        f"The app has not answered at {p.url} after {int(waited)}s; still waiting",
                        p.log,
                        log_tail(p.log),
                    )
                )
                return True
        return False

    async def start(self, rec: OpenRecord) -> PreviewState | None:
        pc = self.cfg.preview
        key = rec.ticket_key
        try:
            port = self.deps.ports.allocate(port_owner(key), ("app",))["app"]
        except ResourceExhausted as exc:
            self.emit(console.line(f"{key}: the app was not started: {exc}"))
            return None
        sdir = Path(rec.session_dir)
        log = sdir / LOG
        setup = self.cfg.checks.setup if pc.setup is None else pc.setup
        script = sdir / SCRIPT
        script.write_text(launcher(expand(setup, port), expand(pc.command, port), Path(rec.worktree), log))
        script.chmod(0o700)
        name = session_name(key, PROCEDURE)
        await self.tmux.kill(name)  # a leftover from before a restart
        # BROWSER=none stops dev servers opening a browser of their own; the coordinator opens it.
        env = base_child_env({"PORT": str(port), "DELIVERY_PORT_APP": str(port), "BROWSER": "none"})
        try:
            await self.tmux.start(name, Path(rec.worktree), ["/bin/sh", str(script)], env)
        except TmuxError as exc:
            self.deps.ports.release(port_owner(key))
            self.emit(console.line(f"{key}: the app could not be started: {exc}"))
            return None
        url = pc.url.replace("{port}", str(port))
        self.emit(console.line(f"{key}: starting the app from the development worktree at {url}"))
        return PreviewState(name=name, port=port, url=url, started_at=utcnow(), log=str(log))

    async def stop(self, rec: OpenRecord) -> None:
        self.gave_up.discard(rec.name)
        if rec.preview is None:
            return
        await self.tmux.kill(rec.preview.name)
        self.deps.ports.release(port_owner(rec.ticket_key))
        rec.preview.state = "stopped"

    async def _open(self, rec: OpenRecord) -> None:
        if rec.preview is None or not self.cfg.preview.open_browser:
            return
        problem = await self.opener(rec.preview.url)
        if problem:
            self.emit(console.line(f"{rec.ticket_key}: could not open {rec.preview.url}: {problem}"))
