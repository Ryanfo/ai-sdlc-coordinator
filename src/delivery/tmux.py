"""A private tmux server that hosts interactive Claude sessions (see delivery.interactive).

The coordinator owns the server (``tmux -L delivery``) and every session in it: it starts each
one with an exact environment (``env -i``), watches it and stops it. People attach to watch or
type; closing a terminal window only detaches it and the session keeps running. The server
uses its own configuration, never ``~/.tmux.conf``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from delivery.proc import ProcessStartError, ProcResult, base_child_env, pid_alive, run_process

if TYPE_CHECKING:
    from delivery.config import InteractiveConfig

SOCKET = "delivery"

CONFIG = """\
set -g history-limit 50000
set -g mouse on
set -g default-terminal "screen-256color"
set -ga terminal-overrides ",*256col*:Tc"
set -g set-titles on
set -g set-titles-string "#S"
set -g status-style "bg=colour236,fg=colour250"
set -g status-left-length 60
set -g status-left " #S "
set -g status-right-length 70
set -g status-right " detach: Ctrl-b d   scroll: mouse wheel (q to leave) "
set -g display-time 6000
"""


class TmuxError(Exception):
    pass


def session_name(ticket: str, procedure: str) -> str:
    """tmux session names cannot contain ``.`` or ``:``; keep them readable."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", f"{ticket}-{procedure}")


def for_config(ic: InteractiveConfig, state_dir: Path | None = None) -> Tmux:
    """The coordinator's tmux server for this configuration."""
    return Tmux(ic.tmux, state_dir / "tmux.conf" if state_dir else None, ic.socket)


class Tmux:
    def __init__(self, executable: str = "tmux", config: Path | None = None, socket: str = SOCKET) -> None:
        self.executable = executable
        self.config = config
        self.socket = socket

    def path(self) -> str | None:
        return shutil.which(self.executable)

    def available(self) -> bool:
        return self.path() is not None

    def _argv(self, *args: str) -> list[str]:
        argv = [self.path() or self.executable, "-L", self.socket]
        if self.config is not None:
            argv += ["-f", str(self.config)]
        return [*argv, *args]

    async def _run(self, *args: str, check: bool = True) -> ProcResult:
        try:
            # The server inherits this environment when it starts: keep it minimal.
            res = await run_process(self._argv(*args), cwd=Path.home(), env=base_child_env(), timeout=15)
        except ProcessStartError as exc:
            raise TmuxError(str(exc)) from None
        if check and res.returncode != 0:
            raise TmuxError(f"tmux {args[0]} failed: {(res.stderr or res.stdout).strip()[:300]}")
        return res

    def _write_config(self) -> None:
        if self.config is not None and (not self.config.exists() or self.config.read_text() != CONFIG):
            self.config.parent.mkdir(parents=True, exist_ok=True)
            self.config.write_text(CONFIG)

    async def start(
        self,
        name: str,
        cwd: Path,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        width: int = 200,
        height: int = 50,
    ) -> int:
        """Start ``argv`` in a new detached session with exactly ``env``. Returns its pid."""
        self._write_config()
        assignments = [f"{k}={v}" for k, v in env.items()]
        await self._run(
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            str(width),
            "-y",
            str(height),
            "-c",
            str(cwd),
            "--",
            "/usr/bin/env",
            "-i",
            *assignments,
            *argv,
        )
        pid = await self.pane_pid(name)
        if pid is None:
            raise TmuxError(f"session {name} ended immediately")
        return pid

    async def alive(self, name: str) -> bool:
        try:
            res = await self._run("has-session", "-t", f"={name}", check=False)
        except TmuxError:
            return False
        return res.returncode == 0

    async def pane_pid(self, name: str) -> int | None:
        try:
            res = await self._run("display-message", "-p", "-t", f"={name}:", "#{pane_pid}", check=False)
        except TmuxError:
            return None
        text = res.stdout.strip()
        return int(text) if res.returncode == 0 and text.isdigit() else None

    async def kill(self, name: str, wait: float = 10.0) -> None:
        """End the session and wait (up to ``wait`` seconds) for its process to exit.

        Claude Code writes its last transcript entries while exiting, so anything that reads or
        removes the transcript has to wait for that.
        """
        pid = await self.pane_pid(name)
        with contextlib.suppress(TmuxError):
            await self._run("kill-session", "-t", f"={name}", check=False)
        if pid is None:
            return
        for _ in range(int(wait * 10)):
            if not pid_alive(pid):
                return
            await asyncio.sleep(0.1)

    async def sessions(self) -> list[str]:
        try:
            res = await self._run("list-sessions", "-F", "#{session_name}", check=False)
        except TmuxError:
            return []
        return [s for s in res.stdout.splitlines() if s] if res.returncode == 0 else []

    async def message(self, name: str, text: str) -> None:
        """Show a short status-line message to anyone attached (ignored if nobody is)."""
        with contextlib.suppress(TmuxError):
            clients = await self._run("list-clients", "-t", f"={name}", "-F", "#{client_name}", check=False)
            for client in clients.stdout.splitlines():
                await self._run("display-message", "-c", client, text[:200], check=False)

    async def send_keys(self, name: str, *keys: str) -> None:
        """Press keys in the session (tmux key names such as Down and Enter)."""
        with contextlib.suppress(TmuxError):
            await self._run("send-keys", "-t", f"={name}:", *keys, check=False)

    async def capture(self, name: str, lines: int = 40) -> str:
        try:
            res = await self._run("capture-pane", "-p", "-t", f"={name}:", "-S", f"-{lines}", check=False)
        except TmuxError:
            return ""
        return res.stdout if res.returncode == 0 else ""

    def attach_argv(self, name: str) -> list[str]:
        return [self.path() or self.executable, "-L", self.socket, "attach-session", "-t", f"={name}"]
