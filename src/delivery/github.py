"""GitHub adapter using the authenticated ``gh`` CLI (``gh api``).

The coordinator uses the operator's ``gh`` login; no token is stored in configuration and
Claude worker sessions never inherit it. Lists use ``--paginate --slurp``. Writes are not
retried blindly: a failure without an HTTP status is reported as uncertain. Requests GitHub
rejected for its rate limits are tried again after a pause.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
    ReviewComment,
    ReviewThread,
    UncertainResult,
)
from delivery.proc import ProcessStartError, run_process

log = logging.getLogger("delivery")


class RateLimited(IntegrationError):
    """GitHub refused the request for its rate limits (nothing was done)."""


_HTTP = re.compile(r"HTTP (\d{3})")
# GitHub's primary and secondary rate limits answer 403 or 429 with one of these.
_RATE_LIMITED = re.compile(r"(rate limit|abuse detection|retry-after|too many requests)", re.I)
# Seconds to wait before each new try of a rate-limited request.
RATE_LIMIT_PAUSES = (20.0, 60.0, 120.0)


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


class GhClient:
    def __init__(
        self, slug: str, executable: str = "gh", *, pauses: tuple[float, ...] = RATE_LIMIT_PAUSES
    ) -> None:
        self.slug = slug
        self.exe = executable
        self.pauses = pauses

    async def api(
        self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None, paginate: bool = False
    ) -> Any:
        """One GitHub API call. Rate-limited requests are tried again after a pause.

        GitHub rejects a rate-limited request before acting on it, so repeating it is safe for
        writes too. If it is still limited after the last pause, the error is retryable: the run
        is resumed later rather than blocked.
        """
        for pause in (*self.pauses, None):
            try:
                return await self._api(path, method=method, body=body, paginate=paginate)
            except RateLimited as exc:
                if pause is None:
                    raise IntegrationError(str(exc), status=exc.status, retryable=True) from None
                log.info("GitHub rate limit on %s %s; trying again in %.0fs", method, path, pause)
                await asyncio.sleep(pause)
        raise AssertionError("unreachable")

    async def _api(self, path: str, *, method: str, body: dict[str, Any] | None, paginate: bool) -> Any:
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
            if status in (403, 429) and _RATE_LIMITED.search(res.stderr):
                raise RateLimited(msg, status=status)
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
                r.get("body") or "",
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
            no_force = no_delete = signed = False
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
                elif t == "required_signatures":
                    signed = True
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
                require_signed_commits=signed,
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
            require_signed_commits=bool((d.get("required_signatures") or {}).get("enabled")),
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

    async def review_threads(self, number: int) -> list[ReviewThread]:
        """Every review thread of a PR, with whether it is resolved (GraphQL only says that)."""
        owner, name = self.slug.split("/", 1)
        threads: list[ReviewThread] = []
        cursor: str | None = None
        for _ in range(20):  # 20 pages of 100 threads
            data = await self.api(
                "graphql",
                method="POST",
                body={
                    "query": _THREADS_QUERY,
                    "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor},
                },
            )
            page = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
            found = page.get("reviewThreads") or {}
            for t in found.get("nodes") or []:
                threads.append(
                    ReviewThread(
                        str(t.get("id", "")),
                        t.get("path") or "",
                        t.get("line") or t.get("originalLine"),
                        bool(t.get("isResolved")),
                        bool(t.get("isOutdated")),
                        tuple(
                            ReviewComment(
                                (c.get("author") or {}).get("login", ""),
                                c.get("body") or "",
                                _dt(c.get("createdAt")),
                                c.get("url") or "",
                            )
                            for c in (t.get("comments") or {}).get("nodes") or []
                        ),
                    )
                )
            info = found.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            cursor = info.get("endCursor")
        return threads

    async def revert_pr(self, number: int, title: str, body: str) -> PullRequest:
        """Open a pull request that reverts a merged one, as GitHub's Revert button does (it handles
        merge, squash and rebase merges alike). Nothing is merged."""
        node = (await self.api(f"repos/{self.slug}/pulls/{number}"))["node_id"]
        data = await self.api(
            "graphql",
            method="POST",
            body={
                "query": _REVERT_MUTATION,
                "variables": {"id": node, "title": title, "body": body},
            },
        )
        errors = (data or {}).get("errors")
        if errors:
            raise IntegrationError(
                f"GitHub could not revert PR #{number}: {errors[0].get('message', errors)}"
            )
        made = data["data"]["revertPullRequest"]["revertPullRequest"]
        return await self.get_pr(int(made["number"]))

    async def close_pr(self, number: int, comment: str) -> None:
        """Close a pull request without merging, saying why. Closing a closed one changes nothing."""
        pr = await self.get_pr(number)
        if pr.state != "open":
            return
        await self.api(f"repos/{self.slug}/issues/{number}/comments", method="POST", body={"body": comment})
        await self.api(f"repos/{self.slug}/pulls/{number}", method="PATCH", body={"state": "closed"})

    async def delete_branch(self, branch: str) -> None:
        """Delete a branch on GitHub. A branch that is already gone is fine."""
        try:
            await self.api(f"repos/{self.slug}/git/refs/heads/{branch}", method="DELETE")
        except NotFound:
            return
        except IntegrationError as exc:
            if exc.status == 422 and "does not exist" in str(exc):
                return
            raise


_REVERT_MUTATION = """
mutation($id: ID!, $title: String!, $body: String!) {
  revertPullRequest(input: {pullRequestId: $id, title: $title, body: $body}) {
    revertPullRequest { number url }
  }
}
"""

_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line originalLine
          comments(first: 50) { nodes { author { login } body createdAt url } }
        }
      }
    }
  }
}
"""
