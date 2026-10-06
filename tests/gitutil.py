"""Real Git fixtures: a bare 'origin' plus helpers to make commits as another party."""

from __future__ import annotations

import subprocess
from pathlib import Path


def sh(*args: str, cwd: Path) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def make_origin(tmp: Path, files: dict[str, str] | None = None) -> Path:
    origin = tmp / "origin.git"
    sh("init", "--bare", "-b", "main", str(origin), cwd=tmp)
    # A push can start a detached `gc --auto` in origin that packs loose objects while a
    # local clone is copying them ("failed to copy file ... objects/..."), so never run it.
    for key, value in (("receive.autogc", "false"), ("gc.auto", "0"), ("maintenance.auto", "false")):
        sh("config", key, value, cwd=origin)
    seed = tmp / "seed"
    sh("clone", str(origin), str(seed), cwd=tmp)
    for path, text in (files or {"README.md": "app\n", "src/app.ts": "export const x = 1;\n"}).items():
        p = seed / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    sh("add", "-A", cwd=seed)
    sh("commit", "-m", "init", cwd=seed)
    sh("push", "origin", "HEAD:refs/heads/main", cwd=seed)
    return origin


def external_commit(tmp: Path, origin: Path, branch: str, path: str, text: str, name: str) -> str:
    """Simulate another party pushing to a branch."""
    work = tmp / f"ext-{name}"
    if not work.exists():
        sh("clone", str(origin), str(work), cwd=tmp)
    sh("fetch", "origin", cwd=work)
    try:
        sh("checkout", "-B", branch, f"origin/{branch}", cwd=work)
    except subprocess.CalledProcessError:
        sh("checkout", "-B", branch, "origin/main", cwd=work)
    p = work / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    sh("add", "-A", cwd=work)
    sh("commit", "-m", f"external {name}", cwd=work)
    sh("push", "origin", f"HEAD:refs/heads/{branch}", cwd=work)
    return sh("rev-parse", "HEAD", cwd=work)
