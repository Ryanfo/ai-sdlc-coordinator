"""In-memory GitHub for PRs, reviews, check runs, statuses and merges.

PR heads are read from the real local 'origin' repository so they always reflect
what the coordinator actually pushed.
"""

from __future__ import annotations

import itertools
import subprocess
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from delivery.ports import (
    BranchProtection,
    CheckRun,
    CommitInfo,
    CommitStatus,
    IntegrationError,
    PullRequest,
    RepoInfo,
    Review,
    ReviewComment,
    ReviewThread,
    UncertainResult,
)


@dataclass
class FakeGitHub:
    origin: Path
    login: str = "dev-bot"
    prs: dict[int, PullRequest] = field(default_factory=dict)
    reviews_by_pr: dict[int, list[Review]] = field(default_factory=dict)
    runs: list[CheckRun] = field(default_factory=list)
    status_list: list[CommitStatus] = field(default_factory=list)
    protection: BranchProtection | None = field(
        default_factory=lambda: BranchProtection(
            required_approving_reviews=1,
            dismiss_stale_reviews=True,
            required_checks=("unit",),
            strict_up_to_date=True,
            enforce_admins=True,
            allow_force_pushes=False,
            allow_deletions=False,
        )
    )
    threads: dict[int, list[ReviewThread]] = field(default_factory=dict)
    lose_next_create: bool = False
    auto_ci: dict[str, str] = field(default_factory=dict)  # check name -> conclusion for every head
    ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def _head(self, branch: str) -> str:
        out = subprocess.run(
            ["git", "--git-dir", str(self.origin), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout.strip()

    def _refresh(self, pr: PullRequest) -> PullRequest:
        if pr.state == "open":
            head = self._head(pr.head_ref)
            if head and head != pr.head_sha:
                pr = replace(pr, head_sha=head)
                self.prs[pr.number] = pr
        return pr

    def approve(self, number: int, login: str, commit: str | None = None) -> None:
        pr = self._refresh(self.prs[number])
        self.reviews_by_pr.setdefault(number, []).append(
            Review(next(self.ids), login, "APPROVED", commit or pr.head_sha, datetime.now(UTC))
        )

    def comment_on_line(
        self,
        number: int,
        path: str,
        line: int,
        body: str,
        login: str = "reviewer",
        *,
        replies: tuple[tuple[str, str], ...] = (),
        resolved: bool = False,
        at: datetime | None = None,
    ) -> ReviewThread:
        """A reviewer starts a conversation on a line of the PR (with optional replies)."""
        when = at or datetime.now(UTC)
        n = next(self.ids)
        thread = ReviewThread(
            f"T{n}",
            path,
            line,
            resolved,
            False,
            (
                ReviewComment(login, body, when, f"https://github.com/example/app/pull/{number}#r{n}"),
                *(ReviewComment(who, text, when) for who, text in replies),
            ),
        )
        self.threads.setdefault(number, []).append(thread)
        return thread

    def resolve(self, number: int, thread_id: str) -> None:
        self.threads[number] = [
            replace(t, resolved=True) if t.id == thread_id else t for t in self.threads.get(number, [])
        ]

    def add_ci(self, sha: str, name: str, conclusion: str | None, app: str = "github-actions") -> None:
        status = "completed" if conclusion else "in_progress"
        self.runs.append(CheckRun(next(self.ids), name, sha, status, conclusion, app, f"https://ci/{name}"))

    def merge(self, number: int, merged_by: str = "human", how: str = "merge") -> str:
        """Merge like GitHub would, in the real origin repository."""
        pr = self._refresh(self.prs[number])
        work = self.origin.parent / f"merge-{number}"
        if not work.exists():
            subprocess.run(["git", "clone", "-q", str(self.origin), str(work)], check=True)
        g = ["git", "-c", "user.name=h", "-c", "user.email=h@h", "-c", "commit.gpgsign=false"]
        subprocess.run([*g, "fetch", "-q", "origin"], cwd=work, check=True)
        subprocess.run(
            [*g, "checkout", "-q", "-B", pr.base_ref, f"origin/{pr.base_ref}"], cwd=work, check=True
        )
        if how == "squash":
            subprocess.run([*g, "merge", "-q", "--squash", pr.head_sha], cwd=work, check=True)
            subprocess.run([*g, "commit", "-q", "-m", f"{pr.title} (#{number})"], cwd=work, check=True)
        else:
            subprocess.run(
                [*g, "merge", "-q", "--no-ff", "-m", f"Merge #{number}", pr.head_sha], cwd=work, check=True
            )
        subprocess.run([*g, "push", "-q", "origin", f"HEAD:refs/heads/{pr.base_ref}"], cwd=work, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.prs[number] = replace(
            pr,
            state="closed",
            merged=True,
            merge_commit_sha=sha,
            merged_at=datetime.now(UTC),
            merged_by=merged_by,
        )
        return sha

    # ------------------------------------------------------------------ GitHubPort
    async def viewer_login(self) -> str:
        return self.login

    async def repo(self) -> RepoInfo:
        return RepoInfo("example/app", "public", "main", True, False)

    async def find_prs(self, head_branch: str, state: str = "all") -> list[PullRequest]:
        return [
            self._refresh(p)
            for p in self.prs.values()
            if p.head_ref == head_branch and (state == "all" or p.state == state)
        ]

    async def create_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        if any(p.head_ref == head and p.state == "open" for p in self.prs.values()):
            raise IntegrationError("A pull request already exists", status=422)
        n = next(self.ids)
        pr = PullRequest(
            n,
            f"https://github.com/example/app/pull/{n}",
            "open",
            False,
            head,
            self._head(head),
            base,
            self._head(base),
            self.login,
            title,
            body,
        )
        self.prs[n] = pr
        if self.lose_next_create:
            self.lose_next_create = False
            raise UncertainResult("response lost after PR created")
        return pr

    async def update_pr(self, number: int, title: str, body: str) -> PullRequest:
        pr = replace(self.prs[number], title=title, body=body)
        self.prs[number] = pr
        return pr

    async def get_pr(self, number: int) -> PullRequest:
        return self._refresh(self.prs[number])

    async def reviews(self, number: int) -> list[Review]:
        return list(self.reviews_by_pr.get(number, []))

    async def check_runs(self, sha: str) -> list[CheckRun]:
        runs = [r for r in self.runs if r.head_sha == sha]
        for name, conclusion in self.auto_ci.items():
            if not any(r.name == name for r in runs):
                runs.append(
                    CheckRun(
                        next(self.ids),
                        name,
                        sha,
                        "completed",
                        conclusion,
                        "github-actions",
                        f"https://ci/{name}",
                    )
                )
        return runs

    async def statuses(self, sha: str) -> list[CommitStatus]:
        return list(self.status_list)

    async def branch_protection(self, branch: str) -> BranchProtection | None:
        return self.protection

    async def commit(self, sha: str) -> CommitInfo:
        out = subprocess.run(
            ["git", "--git-dir", str(self.origin), "show", "-s", "--format=%H %T %P", sha],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        return CommitInfo(out[0], tuple(out[2:]), out[1])

    async def prs_for_commit(self, sha: str) -> list[PullRequest]:
        return [p for p in self.prs.values() if p.head_sha == sha or p.merge_commit_sha == sha]

    async def revert_pr(self, number: int, title: str, body: str) -> PullRequest:
        """Like GitHub: a new branch off the base that reverts the merged PR, and a PR for it."""
        pr = self.prs[number]
        assert pr.merged and pr.merge_commit_sha
        work = self.origin.parent / f"revert-{number}"
        g = ["git", "-c", "user.name=h", "-c", "user.email=h@h", "-c", "commit.gpgsign=false"]
        if not work.exists():
            subprocess.run(["git", "clone", "-q", str(self.origin), str(work)], check=True)
        subprocess.run([*g, "fetch", "-q", "origin"], cwd=work, check=True)
        branch = f"revert-{number}-{pr.head_ref.replace('/', '-')}"
        subprocess.run([*g, "checkout", "-q", "-B", branch, f"origin/{pr.base_ref}"], cwd=work, check=True)
        parents = self._parents(pr.merge_commit_sha)
        mainline = ["-m", "1"] if len(parents) > 1 else []
        subprocess.run([*g, "revert", "--no-edit", *mainline, pr.merge_commit_sha], cwd=work, check=True)
        subprocess.run([*g, "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=work, check=True)
        return await self.create_pr(branch, pr.base_ref, title, body)

    def _parents(self, sha: str) -> list[str]:
        out = subprocess.run(
            ["git", "--git-dir", str(self.origin), "show", "-s", "--format=%P", sha],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.split()

    async def review_threads(self, number: int) -> list[ReviewThread]:
        return list(self.threads.get(number, []))
