"""The coordinator running in the background, in a tmux session of its own.

`coordinator` starts `delivery run` in a private tmux server (``tmux -L <socket>-coordinator``,
session ``coordinator``) and attaches this terminal to it. Closing the window only detaches it:
the coordinator keeps running. `coordinator attach` opens it again, `coordinator stop` stops it
cleanly (as Ctrl-C does: running sessions are saved and resume on the next start) and
`coordinator restart` stops and starts it, which is how code changes are picked up.

The server is started from the caller's own environment, so the coordinator has the same PATH,
Keychain access and Git credentials as a terminal it was started in. The server hosts only this
one session and exits with it. Claude sessions keep their own server (delivery.tmux), which
never receives that environment.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from delivery.config import Config
from delivery.control import send, socket_path
from delivery.journal import JournalCorrupt, JournalStore, SupervisorRecord
from delivery.logfile import log_path
from delivery.ownership import supervisor_lock
from delivery.proc import pid_alive
from delivery.tmux import CONFIG, Tmux

SESSION = "coordinator"
START_WAIT_SECONDS = 30.0
# Shutdown gives running sessions up to 60 seconds to checkpoint; allow for that.
STOP_WAIT_SECONDS = 120.0

TMUX_CONFIG = (
    CONFIG.replace(
        'set -g status-right " detach: Ctrl-b d   scroll: mouse wheel (q to leave) "',
        'set -g status-right " leave it running: Ctrl-b d   stop it: Ctrl-c   scroll: mouse wheel "',
    )
    + 'set -g status-left " delivery coordinator "\n'
)


def server(cfg: Config) -> Tmux:
    ic = cfg.claude.interactive
    return Tmux(
        ic.tmux,
        cfg.runtime.state_dir / "coordinator-tmux.conf",
        f"{ic.socket}-coordinator",
        TMUX_CONFIG,
    )


@dataclass
class State:
    running: bool
    pid: int | None
    in_background: bool
    record: SupervisorRecord | None

    @property
    def stopped_unexpectedly(self) -> bool:
        r = self.record
        return not self.running and r is not None and r.started_at is not None and r.stopped_at is None

    @property
    def since(self) -> str:
        r = self.record
        if r is None or r.started_at is None:
            return "?"
        return r.started_at.astimezone().strftime("%a %d %b %H:%M")


def _session_alive(tmux: Tmux) -> bool:
    if not tmux.available():
        return False
    res = subprocess.run(
        tmux.argv("has-session", "-t", f"={SESSION}"), capture_output=True, text=True, check=False
    )
    return res.returncode == 0


def holder_pid(cfg: Config) -> int | None:
    """The running supervisor's pid, from its lock (truncated on a clean stop)."""
    raw = supervisor_lock(cfg).read_holder().get("pid")
    pid = raw if isinstance(raw, int) else None
    return pid if pid and pid_alive(pid) else None


def state(cfg: Config) -> State:
    store = JournalStore(cfg.runtime.state_dir, cfg.identity_key)
    try:
        record = store.load_supervisor()
    except JournalCorrupt:
        record = None
    pid = holder_pid(cfg)
    return State(pid is not None, pid, _session_alive(server(cfg)), record)


def attach_argv(cfg: Config) -> list[str]:
    return server(cfg).attach_argv(SESSION)


def own_env() -> dict[str, str]:
    """This terminal's environment, minus what would nest tmux inside the caller's tmux."""
    return {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}


def exec_attach(argv: list[str]) -> None:
    """Become a tmux client (works from inside another tmux too)."""
    os.execvpe(argv[0], argv, own_env())  # noqa: S606 (argument array, no shell)


def start(
    cfg: Config,
    *,
    verbose: bool = False,
    emit: Callable[[str], None] = print,
    command: list[str] | None = None,
) -> bool:
    """Start the supervisor in its tmux session and wait until it is running."""
    tmux = server(cfg)
    if cfg.source_path is None:
        raise ValueError("start needs a config loaded from a file")
    tmux.write_config()
    log_file = log_path(cfg.runtime.state_dir, cfg.identity_key)
    seen = log_file.stat().st_size if log_file.exists() else 0
    if command is None:
        command = [sys.executable, "-m", "delivery.cli", *(["-v"] if verbose else [])]
        command += ["run", "--config", str(cfg.source_path)]
    res = subprocess.run(
        tmux.argv("new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", "-c", str(Path.home()), "--")
        + command,
        env={**own_env(), "DELIVERY_BACKGROUND": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        emit(f"Could not start the coordinator in tmux: {(res.stderr or res.stdout).strip()[:300]}")
        return False
    sock = socket_path(cfg.runtime.state_dir, cfg.identity_key)
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline:
        if holder_pid(cfg) and sock.exists():
            return True
        if not _session_alive(tmux):
            break
        time.sleep(0.25)
    else:
        emit("The coordinator is still starting; `coordinator attach` shows it.")
        return True
    emit("The coordinator stopped as soon as it started:")
    for line in _new_log_lines(log_file, seen)[-25:]:
        emit(f"  {line}")
    emit("`coordinator --foreground` runs it in this terminal, where you see every message.")
    return False


def _new_log_lines(path: Path, offset: int) -> list[str]:
    if not path.exists():
        return []
    with path.open(errors="replace") as fh:
        if path.stat().st_size >= offset:
            fh.seek(offset)
        return [ln for ln in fh.read().splitlines() if ln.strip()]


def stop(cfg: Config, *, emit: Callable[[str], None] = print) -> bool:
    """Stop the running supervisor as Ctrl-C would, and wait until it has stopped."""
    pid = holder_pid(cfg)
    if pid is None:
        emit("The coordinator is not running.")
        return True
    sock = socket_path(cfg.runtime.state_dir, cfg.identity_key)
    asked, running = False, 0
    if sock.exists():
        try:
            reply = asyncio.run(send(sock, {"cmd": "shutdown"}, timeout=10))
            asked, running = bool(reply.get("ok")), int(reply.get("running_sessions") or 0)
        except (OSError, TimeoutError, ValueError):
            asked = False
    if not asked:
        os.kill(pid, signal.SIGTERM)
    saving = f"; saving {running} running session{'s' if running != 1 else ''} to resume on the next start"
    emit(f"Stopping the coordinator{saving if running else ''}.")
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            emit("Stopped.")
            return True
        time.sleep(0.25)
    emit(
        f"The coordinator (pid {pid}) has not stopped after {int(STOP_WAIT_SECONDS)}s; "
        "`coordinator attach` shows why."
    )
    return False


def describe(cfg: Config, st: State) -> str:
    if st.running and st.in_background:
        return f"running in the background since {st.since} (pid {st.pid}); `coordinator attach` shows it"
    if st.running:
        return f"running in another terminal since {st.since} (pid {st.pid})"
    if st.stopped_unexpectedly:
        return (
            f"not running; it stopped unexpectedly (started {st.since}). "
            "`coordinator logs` shows its last messages"
        )
    when = (
        st.record.stopped_at.astimezone().strftime("%a %d %b %H:%M")
        if st.record and st.record.stopped_at
        else ""
    )
    return "not running" + (f" (stopped {when})" if when else "")


def code_changed_since(cfg: Config, st: State) -> datetime | None:
    """When this installation's code changed after the running supervisor loaded it."""
    from delivery.codestamp import code_mtime

    r = st.record
    if not st.running or r is None or r.code_mtime is None:
        return None
    now = code_mtime()
    return datetime.fromtimestamp(now).astimezone() if now > r.code_mtime + 1 else None
