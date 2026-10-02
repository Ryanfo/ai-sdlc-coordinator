"""Figma REST adapter (read-only): file versions, node data and frame renders.

Used only by the coordinator. Claude's sessions never receive the token or network access;
they read the snapshots this adapter writes into the run's inputs directory.

* ``X-Figma-Token`` personal access token with ``file_content:read`` (and ``current_user:read``
  for ``delivery credentials check figma``).
* Reads retry on 429/5xx with ``Retry-After``. Render URLs are pre-signed storage URLs: they
  are downloaded with a separate client that never carries the token.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any, Protocol

import httpx

from delivery.ports import AuthError, IntegrationError, NotFound

API = "https://api.figma.com"
READ_ATTEMPTS = 4


class FigmaPort(Protocol):
    async def me(self) -> dict[str, Any]: ...
    async def file_info(self, file_key: str) -> dict[str, Any]: ...
    async def nodes(self, file_key: str, ids: list[str], version: str) -> dict[str, Any]: ...
    async def render(
        self, file_key: str, ids: list[str], version: str, scale: float
    ) -> dict[str, str | None]: ...
    async def download(self, url: str, dest: Path, max_bytes: int) -> int: ...
    async def close(self) -> None: ...


class FigmaClient:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.http = httpx.AsyncClient(
            base_url=API,
            headers={"X-Figma-Token": token, "User-Agent": "delivery-coordinator"},
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=transport,
        )
        # Pre-signed render URLs: no token, no cookies.
        self.files = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0), transport=transport)

    async def close(self) -> None:
        await self.http.aclose()
        await self.files.aclose()

    async def _get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        delay = 2.0
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                r = await self.http.get(url, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == READ_ATTEMPTS:
                    raise IntegrationError(f"Figma unreachable: {exc}", retryable=True) from None
            else:
                if r.status_code < 400:
                    data: dict[str, Any] = r.json()
                    return data
                detail = r.text[:300]
                if r.status_code in (401, 403):
                    raise AuthError(
                        f"Figma {r.status_code}: token rejected or lacks a scope: {detail}",
                        status=r.status_code,
                    )
                if r.status_code == 404:
                    raise NotFound(f"Figma 404: file or node not found (or no access): {detail}", status=404)
                if r.status_code not in (429, 500, 502, 503, 504) or attempt == READ_ATTEMPTS:
                    raise IntegrationError(
                        f"Figma {r.status_code}: {detail}",
                        status=r.status_code,
                        retryable=r.status_code in (429, 500, 502, 503, 504),
                    )
                retry_after = r.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, min(float(retry_after), 120.0))
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 60.0)
        raise IntegrationError("unreachable")  # pragma: no cover

    async def me(self) -> dict[str, Any]:
        return await self._get("/v1/me")

    async def file_info(self, file_key: str) -> dict[str, Any]:
        """Name, lastModified and the current ``version`` (pages only, no layer tree)."""
        return await self._get(f"/v1/files/{file_key}", {"depth": "1"})

    async def nodes(self, file_key: str, ids: list[str], version: str) -> dict[str, Any]:
        return await self._get(f"/v1/files/{file_key}/nodes", {"ids": ",".join(ids), "version": version})

    async def render(
        self, file_key: str, ids: list[str], version: str, scale: float
    ) -> dict[str, str | None]:
        data = await self._get(
            f"/v1/images/{file_key}",
            {"ids": ",".join(ids), "version": version, "format": "png", "scale": f"{scale:g}"},
        )
        if data.get("err"):
            raise IntegrationError(f"Figma render failed: {data['err']}")
        images: dict[str, str | None] = data.get("images") or {}
        return images

    async def download(self, url: str, dest: Path, max_bytes: int) -> int:
        tmp = dest.with_name(dest.name + ".part")
        size = 0
        try:
            async with self.files.stream("GET", url, follow_redirects=True) as r:
                if r.status_code >= 400:
                    raise IntegrationError(f"Figma render download {r.status_code}", status=r.status_code)
                with tmp.open("wb") as fh:
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise IntegrationError(f"Figma render exceeds {max_bytes} bytes")
                        fh.write(chunk)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            tmp.unlink(missing_ok=True)
            raise IntegrationError(f"Figma render download failed: {exc}", retryable=True) from None
        except IntegrationError:
            tmp.unlink(missing_ok=True)
            raise
        tmp.chmod(0o600)
        tmp.replace(dest)
        return size
