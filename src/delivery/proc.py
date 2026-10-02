"""Async subprocess helpers. Every child gets its own process group.

Children are started with ``start_new_session=True`` so that stopping one ticket's
session (or its test commands) signals only that group, never another ticket's.
Arguments are always argument arrays; nothing is interpolated into a shell.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from delivery.redaction import redact

GRACE_SECONDS = 10.0


@dataclass(frozen=True)
class ProcResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    cancelled: bool = False


class ProcessStartError(Exception):
    pass


def process_start_marker(pid: int) -> str | None:
    """Best-effort process start time, used to confirm a PID still refers to our child."""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    marker = out.stdout.strip()
    return marker or None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


async def terminate_group(proc: asyncio.subprocess.Process, grace: float = GRACE_SECONDS) -> None:
    """SIGTERM the child's group, wait, then SIGKILL. Only touches this child's group."""
    if proc.returncode is not None:
        return
    pgid = proc.pid  # start_new_session makes the child its own group leader
    signal_group(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except TimeoutError:
        signal_group(pgid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=grace)
    # Reap any stragglers left in the group after the leader exits.
    signal_group(pgid, signal.SIGKILL)


class _Capture:
    """Collects a stream in memory (head and tail kept when over the cap, never the middle)
    while writing it line by line to a file, so logs exist while a child runs and survive a
    timeout or stop."""

    def __init__(self, path: Path | None, cap: int, redact_memory: bool) -> None:
        self.cap = cap
        self.redact_memory = redact_memory
        self.head: list[bytes] = []
        self.head_bytes = 0
        self.tail: collections.deque[bytes] = collections.deque()
        self.tail_bytes = 0
        self.dropped = False
        self.fh = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            self.fh = os.fdopen(fd, "wb")

    def _keep(self, line: bytes) -> None:
        if self.head_bytes + len(line) <= self.cap // 2:
            self.head.append(line)
            self.head_bytes += len(line)
            return
        self.tail.append(line)
        self.tail_bytes += len(line)
        while self.tail_bytes > self.cap // 2 and len(self.tail) > 1:
            self.tail_bytes -= len(self.tail.popleft())
            self.dropped = True

    def line(self, line: bytes) -> None:
        self._keep(line)
        if self.fh:
            self.fh.write(redact(line.decode("utf-8", errors="replace")).encode())
            self.fh.flush()

    async def pump(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        pending = b""
        while chunk := await stream.read(65536):
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for ln in lines:
                self.line(ln + b"\n")
        if pending:
            self.line(pending)

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None

    def text(self) -> str:
        middle = [b"...[output truncated]...\n"] if self.dropped else []
        raw = b"".join([*self.head, *middle, *self.tail]).decode("utf-8", errors="replace")
        return redact(raw) if self.redact_memory else raw


async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    stdin_data: bytes | None = None,
    on_start: Callable[[asyncio.subprocess.Process], None] | None = None,
    max_capture: int = 2_000_000,
) -> ProcResult:
    """Run a command in its own process group, capturing output (and streaming it to files)."""
    start = time.monotonic()
    if not cwd.is_dir():
        raise ProcessStartError(f"working directory {cwd} does not exist")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        raise ProcessStartError(f"cannot start {argv[0]!r}: {exc.strerror or exc}") from None
    if on_start:
        on_start(proc)
    out = _Capture(stdout_path, max_capture, redact_memory=False)
    err = _Capture(stderr_path, max_capture, redact_memory=True)
    timed_out = False
    cancelled = False

    async def feed() -> None:
        if stdin_data is not None and proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
            proc.stdin.close()

    tasks = [
        asyncio.create_task(feed()),
        asyncio.create_task(proc.wait()),
        asyncio.create_task(out.pump(proc.stdout)),
        asyncio.create_task(err.pump(proc.stderr)),
    ]
    try:
        # asyncio.wait never cancels: on timeout the readers keep draining what is left.
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            timed_out = True
            await terminate_group(proc)
            _, pending = await asyncio.wait(pending, timeout=5)
            for t in pending:
                t.cancel()
    except asyncio.CancelledError:
        cancelled = True
        await asyncio.shield(terminate_group(proc))
        for t in tasks:
            t.cancel()
        raise
    finally:
        if not cancelled and proc.returncode is None:
            await terminate_group(proc)
        out.close()
        err.close()
    return ProcResult(
        tuple(argv),
        proc.returncode if proc.returncode is not None else -1,
        out.text(),
        err.text(),
        time.monotonic() - start,
        timed_out=timed_out,
    )


def base_child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for children. Credentials are never forwarded."""
    # No SSH agent socket, tokens or provider variables: children must not inherit write
    # credentials. The coordinator's own Git commands add what they need separately.
    keep = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "SHELL", "TERM", "TZ")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("LANG", "en_US.UTF-8")
    if extra:
        env.update(extra)
    return env
