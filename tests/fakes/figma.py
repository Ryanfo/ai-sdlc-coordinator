"""In-memory Figma: versioned files, node trees and PNG renders."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from delivery.ports import AuthError, IntegrationError, NotFound

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def text(node_id: str, chars: str, y: int = 0, size: int = 16, colour: float = 0.1) -> dict[str, Any]:
    return {
        "id": node_id,
        "name": chars[:20],
        "type": "TEXT",
        "characters": chars,
        "absoluteBoundingBox": {"x": 24, "y": y, "width": 200, "height": 24},
        "style": {"fontFamily": "Inter", "fontWeight": 600, "fontSize": size, "lineHeightPx": size * 1.5},
        "fills": [{"type": "SOLID", "color": {"r": colour, "g": colour, "b": colour, "a": 1}}],
    }


def frame(node_id: str, name: str, *children: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node_id,
        "name": name,
        "type": "FRAME",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 390, "height": 844},
        "layoutMode": "VERTICAL",
        "itemSpacing": 16,
        "paddingTop": 24,
        "paddingRight": 24,
        "paddingBottom": 24,
        "paddingLeft": 24,
        "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1, "a": 1}}],
        "children": list(children),
    }


class FakeFigma:
    def __init__(self) -> None:
        # file key -> list of (version, {node_id: document})
        self.files: dict[str, list[tuple[str, dict[str, dict[str, Any]]]]] = {}
        self.names: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.denied: set[str] = set()
        self.downloads: list[str] = []

    def publish(self, key: str, nodes: dict[str, dict[str, Any]], name: str = "App designs") -> str:
        versions = self.files.setdefault(key, [])
        version = str(1000 + len(versions))
        versions.append((version, copy.deepcopy(nodes)))
        self.names[key] = name
        return version

    def _file(self, key: str, version: str | None = None) -> tuple[str, dict[str, dict[str, Any]]]:
        if key in self.denied:
            raise AuthError("Figma 403: no access", status=403)
        if key not in self.files:
            raise NotFound("Figma 404", status=404)
        if version is None:
            return self.files[key][-1]
        for v, nodes in self.files[key]:
            if v == version:
                return v, nodes
        raise NotFound(f"Figma 404: no version {version}", status=404)

    async def me(self) -> dict[str, Any]:
        return {"handle": "designer", "email": "designer@example.com"}

    async def file_info(self, file_key: str) -> dict[str, Any]:
        self.calls.append(("file_info", file_key))
        version, _ = self._file(file_key)
        return {"name": self.names[file_key], "version": version, "lastModified": "2026-10-02T09:00:00Z"}

    async def nodes(self, file_key: str, ids: list[str], version: str) -> dict[str, Any]:
        self.calls.append(("nodes", f"{file_key}@{version}"))
        _, nodes = self._file(file_key, version)
        return {"nodes": {i: ({"document": nodes[i], "components": {}} if i in nodes else None) for i in ids}}

    async def render(
        self, file_key: str, ids: list[str], version: str, scale: float
    ) -> dict[str, str | None]:
        self.calls.append(("render", f"{file_key}@{version}"))
        return {i: f"https://render.example/{file_key}/{version}/{i}.png" for i in ids}

    async def download(self, url: str, dest: Path, max_bytes: int) -> int:
        self.downloads.append(url)
        if len(PNG) > max_bytes:
            raise IntegrationError("too large")
        dest.write_bytes(PNG)
        return len(PNG)

    async def close(self) -> None:
        return None
