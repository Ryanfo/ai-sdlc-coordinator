"""Delivery plugin introspection: manifest, procedures, contract IDs and digest.

Contract IDs live only in each SKILL.md. The input envelope never contains them, so a
structured result that echoes the right contract ID shows the procedure actually loaded.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

PROCEDURES = (
    "refine-ticket",
    "plan-ticket",
    "implement-ticket",
    "review-ticket",
    "verify-ticket",
    "prepare-release",
    "amend-spec",
    "resolve-conflicts",
    "investigate-ticket",
    "resolve-blocker",
)
_CONTRACT = re.compile(r"^contract_id:\s*`?(?P<id>delivery\.[a-z-]+/v\d+)`?\s*$", re.M)


class PluginError(Exception):
    pass


@dataclass(frozen=True)
class PluginInfo:
    path: Path
    name: str
    version: str
    contracts: dict[str, str]
    digest: str


def plugin_digest(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = f.relative_to(path).as_posix()
        if "/." in f"/{rel}" and not rel.startswith(".claude-plugin/"):
            continue
        h.update(rel.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def load_plugin(path: Path) -> PluginInfo:
    manifest = path / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        raise PluginError(f"plugin manifest not found at {manifest}")
    try:
        data = json.loads(manifest.read_text())
    except json.JSONDecodeError as exc:
        raise PluginError(f"invalid plugin manifest: {exc}") from None
    if data.get("name") != "delivery":
        raise PluginError(f"plugin at {path} is {data.get('name')!r}, expected 'delivery'")
    contracts: dict[str, str] = {}
    for proc in PROCEDURES:
        skill = path / "skills" / proc / "SKILL.md"
        if not skill.is_file():
            raise PluginError(f"procedure {proc} missing ({skill})")
        m = _CONTRACT.search(skill.read_text())
        if not m:
            raise PluginError(f"procedure {proc} has no contract_id line")
        contracts[proc] = m.group("id")
    return PluginInfo(path, "delivery", str(data.get("version", "")), contracts, plugin_digest(path))
