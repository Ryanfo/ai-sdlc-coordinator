"""Project guidance for Claude: notes that apply to every ticket, not just one.

A ``FOR CLAUDE`` comment guides the next sessions of its own ticket. When the same correction
keeps coming up ("use the shared date helper", "never mock the database"), write it once as
``FOR CLAUDE project`` on any ticket: the coordinator adds it to the project's guidance file,
``docs/delivery/guidance.md`` on the ``delivery/guidance`` branch of the application repository,
and confirms on the ticket with a link. Every Claude session, on every ticket and every
developer's machine, gets that file as ``project_guidance`` in its envelope.

The file is plain Markdown under version control: edit or remove entries on GitHub (or with
any Git client) on that branch, and ``delivery guidance add "<text>"`` adds one from the
terminal. Each entry records the ticket and comment it came from, so adding is idempotent.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Container
from datetime import datetime
from pathlib import Path

from delivery.config import Config
from delivery.feedback import is_coordinator_comment
from delivery.git import ManagedRepo, blob_url
from delivery.journal import RunJournal, atomic_write_json, ensure_private_dir
from delivery.ports import GitHubPort, JiraComment, JiraPort
from delivery.publication import Publisher

log = logging.getLogger("delivery")
BRANCH = "delivery/guidance"
PATH = "docs/delivery/guidance.md"
HEADER = (
    "# Project guidance for Claude\n\n"
    "Every Claude session the delivery coordinator runs reads this file, on every ticket. Add to it\n"
    "with a Jira comment whose first line is `FOR CLAUDE project`, or `delivery guidance add`.\n"
    "Edit or remove entries here; keep each one short and about how to work in this codebase.\n"
)
_PROJECT_NOTE = re.compile(r"^FOR\s+CLAUDE\s+project\b\s*[:\-]?\s*(?P<text>.*)$", re.I)
DONE = "added.json"
_MARKER = re.compile(r"<!-- delivery-guidance: (?P<id>[^ ]+) -->")


def project_note(comment: JiraComment) -> str | None:
    """The guidance in a ``FOR CLAUDE project`` comment, or None."""
    if is_coordinator_comment(comment):
        return None
    first, _, rest = comment.body_text.strip().partition("\n")
    m = _PROJECT_NOTE.match(first.strip())
    if not m:
        return None
    text = "\n".join(t for t in (m.group("text").strip(), rest.strip()) if t)
    return text or None


def entry_ids(text: str) -> set[str]:
    return set(_MARKER.findall(text))


def entry(source: str, marker: str, text: str, when: datetime, by: str = "") -> str:
    who = f", by {by}" if by else ""
    title = f"## From {source} ({when:%d %b %Y}{who})"
    return f"\n{title}\n\n{text.strip()}\n\n<!-- delivery-guidance: {marker} -->\n"


async def current(repo: ManagedRepo) -> str | None:
    """The guidance file as published (None when there is none yet)."""
    data = await repo.show_file(f"origin/{BRANCH}", PATH)
    return data.decode(errors="replace") if data is not None else None


def url(cfg: Config, commit: str) -> str:
    return blob_url(cfg.repository.url, commit, PATH)


def edit_url(cfg: Config) -> str:
    return f"{cfg.repository.url.removesuffix('.git').rstrip('/')}/edit/{BRANCH}/{PATH}"


class Guidance:
    """Adds project notes to the guidance file and confirms them on their tickets."""

    def __init__(
        self,
        cfg: Config,
        jira: JiraPort,
        github: GitHubPort | None,
        repo: ManagedRepo,
    ) -> None:
        self.cfg = cfg
        self.jira = jira
        self.github = github
        self.repo = repo
        self.dir = cfg.runtime.state_dir / "guidance"

    def notes(
        self, comments: list[JiraComment], allowed_authors: Container[str]
    ) -> list[tuple[JiraComment, str]]:
        return [
            (c, text)
            for c in comments
            if c.author_account_id in allowed_authors and (text := project_note(c)) is not None
        ]

    async def add(self, entries: list[tuple[str, str, str, datetime, str]]) -> str | None:
        """Add ``(source, marker, text, when, by)`` entries not yet in the file; push. Returns
        the commit when something was added."""
        await self.repo.fetch()
        existing = await current(self.repo) or ""
        new = [e for e in entries if e[1] not in entry_ids(existing)]
        if not new:
            return None
        key = "-".join(sorted(e[1] for e in new))[:60]
        wt = self.cfg.repository.worktree_root / "_guidance" / re.sub(r"[^A-Za-z0-9_-]", "-", key)
        if wt.exists():
            await self.repo.remove_worktree(wt)
        start = f"origin/{BRANCH}" if existing else f"origin/{self.cfg.repository.base_branch}"
        await self.repo.add_worktree(wt, start=start, branch=BRANCH)
        try:
            dest = wt / PATH
            dest.parent.mkdir(parents=True, exist_ok=True)
            text = existing or HEADER
            text = text.rstrip("\n") + "\n" + "".join(entry(*e) for e in new)
            dest.write_text(text)
            journal = RunJournal(ensure_private_dir(self.dir))
            pub = Publisher(self.cfg, self.jira, self.github, self.repo, journal, "guidance")
            sources = ", ".join(sorted({e[0] for e in new}))
            message = f"Project guidance for Claude: from {sources}"
            sha = await pub.commit_and_push(wt, BRANCH, "guidance", message, revision=key, paths=[PATH])
            return sha or await self.repo.worktree_head(wt)
        finally:
            await self.repo.remove_worktree(wt)

    def _done(self) -> set[str]:
        """Comments already added from this machine: an entry removed from the file later is
        never added again."""
        try:
            return set(json.loads((self.dir / DONE).read_text()))
        except (OSError, ValueError):
            return set()

    def _record(self, ids: set[str]) -> None:
        atomic_write_json(ensure_private_dir(self.dir) / DONE, sorted(ids))

    async def collect(
        self, key: str, comments: list[JiraComment], allowed_authors: Container[str]
    ) -> list[str]:
        """Add the ticket's new ``FOR CLAUDE project`` notes; confirm each on the ticket. Returns
        the IDs of the comments added."""
        done = self._done()
        found = [(c, text) for c, text in self.notes(comments, allowed_authors) if c.id not in done]
        if not found:
            return []
        entries = [(key, c.id, text, c.created, c.author_name) for c, text in found]
        sha = await self.add(entries) or await self.repo.remote_sha(BRANCH)
        if sha is None:
            return []
        journal = RunJournal(ensure_private_dir(self.dir / key))
        pub = Publisher(self.cfg, self.jira, None, None, journal, f"guidance-{key}")
        link = url(self.cfg, sha)
        for c, _ in found:
            await pub.comment(
                key,
                "guidance",
                "**Added to the project guidance for Claude**: every Claude session, on every ticket, "
                f"now reads [the guidance file]({link}). Edit or remove entries there "
                f"([edit on GitHub]({edit_url(self.cfg)})).",
                c.id,
            )
        self._record(done | {c.id for c, _ in found})
        return [c.id for c, _ in found]


async def copy_for_run(repo: ManagedRepo, dest: Path) -> Path | None:
    """The guidance file in a run's inputs, or None when the project has none."""
    text = await current(repo)
    if not text:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    return dest


__all__ = ["BRANCH", "PATH", "Guidance", "copy_for_run", "current", "project_note"]
