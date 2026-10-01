"""Managed Git repository: a coordinator-owned bare clone plus one worktree per run.

The developer's own checkout is never modified. Operations that change shared metadata
(clone, fetch, worktree add/remove, commit, push) take the short repository lock; a
Claude session never holds it. There is no force push: a diverged remote blocks.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from delivery.ownership import RepoLocks
from delivery.ports import UncertainResult
from delivery.proc import ProcResult, run_process

SHA = re.compile(r"^[0-9a-f]{40}$")
_NETWORK_HINTS = (
    "Could not resolve host",
    "Connection timed out",
    "Connection reset",
    "unable to access",
    "early EOF",
    "The remote end hung up",
    "Operation timed out",
)


class GitError(Exception):
    def __init__(self, message: str, result: ProcResult | None = None) -> None:
        self.result = result
        detail = f": {result.stderr.strip()[-500:]}" if result and result.stderr.strip() else ""
        super().__init__(message + detail)


class BranchDiverged(GitError):
    """The remote branch cannot be fast-forwarded. Never force-pushed."""


class WorktreeConflict(GitError):
    """Worktree path or branch already in use (dirty, foreign or another writer)."""


@dataclass(frozen=True)
class MergeResult:
    ok: bool
    sha: str | None
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class CommitDetails:
    sha: str
    parents: tuple[str, ...]
    tree: str
    message: str


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.setdefault("LANG", "C")
    return env


class ManagedRepo:
    def __init__(
        self,
        url: str,
        base_branch: str,
        root: Path,
        locks: RepoLocks,
        *,
        author_name: str = "delivery coordinator",
        author_email: str = "delivery-coordinator@localhost",
        reference: Path | None = None,
    ) -> None:
        self.url = url
        self.base_branch = base_branch
        self.root = root
        self.git_dir = root / "_repo.git"
        self.locks = locks
        self.key = hashlib.sha256(url.encode()).hexdigest()[:12]
        self.author_name = author_name
        self.author_email = author_email
        self.reference = reference

    # ------------------------------------------------------------------ plumbing
    def _argv(self, args: tuple[str, ...], bare: bool) -> list[str]:
        base = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgsign=false",
            "-c", "advice.detachedHead=false",
            "-c", f"user.name={self.author_name}",
            "-c", f"user.email={self.author_email}",
        ]  # fmt: skip
        if bare:
            base += ["--git-dir", str(self.git_dir)]
        return base + list(args)

    async def git(
        self, *args: str, cwd: Path | None = None, check: bool = True, timeout: float = 600
    ) -> ProcResult:
        res = await run_process(
            self._argv(args, bare=cwd is None),
            cwd=cwd or self.root,
            env=_git_env(),
            timeout=timeout,
        )
        if res.timed_out:
            raise UncertainResult(f"git {args[0]} timed out")
        if check and res.returncode != 0:
            raise GitError(f"git {' '.join(args[:2])} failed", res)
        return res

    # ------------------------------------------------------------------ repository
    async def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        async with self.locks.hold(self.key, "ensure"):
            if not self.git_dir.exists():
                args = ["clone", "--bare", "--no-tags"]
                if self.reference and (self.reference / ".git").exists():
                    args += ["--reference-if-able", str(self.reference), "--dissociate"]
                await self.git(*args, self.url, str(self.git_dir), cwd=self.root)
                await self.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
            else:
                res = await self.git("config", "--get", "remote.origin.url", check=False)
                if res.stdout.strip() != self.url:
                    raise GitError(
                        f"managed repository at {self.git_dir} points at "
                        f"{res.stdout.strip() or 'nothing'}, not {self.url}"
                    )
        await self.fetch()

    async def fetch(self) -> None:
        async with self.locks.hold(self.key, "fetch"):
            res = await self.git("fetch", "origin", "--prune", "--no-tags", check=False)
            if res.returncode != 0:
                if any(h in res.stderr for h in _NETWORK_HINTS):
                    raise UncertainResult(f"git fetch failed: {res.stderr.strip()[-300:]}")
                raise GitError("git fetch failed", res)

    async def remote_sha(self, branch: str) -> str | None:
        res = await self.git(
            "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}^{{commit}}", check=False
        )
        sha = res.stdout.strip()
        return sha if SHA.match(sha) else None

    async def ls_remote(self, branch: str) -> str | None:
        res = await self.git("ls-remote", "--heads", "origin", f"refs/heads/{branch}", check=False)
        if res.returncode != 0:
            raise UncertainResult(f"git ls-remote failed: {res.stderr.strip()[-300:]}")
        for line in res.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref == f"refs/heads/{branch}":
                return sha
        return None

    async def resolve(self, ref: str) -> str | None:
        res = await self.git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
        sha = res.stdout.strip()
        return sha if SHA.match(sha) else None

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        res = await self.git("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        return res.returncode == 0

    async def commit_details(self, sha: str) -> CommitDetails:
        res = await self.git("show", "-s", "--format=%H%n%P%n%T%n%B", sha)
        lines = res.stdout.splitlines()
        return CommitDetails(lines[0], tuple(lines[1].split()), lines[2], "\n".join(lines[3:]))

    async def show_file(self, ref: str, path: str) -> bytes | None:
        res = await run_process(
            self._argv(("show", f"{ref}:{path}"), bare=True), cwd=self.root, env=_git_env()
        )
        return res.stdout.encode() if res.returncode == 0 else None

    async def ls_tree(self, ref: str, prefix: str) -> list[str]:
        res = await self.git("ls-tree", "-r", "--name-only", ref, "--", prefix, check=False)
        return [ln for ln in res.stdout.splitlines() if ln] if res.returncode == 0 else []

    async def diff_names(self, a: str, b: str) -> list[str]:
        res = await self.git("diff", "--name-only", f"{a}...{b}")
        return [ln for ln in res.stdout.splitlines() if ln]

    async def find_commit_with_marker(self, branch: str, marker: str) -> str | None:
        """Find a commit on the remote-tracking branch carrying an operation marker."""
        res = await self.git(
            "log",
            f"refs/remotes/origin/{branch}",
            "--format=%H",
            "-F",
            f"--grep={marker}",
            "-n",
            "1",
            check=False,
        )
        sha = res.stdout.strip()
        return sha if SHA.match(sha) else None

    # ------------------------------------------------------------------ worktrees
    async def add_worktree(self, path: Path, *, start: str, branch: str | None = None) -> Path:
        """Create a worktree. A named branch can only be checked out by one worktree."""
        if path.exists():
            raise WorktreeConflict(f"worktree path {path} already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        async with self.locks.hold(self.key, "worktree-add"):
            if branch:
                res = await self.git("worktree", "add", "-B", branch, str(path), start, check=False)
            else:
                res = await self.git("worktree", "add", "--detach", str(path), start, check=False)
        if res.returncode != 0:
            if "already checked out" in res.stderr or "is already used by worktree" in res.stderr:
                raise WorktreeConflict(f"branch {branch} is already in use by another run", res)
            raise GitError("git worktree add failed", res)
        return path

    async def remove_worktree(self, path: Path) -> None:
        if not path.exists():
            async with self.locks.hold(self.key, "worktree-prune"):
                await self.git("worktree", "prune", check=False)
            return
        if self.root not in path.parents:
            raise GitError(f"refusing to remove {path}: not a managed worktree")
        async with self.locks.hold(self.key, "worktree-remove"):
            res = await self.git("worktree", "remove", "--force", str(path), check=False)
            if res.returncode != 0 and path.exists():
                shutil.rmtree(path)
            await self.git("worktree", "prune", check=False)

    async def worktree_head(self, wt: Path) -> str:
        res = await self.git("rev-parse", "HEAD", cwd=wt)
        return res.stdout.strip()

    async def status(self, wt: Path) -> list[str]:
        res = await self.git("status", "--porcelain=v1", "--untracked-files=all", cwd=wt)
        return [ln for ln in res.stdout.splitlines() if ln]

    async def tracked_changes(self, wt: Path) -> list[str]:
        """Modified/deleted tracked files (ignores untracked caches and build output)."""
        res = await self.git("status", "--porcelain=v1", "--untracked-files=no", cwd=wt)
        return [ln[3:] for ln in res.stdout.splitlines() if ln]

    async def changed_paths(self, wt: Path, since: str) -> list[str]:
        committed = await self.git("diff", "--name-only", since, cwd=wt)
        untracked = await self.git("ls-files", "--others", "--exclude-standard", cwd=wt)
        names = set(committed.stdout.split("\n")) | set(untracked.stdout.split("\n"))
        return sorted(n for n in names if n)

    async def commit(self, wt: Path, message: str, *, paths: list[str] | None = None) -> str | None:
        """Stage and commit. Returns the new SHA, or None if there was nothing to commit."""
        async with self.locks.hold(self.key, "commit"):
            await self.git("add", "-A", "--", *(paths or ["."]), cwd=wt)
            staged = await self.git("diff", "--cached", "--quiet", cwd=wt, check=False)
            if staged.returncode == 0:
                return None
            await self.git("commit", "--no-verify", "-q", "-m", message, cwd=wt)
            head = await self.git("rev-parse", "HEAD", cwd=wt)
        return head.stdout.strip()

    async def push(self, wt: Path, branch: str) -> None:
        """Fast-forward-only push of HEAD. Divergence blocks; uncertainty must be reconciled."""
        async with self.locks.hold(self.key, "push"):
            res = await self.git(
                "push", "--porcelain", "origin", f"HEAD:refs/heads/{branch}", cwd=wt, check=False
            )
        if res.returncode == 0:
            return
        text = res.stderr + res.stdout
        if any(s in text for s in ("non-fast-forward", "fetch first", "[rejected]", "stale info")):
            raise BranchDiverged(f"remote {branch} has diverged; no force push", res)
        if any(h in text for h in _NETWORK_HINTS):
            raise UncertainResult(f"push of {branch} may not have completed")
        raise GitError(f"push of {branch} failed", res)

    async def merge(self, wt: Path, ref: str, message: str) -> MergeResult:
        res = await self.git("merge", "--no-ff", "--no-edit", "-m", message, ref, cwd=wt, check=False)
        if res.returncode == 0:
            return MergeResult(True, await self.worktree_head(wt), ())
        conflicts = await self.git("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False)
        await self.git("merge", "--abort", cwd=wt, check=False)
        names = tuple(n for n in conflicts.stdout.splitlines() if n)
        if not names:
            raise GitError("git merge failed", res)
        return MergeResult(False, None, names)


def blob_url(repo_url: str, commit: str, path: str) -> str:
    """Immutable GitHub URL pinned to a commit, never a moving branch."""
    base = repo_url.removesuffix(".git").rstrip("/")
    return f"{base}/blob/{commit}/{path}"


def commit_url(repo_url: str, commit: str) -> str:
    return f"{repo_url.removesuffix('.git').rstrip('/')}/commit/{commit}"


def markers(run_id: str, op_id: str) -> str:
    return f"Delivery-Run: {run_id}\nDelivery-Op: {op_id}"
