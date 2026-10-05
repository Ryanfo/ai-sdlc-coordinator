"""Per-run mutable resources (ports), and the machine's room for more sessions.

There is no pool size. A port comes from the operating system; exhaustion surfaces as a
real resource error for the affected run only. New sessions wait while the disk is nearly full
or macOS reports critical memory pressure (``short_of_room``), and the Mac is kept from idle
sleep while sessions run (``KeepAwake``).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT_NAMES = ("app", "e2e", "aux")


class ResourceExhausted(Exception):
    pass


@dataclass
class PortRegistry:
    _owner: dict[int, str] = field(default_factory=dict)

    def allocate(self, run_id: str, names: tuple[str, ...] = DEFAULT_PORT_NAMES) -> dict[str, int]:
        ports: dict[str, int] = {}
        attempts = 0
        while len(ports) < len(names):
            attempts += 1
            if attempts > 200:
                self.release(run_id)
                raise ResourceExhausted("could not obtain a free local port")
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("127.0.0.1", 0))
                    port = int(s.getsockname()[1])
            except OSError as exc:
                self.release(run_id)
                raise ResourceExhausted(f"cannot bind a local port: {exc}") from None
            if port in self._owner:
                continue
            self._owner[port] = run_id
            ports[names[len(ports)]] = port
        return ports

    def adopt(self, run_id: str, ports: dict[str, int]) -> None:
        for p in ports.values():
            self._owner[p] = run_id

    def release(self, run_id: str) -> None:
        for port in [p for p, owner in self._owner.items() if owner == run_id]:
            del self._owner[port]

    def owned_by(self, run_id: str) -> list[int]:
        return sorted(p for p, owner in self._owner.items() if owner == run_id)


def port_env(ports: dict[str, int]) -> dict[str, str]:
    env = {f"DELIVERY_PORT_{k.upper()}": str(v) for k, v in ports.items()}
    if "app" in ports:
        env["PORT"] = str(ports["app"])
    if "e2e" in ports:
        env["E2E_PORT"] = str(ports["e2e"])
    return env


# --------------------------------------------------------------------------- machine room


def memory_pressure_level() -> int | None:
    """macOS memory pressure: 1 normal, 2 warning, 4 critical. None where it cannot be read."""
    if sys.platform != "darwin":
        return None
    try:
        res = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return int(res.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def short_of_room(
    worktree_root: Path,
    min_free_disk_gb: int,
    watch_memory: bool,
    pressure: Callable[[], int | None] = memory_pressure_level,
) -> str | None:
    """Why new sessions should wait for now, if this machine is short of disk or memory.

    Not a session limit: any number of sessions run while there is room, and the ones running
    are never stopped. It only keeps new ones from making a full disk or a swapping Mac worse.
    """
    if min_free_disk_gb:
        where = _existing(worktree_root)
        free = shutil.disk_usage(where).free
        if free < min_free_disk_gb * 1024**3:
            return (
                f"only {free / 1024**3:.1f} GB is free on the disk holding {where} "
                f"(runtime.min_free_disk_gb = {min_free_disk_gb})"
            )
    if watch_memory and pressure() == MEMORY_CRITICAL:
        return "macOS reports critical memory pressure"
    return None


MEMORY_CRITICAL = 4


class KeepAwake:
    """Keep the Mac from idle sleep while sessions run (``caffeinate -i``).

    ``-w`` ties it to this process, so it never outlives a coordinator that stops or crashes.
    Closing the lid still sleeps the Mac.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled and sys.platform == "darwin" and shutil.which("caffeinate") is not None
        self.proc: subprocess.Popen[bytes] | None = None

    def update(self, busy: bool) -> None:
        if not self.enabled:
            return
        running = self.proc is not None and self.proc.poll() is None
        if busy and not running:
            try:
                self.proc = subprocess.Popen(
                    ["caffeinate", "-i", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                self.enabled = False
        elif not busy and running:
            self.stop()

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
