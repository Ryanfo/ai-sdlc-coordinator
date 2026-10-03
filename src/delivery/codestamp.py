"""When this installation's coordinator code last changed.

A running supervisor keeps the code it started with. It records this stamp at start, and
`coordinator status`, `coordinator attach` and the supervisor itself compare it with the files
on disk, so nobody assumes a fix is live before a `coordinator restart`.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parent


def code_mtime(package: Path = PACKAGE) -> float:
    """Latest modification time of the package's Python files."""
    newest = 0.0
    for p in package.rglob("*.py"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest
