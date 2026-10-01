"""GitHub adapter using the authenticated ``gh`` CLI (``gh api``).

The coordinator uses the operator's ``gh`` login; no token is stored in configuration and
Claude worker sessions never inherit it. Lists use ``--paginate --slurp``. Writes are not
retried blindly: a failure without an HTTP status is reported as uncertain.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from delivery.ports import (
    AuthError,
    BranchProtection,
    CheckRun,
    CommitInfo,
    CommitStatus,
    IntegrationError,
    NotFound,
    PullRequest,
    RepoInfo,
    Review,
    UncertainResult,
)
from delivery.proc import ProcessStartError, run_process

_HTTP = re.compile(r"HTTP (\d{3})")


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


class GhClient:
    def __init__(self, slug: str, executable: str = "gh") -> None:
        self.slug = slug
        self.exe = executable

    async def api(
        self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None, paginate: bool = False
    ) -> Any:
        argv = [
            self.exe,
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            "--method",
            method,
            path,
        ]
        if paginate:
            argv += ["--paginate", "--slurp"]
        if body is not None:
            argv += ["--input", "-"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GH_PROMPT_DISABLED"] = "1"
        try:
            res = await run_process(
                argv,
                cwd=Path.home(),
                env=env,
                timeout=60,
                stdin_data=json.dumps(body).encode() if body is not None else None,
            )
        except ProcessStartError as exc:
            raise IntegrationError(f"gh CLI unavailable: {exc}") from None
        if res.timed_out:
            if method == "GET":
                raise IntegrationError(f"gh api {path} timed out", retryable=True)
            raise UncertainResult(f"gh api {method} {path} timed out; outcome unknown")
        if res.returncode != 0:
            m = _HTTP.search(res.stderr)
            status = int(m.group(1)) if m else None
            msg = f"GitHub {status or 'error'} on {method} {path}: {res.stderr.strip()[-300:]}"
            if status in (401, 403):
                raise AuthError(msg, status=status)
            if status == 404:
                raise NotFound(msg, status=404)
            if status is None and method != "GET":
                raise UncertainResult(msg)
            raise IntegrationError(msg, status=status, retryable=status is None or status >= 500)
        if not res.stdout.strip():
            return None
        data = json.loads(res.stdout)
        if paginate and isinstance(data, list):
            merged: list[Any] = []
            for page in data:
                if isinstance(page, list):
                    merged.extend(page)
                else:
                    merged.append(page)
            return merged
        return data

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _pr(d: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=int(d["number"]),
            url=d.get("html_url", ""),
            state=d.get("state", ""),
            merged=bool(d.get("merged") or d.get("merged_at")),
            head_ref=(d.get("head") or {}).get("ref", ""),
            head_sha=(d.get("head") or {}).get("sha", ""),
            base_ref=(d.get("base") or {}).get("ref", ""),
            base_sha=(d.get("base") or {}).get("sha", ""),
            author_login=(d.get("user") or {}).get("login", ""),
            title=d.get("title", ""),
            body=d.get("body") or "",
            merge_commit_sha=d.get("merge_commit_sha") if d.get("merged_at") else None,
            merged_at=_dt(d.get("merged_at")),
            merged_by=(d.get("merged_by") or {}).get("login"),
        )

    # ------------------------------------------------------------------ GitHubPort
    async def viewer_login(self) -> str:
        return str((await self.api("user"))["login"])

    async def repo(self) -> RepoInfo:
        d = await self.api(f"repos/{self.slug}")
        perms = d.get("permissions") or {}
        return RepoInfo(
            d["full_name"],
            d.get("visibility", "private" if d.get("private") else "public"),
            d.get("default_branch", "main"),
            bool(perms.get("push")),
            bool(perms.get("admin")),
            bool(d.get("allow_merge_commit", True)),
            bool(d.get("allow_squash_merge", True)),
            bool(d.get("allow_rebase_merge", True)),
        )

    async def find_prs(self, head_branch: str, state: str = "all") -> list[PullRequest]:
        owner = self.slug.split("/")[0]
        data = await self.api(f"repos/{self.slug}/pulls?head={owner}:{head_branch}&state={state}&per_page=50")
        return [self._pr(d) for d in data or []]

    async def create_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        d = await self.api(
            f"repos/{self.slug}/pulls",
            method="POST",
            body={"head": head, "base": base, "title": title, "body": body},
        )
        return self._pr(d)

    async def update_pr(self, number: int, title: str, body: str) -> PullRequest:
        d = await self.api(
            f"repos/{self.slug}/pulls/{number}", method="PATCH", body={"title": title, "body": body}
        )
        return self._pr(d)

    async def get_pr(self, number: int) -> PullRequest:
        return self._pr(await self.api(f"repos/{self.slug}/pulls/{number}"))

    async def reviews(self, number: int) -> list[Review]:
        data = await self.api(f"repos/{self.slug}/pulls/{number}/reviews?per_page=100", paginate=True)
        return [
            Review(
                int(r["id"]),
                (r.get("user") or {}).get("login", ""),
                r.get("state", ""),
                r.get("commit_id", ""),
                _dt(r.get("submitted_at")),
                (r.get("user") or {}).get("type", "User"),
            )
            for r in data or []
        ]

    async def check_runs(self, sha: str) -> list[CheckRun]:
        pages = await self.api(f"repos/{self.slug}/commits/{sha}/check-runs?per_page=100", paginate=True)
        runs: list[CheckRun] = []
        for page in pages or []:
            for c in page.get("check_runs", []):
                runs.append(
                    CheckRun(
                        int(c["id"]),
                        c.get("name", ""),
                        c.get("head_sha", ""),
                        c.get("status", ""),
                        c.get("conclusion"),
                        (c.get("app") or {}).get("slug", ""),
                        c.get("html_url", ""),
                        _dt(c.get("started_at")),
                        _dt(c.get("completed_at")),
                    )
                )
        return runs

    async def statuses(self, sha: str) -> list[CommitStatus]:
        data = await self.api(f"repos/{self.slug}/commits/{sha}/statuses?per_page=100", paginate=True)
        return [
            CommitStatus(
                int(s["id"]),
                s.get("context", ""),
                s.get("state", ""),
                (s.get("creator") or {}).get("login", ""),
                s.get("target_url") or "",
                s.get("description") or "",
                _dt(s.get("created_at")),
            )
            for s in data or []
        ]

    async def branch_protection(self, branch: str) -> BranchProtection | None:
        """Effective rules via the rules API (readable without admin), else classic protection."""
        try:
            rules = await self.api(f"repos/{self.slug}/rules/branches/{branch}", paginate=True)
        except NotFound:
            rules = []
        if rules:
            reviews = 0
            dismiss = last_push = strict = False
            checks: list[str] = []
            no_force = no_delete = False
            for r in rules:
                t, p = r.get("type"), r.get("parameters") or {}
                if t == "pull_request":
                    reviews = max(reviews, int(p.get("required_approving_review_count", 0)))
                    dismiss = dismiss or bool(p.get("dismiss_stale_reviews_on_push"))
                    last_push = last_push or bool(p.get("require_last_push_approval"))
                elif t == "required_status_checks":
                    strict = strict or bool(p.get("strict_required_status_checks_policy"))
                    checks += [c.get("context", "") for c in p.get("required_status_checks", [])]
                elif t == "non_fast_forward":
                    no_force = True
                elif t == "deletion":
                    no_delete = True
            return BranchProtection(
                reviews,
                dismiss,
                last_push,
                tuple(checks),
                strict,
                False,
                not no_force,
                not no_delete,
                source="rulesets",
            )
        try:
            d = await self.api(f"repos/{self.slug}/branches/{branch}/protection")
        except NotFound:
            return None
        except AuthError:
            return BranchProtection(source="unreadable (needs admin to read classic protection)")
        prr = d.get("required_pull_request_reviews") or {}
        rsc = d.get("required_status_checks") or {}
        return BranchProtection(
            int(prr.get("required_approving_review_count", 0)),
            bool(prr.get("dismiss_stale_reviews")),
            bool(prr.get("require_last_push_approval")),
            tuple(c.get("context", "") for c in rsc.get("checks", [])) or tuple(rsc.get("contexts", [])),
            bool(rsc.get("strict")),
            bool((d.get("enforce_admins") or {}).get("enabled")),
            bool((d.get("allow_force_pushes") or {}).get("enabled")),
            bool((d.get("allow_deletions") or {}).get("enabled")),
        )

    async def commit(self, sha: str) -> CommitInfo:
        d = await self.api(f"repos/{self.slug}/commits/{sha}")
        return CommitInfo(
            d["sha"],
            tuple(p["sha"] for p in d.get("parents", [])),
            (d.get("commit") or {}).get("tree", {}).get("sha", ""),
            (d.get("commit") or {}).get("message", ""),
        )

    async def prs_for_commit(self, sha: str) -> list[PullRequest]:
        data = await self.api(f"repos/{self.slug}/commits/{sha}/pulls")
        return [self._pr(d) for d in data or []]
