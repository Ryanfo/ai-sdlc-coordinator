"""Async subprocess helpers. Every child gets its own process group.

Children are started with ``start_new_session=True`` so that stopping one ticket's
session (or its test commands) signals only that group, never another ticket's.
Arguments are always argument arrays; nothing is interpolated into a shell.
"""

from __future__ import annotations

import asyncio
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
    """Run a command in its own process group, capturing output (optionally to files)."""
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
    timed_out = False
    cancelled = False
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(stdin_data), timeout=timeout)
    except TimeoutError:
        timed_out = True
        await terminate_group(proc)
        out_b, err_b = b"", b""
    except asyncio.CancelledError:
        cancelled = True
        await asyncio.shield(terminate_group(proc))
        raise
    finally:
        if not cancelled and proc.returncode is None:
            await terminate_group(proc)
    stdout = out_b[:max_capture].decode("utf-8", errors="replace")
    stderr = redact(err_b[:max_capture].decode("utf-8", errors="replace"))
    if stdout_path:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(redact(stdout))
        os.chmod(stdout_path, 0o600)
    if stderr_path:
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.write_text(stderr)
        os.chmod(stderr_path, 0o600)
    return ProcResult(
        tuple(argv),
        proc.returncode if proc.returncode is not None else -1,
        stdout,
        stderr,
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
