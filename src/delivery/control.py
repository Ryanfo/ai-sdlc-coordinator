"""Local control channel between operator commands and the running supervisor.

A Unix domain socket (mode 0600, inside the 0700 state directory where the path fits)
carries one JSON request and one JSON response per connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
MAX_SOCKET_PATH = 100


def socket_path(state_dir: Path, identity_key: str) -> Path:
    preferred = state_dir / "supervisor" / identity_key / "control.sock"
    if len(str(preferred)) <= MAX_SOCKET_PATH:
        return preferred
    short = hashlib.sha256(str(preferred).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"delivery-{os.getuid()}-{short}.sock"


class ControlServer:
    def __init__(self, path: Path, handler: Handler) -> None:
        self.path = path
        self.handler = handler
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            request = json.loads(line or b"{}")
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            response = await self.handler(request)
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        writer.write((json.dumps(response, default=str) + "\n").encode())
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


async def send(path: Path, request: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), timeout=5)
    writer.write((json.dumps(request) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    writer.close()
    data = json.loads(line or b"{}")
    return data if isinstance(data, dict) else {"ok": False, "error": "malformed response"}


def supervisor_reachable(path: Path | None) -> bool:
    return bool(path) and Path(str(path)).exists()
