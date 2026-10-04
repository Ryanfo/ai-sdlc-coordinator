"""Run the app from a worktree so people can try a change.

With ``[preview]`` configured, the coordinator runs the app in two places:

* from a finished development session's worktree, while that session stays open (interactive
  sessions with keep_open). A dev server that reloads shows the changes asked for in the open
  session straight away. It stops when the development session closes;
* from the code-approved candidate while the ticket is in Acceptance review
  (delivery.acceptance). It stops when the ticket leaves Acceptance review.

Either way it runs ``preview.setup`` (by default ``checks.setup``), then ``preview.seed``, then
``preview.command`` in the worktree, in a tmux session of its own (``<ticket>-preview`` or
``<ticket>-acceptance``) on a port of its own. When the app answers, the browser opens on it.
Like the coordinator's checks, it runs outside Claude's sandbox with the minimal child
environment (no credentials). ``delivery preview <ticket>`` opens it again, or asks the
coordinator to restart it if it has stopped.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import sys
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from delivery import console
from delivery.models import Model, utcnow
from delivery.proc import base_child_env, run_process
from delivery.resources import ResourceExhausted
from delivery.tmux import TmuxError, session_name
from delivery.tmux import for_config as tmux_for

if TYPE_CHECKING:
    from delivery.runtime import Deps

PROCEDURE = "preview"  # the tmux session is <ticket>-preview (delivery attach <ticket> --procedure preview)
ACCEPTANCE = "acceptance"  # the acceptance review app: <ticket>-acceptance
APP_PROCEDURES = (PROCEDURE, ACCEPTANCE)
LOG = "preview.log"
SCRIPT = "preview.sh"
# Written by `delivery preview <ticket>` into the app's folder: start the app again.
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


class AppHost(Protocol):
    """Where an app runs from: a development session left open, or an acceptance review."""

    ticket_key: str
    session_dir: str  # holds the launcher script, the app's log and restart requests
    worktree: str
    preview: PreviewState | None


def port_owner(key: str, procedure: str = PROCEDURE) -> str:
    return f"{procedure}:{key}"


def expand(argv: list[str], port: int) -> list[str]:
    return [a.replace("{port}", str(port)) for a in argv]


def launcher(
    setup: list[str], command: list[str], worktree: Path, log: Path, seed: Sequence[str] = ()
) -> str:
    """A shell script that runs setup, the seed, then the app, showing output in tmux and keeping
    it in ``log``. A failing setup or seed never starts the app."""
    run = shlex.join(command)
    steps = [shlex.join(s) for s in (setup, list(seed)) if s]
    body = " && ".join([*steps, f"exec {run}"])
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
    """Starts, watches and stops apps: one per host (``procedure`` names which kind)."""

    def __init__(
        self,
        deps: Deps,
        emit: Callable[[str], None],
        opener: Callable[[str], Awaitable[str | None]] = open_url,
        procedure: str = PROCEDURE,
    ) -> None:
        self.deps = deps
        self.cfg = deps.cfg
        self.emit = emit
        self.opener = opener
        self.procedure = procedure
        self.tmux = tmux_for(self.cfg.claude.interactive, self.cfg.runtime.state_dir)
        # Hosts whose app could not be started (not retried until asked again).
        self.gave_up: set[str] = set()

    async def tick(
        self,
        host: AppHost,
        can_start: Callable[[], bool],
        note: Callable[[str], Awaitable[None]] | None = None,
    ) -> bool:
        """Advance ``host``'s app; True when the host changed. The app starts once ``can_start``
        says so. ``note`` shows a line to the people watching (the open Claude session)."""
        restart = Path(host.session_dir) / RESTART
        asked = restart.exists()
        p = host.preview
        if asked or (p is None and host.ticket_key not in self.gave_up):
            if not Path(host.worktree).is_dir() or not can_start():
                return False  # a restart request waits until the app can start
            restart.unlink(missing_ok=True)
            self.gave_up.discard(host.ticket_key)
            await self.stop(host)
            host.preview = await self.start(host)
            if host.preview is None:
                self.gave_up.add(host.ticket_key)  # until `delivery preview` asks again
            return True
        if p is None or p.state == "stopped":
            return False
        self.deps.ports.adopt(port_owner(host.ticket_key, self.procedure), {"app": p.port})  # after a restart
        if not await self.tmux.alive(p.name):
            what = "stopped before it answered" if p.state == "starting" else "stopped"
            p.state = "stopped"
            self.deps.ports.release(port_owner(host.ticket_key, self.procedure))
            self.emit(
                console.preview_trouble(
                    self.cfg, host.ticket_key, f"The app {what}", p.log, log_tail(p.log), self.procedure
                )
            )
            return True
        if p.state == "starting":
            if await asyncio.to_thread(answers, p.url):
                p.state, p.ready_at = "ready", utcnow()
                await self._open(host)
                self.emit(
                    console.preview_ready(
                        self.cfg, host.ticket_key, p.url, host.worktree, p.log, self.procedure
                    )
                )
                if note is not None:
                    await note(f"The app is running at {p.url}")
                return True
            waited = (utcnow() - p.started_at).total_seconds()
            if not p.slow_noted and waited > self.cfg.preview.ready_timeout_seconds:
                p.slow_noted = True
                self.emit(
                    console.preview_trouble(
                        self.cfg,
                        host.ticket_key,
                        f"The app has not answered at {p.url} after {int(waited)}s; still waiting",
                        p.log,
                        log_tail(p.log),
                        self.procedure,
                    )
                )
                return True
        return False

    async def start(self, host: AppHost) -> PreviewState | None:
        pc = self.cfg.preview
        key = host.ticket_key
        owner = port_owner(key, self.procedure)
        try:
            port = self.deps.ports.allocate(owner, ("app",))["app"]
        except ResourceExhausted as exc:
            self.emit(console.line(f"{key}: the app was not started: {exc}"))
            return None
        sdir = Path(host.session_dir)
        sdir.mkdir(parents=True, exist_ok=True)
        log = sdir / LOG
        setup = self.cfg.checks.setup if pc.setup is None else pc.setup
        script = sdir / SCRIPT
        script.write_text(
            launcher(
                expand(setup, port),
                expand(pc.command, port),
                Path(host.worktree),
                log,
                seed=expand(pc.seed, port),
            )
        )
        script.chmod(0o700)
        name = session_name(key, self.procedure)
        await self.tmux.kill(name)  # a leftover from before a restart
        # BROWSER=none stops dev servers opening a browser of their own; the coordinator opens it.
        env = base_child_env({"PORT": str(port), "DELIVERY_PORT_APP": str(port), "BROWSER": "none"})
        try:
            await self.tmux.start(name, Path(host.worktree), ["/bin/sh", str(script)], env)
        except TmuxError as exc:
            self.deps.ports.release(owner)
            self.emit(console.line(f"{key}: the app could not be started: {exc}"))
            return None
        url = pc.url.replace("{port}", str(port))
        where = "the development worktree" if self.procedure == PROCEDURE else "the approved candidate"
        self.emit(console.line(f"{key}: starting the app from {where} at {url}"))
        return PreviewState(name=name, port=port, url=url, started_at=utcnow(), log=str(log))

    async def stop(self, host: AppHost) -> None:
        self.gave_up.discard(host.ticket_key)
        if host.preview is None:
            return
        await self.tmux.kill(host.preview.name)
        self.deps.ports.release(port_owner(host.ticket_key, self.procedure))
        host.preview.state = "stopped"

    async def _open(self, host: AppHost) -> None:
        if host.preview is None or not self.cfg.preview.open_browser:
            return
        problem = await self.opener(host.preview.url)
        if problem:
            self.emit(console.line(f"{host.ticket_key}: could not open {host.preview.url}: {problem}"))
