"""A candidate under review that the base branch has moved past.

Verification tests each candidate on its own and merged with the base branch as it was then.
While the ticket waits in Code review or Acceptance review, other work merges into the base.
When that later work changes files this candidate also changes, or the candidate no longer
merges cleanly with it, what was verified may not hold any more. The coordinator says so once on
the ticket: what changed, which tickets it came from, and how to verify again on the latest base
(Submit follow-up changes re-verifies the same candidate). It is a warning; nothing waits for it.

It looks every ``CHECK_SECONDS`` and remembers what it said in ``<state_dir>/stale``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from delivery import console
from delivery.config import Config
from delivery.git import GitError, ManagedRepo
from delivery.intake import RecordCorrupt, load_context
from delivery.journal import RunJournal, atomic_write_json, ensure_private_dir
from delivery.models import SharedExecutionRecord, utcnow
from delivery.ownership import review_jql
from delivery.ports import IntegrationError, JiraPort, UncertainResult
from delivery.publication import PublicationError, PublicationUncertain, Publisher
from delivery.workflow import STATUS_NAMES, Status

log = logging.getLogger("delivery")
CHECK_SECONDS = 300
_KEY = re.compile(r"\b[A-Z][A-Z0-9_]{1,9}-\d{1,9}\b")


@dataclass
class Drift:
    """How the base moved on from the base a candidate was verified against."""

    verified_base: str
    base: str
    overlap: list[str] = field(default_factory=list)  # files both changed
    conflicts: list[str] = field(default_factory=list)  # files that no longer merge
    merged: list[str] = field(default_factory=list)  # commit subjects that touched them
    tickets: list[str] = field(default_factory=list)

    @property
    def kinds(self) -> list[str]:
        return [k for k, v in (("overlap", self.overlap), ("conflict", self.conflicts)) if v]


async def verified_base(repo: ManagedRepo, rec: SharedExecutionRecord) -> str | None:
    """The base commit the current candidate was verified against (its checks.json)."""
    ref = rec.artefacts.get("review")
    if not ref or "@" not in ref:
        return None
    path, commit = ref.rsplit("@", 1)
    data = await repo.show_file(commit, path.rsplit("/", 1)[0] + "/checks.json")
    if data is None:
        return None
    try:
        found = json.loads(data).get("base_sha")
    except ValueError:
        return None
    return str(found) if found else None


async def drift(repo: ManagedRepo, candidate: str, verified: str, base: str) -> Drift:
    out = Drift(verified, base)
    if verified == base:
        return out
    moved = set(await repo.diff_names(verified, base))
    mine = set(await repo.diff_names(verified, candidate))
    out.overlap = sorted(moved & mine)
    res = await repo.git(
        "merge-tree", "--write-tree", "--name-only", "--no-messages", base, candidate, check=False
    )
    if res.returncode == 1:
        out.conflicts = sorted({ln for ln in res.stdout.splitlines()[1:] if ln.strip()})
    touched = sorted(set(out.overlap) | set(out.conflicts))
    if touched:
        log_res = await repo.git("log", "--format=%s", f"{verified}..{base}", "--", *touched, check=False)
        out.merged = [s for s in log_res.stdout.splitlines() if s.strip()][:10]
        out.tickets = sorted({k for s in out.merged for k in _KEY.findall(s)})
    return out


def warning(n: int, base_branch: str, d: Drift, *, claude_resolves: bool = True) -> str:
    lines = [
        f"## Candidate c{n} may be out of date",
        f"`{base_branch}` has moved on since candidate c{n} was verified against "
        f"`{d.verified_base[:12]}` (now `{d.base[:12]}`).",
    ]
    if d.overlap:
        lines.append(
            f"The new commits change files this candidate also changes: {', '.join(d.overlap[:15])}."
        )
    if d.conflicts:
        lines.append(
            f"It no longer merges cleanly with `{base_branch}`: conflicts in "
            f"{', '.join(d.conflicts[:15])}. "
            "Resolve them when merging the pull request"
            + (
                ", or request code changes: the next development run merges the latest base first "
                "and Claude resolves the conflicts."
                if claude_resolves
                else "."
            )
        )
    if d.tickets:
        lines.append(f"Merged since, touching those files: {', '.join(d.tickets)}.")
    elif d.merged:
        lines.append("Merged since, touching those files: " + "; ".join(d.merged[:5]) + ".")
    lines += [
        "",
        f"**Nothing waits for this.** To check candidate c{n} against the latest `{base_branch}` "
        f"before deciding, choose **Submit follow-up changes** (moves into "
        f"**{STATUS_NAMES[Status.READY_VERIFICATION]}**): the same code is reviewed and verified "
        "again, merged with the latest base. Approvals of this candidate then need repeating.",
    ]
    return "\n".join(lines)


class Staleness:
    """Warns about candidates under review that the base branch has moved past."""

    def __init__(self, cfg: Config, jira: JiraPort, repo: ManagedRepo, emit: Callable[[str], None]) -> None:
        self.cfg = cfg
        self.jira = jira
        self.repo = repo
        self.emit = emit
        self.dir = cfg.runtime.state_dir / "stale"
        self._last: datetime | None = None

    def _said(self, key: str) -> dict[str, list[str]]:
        try:
            return dict(json.loads((self.dir / f"{key}.json").read_text()))
        except (OSError, ValueError):
            return {}

    async def tick(self, *, force: bool = False) -> None:
        now = utcnow()
        if not force and self._last is not None and now - self._last < timedelta(seconds=CHECK_SECONDS):
            return
        self._last = now
        try:
            issues = await self.jira.search(review_jql(self.cfg))
            if issues:
                await self.repo.fetch()
        except (IntegrationError, UncertainResult, GitError) as exc:
            log.info("staleness check skipped: %s", exc)
            return
        for issue in issues:
            try:
                await self.check(issue.key)
            except (
                IntegrationError,
                UncertainResult,
                GitError,
                RecordCorrupt,
                PublicationError,
                PublicationUncertain,
            ) as exc:
                log.warning("staleness check of %s failed: %s", issue.key, exc)

    async def check(self, key: str) -> Drift | None:
        ctx = await load_context(self.jira, self.cfg, key)
        rec = ctx.record
        candidate = rec.candidate_sha
        base_branch = self.cfg.repository.base_branch
        if not candidate or ctx.status not in (Status.CODE_REVIEW, Status.ACCEPTANCE_REVIEW):
            return None
        verified = await verified_base(self.repo, rec)
        base = await self.repo.remote_sha(base_branch)
        if not verified or not base:
            return None
        d = await drift(self.repo, candidate, verified, base)
        said = self._said(key)
        new = [k for k in d.kinds if k not in said.get(candidate, [])]
        if not new:
            return d
        journal = RunJournal(ensure_private_dir(self.dir / key))
        pub = Publisher(self.cfg, self.jira, None, None, journal, f"stale-{key}")
        await pub.comment(
            key,
            "stale",
            warning(rec.candidate_number, base_branch, d, claude_resolves=self.cfg.flow.resolve_conflicts),
            f"{candidate[:12]}-{'-'.join(new)}",
        )
        atomic_write_json(self.dir / f"{key}.json", {candidate: sorted({*said.get(candidate, []), *new})})
        self.emit(
            console.line(
                f"{key}: candidate c{rec.candidate_number} may be out of date: {base_branch} moved on and "
                + ("conflicts with it" if d.conflicts else "changes the same files")
            )
        )
        return d


__all__ = ["CHECK_SECONDS", "Drift", "Staleness", "drift", "verified_base", "warning"]
