"""Per-run mutable resources: ports today, explicit and owned by exactly one run.

There is no pool size. A port comes from the operating system; exhaustion surfaces as a
real resource error for the affected run only.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field

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
