from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from delivery.git import BranchDiverged, ManagedRepo, WorktreeConflict, markers
from delivery.ownership import RepoLocks
from gitutil import external_commit, make_origin


@pytest.fixture
async def repo(tmp_path: Path) -> ManagedRepo:
    origin = make_origin(tmp_path)
    r = ManagedRepo(str(origin), "main", tmp_path / "wt", RepoLocks(tmp_path / "locks"))
    await r.ensure()
    return r


async def test_clone_is_bare_and_separate_from_checkout(repo: ManagedRepo) -> None:
    assert repo.git_dir.exists() and (repo.git_dir / "HEAD").exists()
    assert await repo.remote_sha("main")


async def test_one_writer_per_branch(repo: ManagedRepo, tmp_path: Path) -> None:
    a = await repo.add_worktree(tmp_path / "wt/P-1/r1/feature", start="origin/main", branch="feature/P-1")
    with pytest.raises(WorktreeConflict):
        await repo.add_worktree(tmp_path / "wt/P-1/r2/feature", start="origin/main", branch="feature/P-1")
    with pytest.raises(WorktreeConflict):
        await repo.add_worktree(a, start="origin/main", branch="feature/P-2")
    b = await repo.add_worktree(tmp_path / "wt/P-2/r1/feature", start="origin/main", branch="feature/P-2")
    assert a != b


async def test_commit_push_and_find_marker(repo: ManagedRepo, tmp_path: Path) -> None:
    wt = await repo.add_worktree(tmp_path / "wt/P-1/r1/d", start="origin/main", branch="delivery/P-1")
    (wt / "docs/delivery/P-1/specification").mkdir(parents=True)
    (wt / "docs/delivery/P-1/specification/v001.md").write_text("# spec\n")
    sha = await repo.commit(wt, "P-1: spec v1\n\n" + markers("r1", "op-abc"))
    assert sha
    assert await repo.commit(wt, "nothing") is None  # nothing to commit
    await repo.push(wt, "delivery/P-1")
    await repo.fetch()
    assert await repo.find_commit_with_marker("delivery/P-1", "Delivery-Op: op-abc") == sha
    assert await repo.find_commit_with_marker("delivery/P-1", "Delivery-Op: op-zzz") is None
    assert await repo.ls_remote("delivery/P-1") == sha


async def test_diverged_remote_blocks_without_force(repo: ManagedRepo, tmp_path: Path) -> None:
    wt = await repo.add_worktree(tmp_path / "wt/P-1/r1/f", start="origin/main", branch="feature/P-1")
    (wt / "a.txt").write_text("ours\n")
    await repo.commit(wt, "ours")
    await repo.push(wt, "feature/P-1")
    origin = Path(repo.url)
    theirs = external_commit(tmp_path, origin, "feature/P-1", "b.txt", "theirs\n", "x")
    (wt / "c.txt").write_text("more\n")
    await repo.commit(wt, "more")
    with pytest.raises(BranchDiverged):
        await repo.push(wt, "feature/P-1")
    assert await repo.ls_remote("feature/P-1") == theirs  # remote untouched


async def test_merge_conflict_detected_and_aborted(repo: ManagedRepo, tmp_path: Path) -> None:
    origin = Path(repo.url)
    external_commit(tmp_path, origin, "feature/A", "src/app.ts", "export const x = 2;\n", "a")
    external_commit(tmp_path, origin, "feature/B", "src/app.ts", "export const x = 3;\n", "b")
    await repo.fetch()
    wt = await repo.add_worktree(tmp_path / "wt/int", start="origin/feature/A")
    res = await repo.merge(wt, "origin/feature/B", "integration")
    assert not res.ok and res.conflicts == ("src/app.ts",)
    assert await repo.status(wt) == []  # merge aborted cleanly


async def test_clean_merge_and_ancestry(repo: ManagedRepo, tmp_path: Path) -> None:
    origin = Path(repo.url)
    a = external_commit(tmp_path, origin, "feature/A", "a.ts", "a\n", "a2")
    external_commit(tmp_path, origin, "feature/B", "b.ts", "b\n", "b2")
    await repo.fetch()
    wt = await repo.add_worktree(tmp_path / "wt/int2", start=a)
    res = await repo.merge(wt, "origin/feature/B", "integration")
    assert res.ok and res.sha
    details = await repo.commit_details(res.sha)
    assert details.parents[0] == a and len(details.parents) == 2
    assert await repo.is_ancestor(a, res.sha)


async def test_concurrent_worktrees_and_tracked_changes(repo: ManagedRepo, tmp_path: Path) -> None:
    paths = [tmp_path / f"wt/P-{i}/r/f" for i in range(8)]
    await asyncio.gather(
        *(repo.add_worktree(p, start="origin/main", branch=f"feature/P-{i}") for i, p in enumerate(paths))
    )
    (paths[0] / "README.md").write_text("changed\n")
    (paths[0] / "node_modules").mkdir()
    (paths[0] / "node_modules/x.js").write_text("cache")
    assert await repo.tracked_changes(paths[0]) == ["README.md"]
    assert await repo.tracked_changes(paths[1]) == []
    await repo.remove_worktree(paths[0])
    assert not paths[0].exists()
    with pytest.raises(Exception, match="not a managed worktree"):
        await repo.remove_worktree(tmp_path)


async def test_mismatched_managed_repo_refused(tmp_path: Path, repo: ManagedRepo) -> None:
    other = ManagedRepo("https://github.com/x/y.git", "main", repo.root, repo.locks)
    with pytest.raises(Exception, match="points at"):
        await other.ensure()
