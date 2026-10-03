"""Advisory overlap detection between in-flight tickets (any assignee).

Deterministic path/component/contract comparison of published change footprints. This
is not a lock and cannot see unpublished local work on other machines. File-path
separation never proves behavioural independence; reviewers are told so explicitly.
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from delivery.models import Footprint


class OverlapKind(StrEnum):
    SAME_PATH = "same_path"
    SAME_COMPONENT = "same_component"
    SHARED_CONTRACT = "shared_contract"
    DECLARED_DEPENDENCY = "declared_dependency"
    ACTUAL_PATH = "actual_path"


class Severity(StrEnum):
    """How prominently an overlap is flagged. Overlaps never pause work."""

    WARN = "warn"
    HIGH = "high"  # a shared interface, schema or migration, or a declared dependency

    @classmethod
    def _missing_(cls, value: object) -> Severity | None:
        return cls.HIGH if value == "block" else None  # records written when HIGH paused work


@dataclass(frozen=True)
class OverlapFinding:
    warning_id: str
    kind: OverlapKind
    severity: Severity
    ticket: str
    other: str
    other_assignee: str | None
    details: tuple[str, ...]
    revisions: dict[str, Any] = field(default_factory=dict)

    @property
    def tickets(self) -> tuple[str, str]:
        a, b = sorted((self.ticket, self.other))
        return a, b


def _norm(p: str) -> str:
    p = p.strip().lstrip("./").rstrip("/")
    return p


def _base(p: str) -> str:
    """Directory part before the first wildcard."""
    parts = []
    for seg in p.split("/"):
        if any(c in seg for c in "*?["):
            break
        parts.append(seg)
    return "/".join(parts)


def paths_overlap(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    ba, bb = _base(a), _base(b)
    wild_a, wild_b = ba != a, bb != b
    # A directory or glob covers anything beneath its fixed prefix.
    if wild_a and (b == ba or b.startswith(ba + "/") or (wild_b and bb.startswith(ba + "/"))):
        return True
    if wild_b and (a == bb or a.startswith(bb + "/") or (wild_a and ba.startswith(bb + "/"))):
        return True
    # Plain directory entries ("src/search") cover their contents.
    return b.startswith(a + "/") or a.startswith(b + "/")


def warning_id(kind: OverlapKind, tickets: tuple[str, str], details: tuple[str, ...]) -> str:
    raw = "|".join([kind.value, *tickets, *sorted(details)])
    return "OVL-" + hashlib.sha256(raw.encode()).hexdigest()[:10]


def _ci(values: list[str]) -> dict[str, str]:
    return {v.strip().lower(): v.strip() for v in values if v.strip()}


def compare(
    mine: Footprint,
    other: Footprint,
    *,
    other_assignee: str | None = None,
    other_done: bool = False,
    low_signal_paths: Sequence[str] = (),
    use_actual: bool = False,
) -> list[OverlapFinding]:
    """Findings for ``mine`` with respect to ``other`` (directional for dependencies)."""
    findings: list[OverlapFinding] = []
    tickets = tuple(sorted((mine.ticket_key, other.ticket_key)))
    revisions = {
        mine.ticket_key: {
            "plan": mine.plan_revision,
            "commit": mine.actual_commit or mine.source_commit,
        },
        other.ticket_key: {
            "plan": other.plan_revision,
            "commit": other.actual_commit or other.source_commit,
        },
    }

    def add(kind: OverlapKind, sev: Severity, details: list[str]) -> None:
        d = tuple(sorted(set(details)))
        findings.append(
            OverlapFinding(
                warning_id(kind, tickets, d),  # type: ignore[arg-type]
                kind,
                sev,
                mine.ticket_key,
                other.ticket_key,
                other_assignee,
                d,
                revisions,
            )
        )

    if other.ticket_key in mine.ticket_dependencies and not other_done:
        add(
            OverlapKind.DECLARED_DEPENDENCY,
            Severity.HIGH,
            [f"{mine.ticket_key} depends on {other.ticket_key}"],
        )

    mine_paths = mine.actual_paths if use_actual and mine.actual_paths else mine.paths
    other_paths = other.actual_paths if use_actual and other.actual_paths else other.paths
    low = [_norm(p) for p in low_signal_paths]
    shared = sorted(
        {
            f"{a} ~ {b}" if _norm(a) != _norm(b) else _norm(a)
            for a in mine_paths
            for b in other_paths
            if paths_overlap(a, b) and _norm(a) not in low and _norm(b) not in low
        }
    )
    if shared:
        kind = OverlapKind.ACTUAL_PATH if use_actual else OverlapKind.SAME_PATH
        add(kind, Severity.WARN, shared)

    comps = sorted(set(_ci(mine.components)) & set(_ci(other.components)))
    if comps:
        add(OverlapKind.SAME_COMPONENT, Severity.WARN, [_ci(mine.components)[c] for c in comps])

    contracts: list[str] = []
    for label, a, b in (
        ("interface", mine.interfaces, other.interfaces),
        ("domain model", mine.domain_models, other.domain_models),
        ("schema", mine.schemas, other.schemas),
        ("migration", mine.migrations, other.migrations),
        ("dependency", mine.dependencies, other.dependencies),
    ):
        ca, cb = _ci(a), _ci(b)
        contracts.extend(f"{label}: {ca[k]}" for k in sorted(set(ca) & set(cb)))
    if contracts:
        add(OverlapKind.SHARED_CONTRACT, Severity.HIGH, contracts)
    return findings
