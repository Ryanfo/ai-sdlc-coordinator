"""GitHub merge-commit CI provenance (amendment §6): CI runs on head merged onto base."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from delivery.ports import CommitStatus
from delivery.stages import VerificationStage
from gitutil import external_commit, sh
from harness import World, make_world


def merge_commit(tmp: Path, origin: Path, base: str, head: str) -> str:
    """Create GitHub's temporary merge commit (refs/pull/N/merge) in the origin."""
    work = tmp / "prmerge"
    if not work.exists():
        sh("clone", "-q", str(origin), str(work), cwd=tmp)
    sh("fetch", "-q", "origin", cwd=work)
    sh("checkout", "-q", "--detach", base, cwd=work)
    sh("merge", "-q", "--no-ff", "-m", "Merge head into base", head, cwd=work)
    sha = sh("rev-parse", "HEAD", cwd=work)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/pull/1/merge"], cwd=work, check=True)
    return sha


def _stage(w: World) -> VerificationStage:
    """Only ``ci_provenance`` is exercised, which needs the config and GitHub port."""
    stage = VerificationStage.__new__(VerificationStage)
    stage.deps = w.deps
    stage.ctx = SimpleNamespace(cfg=w.cfg)  # type: ignore[assignment]
    return stage


async def test_provenance_states(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    base = sh("--git-dir", str(w.origin), "rev-parse", "refs/heads/main", cwd=tmp_path)
    head = external_commit(tmp_path, w.origin, "feature/P-1", "src/x.ts", "x\n", "feat")
    tested = merge_commit(tmp_path, w.origin, base, head)
    stage = _stage(w)
    assert (await stage.ci_provenance(head, base))["state"] == "missing"

    def status(desc: str, sid: int) -> CommitStatus:
        return CommitStatus(
            sid, "delivery/integration-provenance", "success", "github-actions[bot]", "", desc
        )

    w.github.status_list.append(status(f"tested={tested} base={base}", 1))
    assert (await stage.ci_provenance(head, base))["state"] == "verified"
    newer_base = external_commit(tmp_path, w.origin, "main", "docs/n.md", "n\n", "newbase")
    assert (await stage.ci_provenance(head, newer_base))["state"] == "stale_base"
    w.github.status_list.append(status(f"tested={tested} base={newer_base}", 2))
    bad = await stage.ci_provenance(head, newer_base)
    assert bad["state"] == "mismatch" and "not" in bad["detail"]
