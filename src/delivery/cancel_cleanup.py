"""Tidy what a cancelled ticket leaves in the repository.

When a ticket is cancelled in Jira its run is closed (see ``Supervisor._close_cancelled``), but
its pull request and branches would stay open for ever. This removes them, keeping only the
documents a person may want to read later:

* the open pull request is closed with a comment saying why, and the feature branch is deleted;
* the delivery branch keeps the specification and plan (and a read-only investigation's
  findings) and loses everything else the coordinator wrote there. The newest of each gets a
  short note at the top saying the ticket was cancelled;
* the run's local worktrees and branches go too (changes never pushed are saved as a patch).

A ticket with a merged pull request is left alone: that code shipped.

Every step can be repeated, so a failure (GitHub down, a protected branch) is logged and tried
again on the next poll; nothing here ever blocks the poll or the Jira side of the cancel.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from delivery.cleanup import save_unpushed
from delivery.git import GitError
from delivery.journal import RunEntry
from delivery.models import utcnow
from delivery.publication import Publisher
from delivery.runtime import Deps

log = logging.getLogger(__name__)

MARKER = "<!-- delivery: ticket cancelled -->"
# What stays on the delivery branch: revisions of these documents, nothing beside them.
KEPT_FOLDERS = ("specification", "plan", "findings")
_REVISION = re.compile(r"v(\d{3,4})\.md$")


def banner(key: str) -> str:
    return (
        f"{MARKER}\n"
        f"> **Cancelled.** {key} was cancelled in Jira on {utcnow():%d %b %Y}. Work stopped there; "
        "this document is kept for reference only. Its pull request and other files were removed.\n\n"
    )


def split_docs(doc_root: str, names: list[str]) -> tuple[list[str], list[str]]:
    """Split the files under ``doc_root`` into those to keep and those to remove."""
    keep, drop = [], []
    for name in names:
        rel = name[len(doc_root) + 1 :] if name.startswith(f"{doc_root}/") else name
        folder, _, rest = rel.partition("/")
        if folder in KEPT_FOLDERS and "/" not in rest and _REVISION.fullmatch(rest):
            keep.append(name)
        else:
            drop.append(name)
    return keep, drop


def latest_of_each(doc_root: str, keep: list[str]) -> list[str]:
    """The newest revision of each kept document."""
    newest: dict[str, tuple[int, str]] = {}
    for name in keep:
        folder = name[len(doc_root) + 1 :].partition("/")[0]
        m = _REVISION.search(name)
        assert m
        rev = int(m.group(1))
        if folder not in newest or rev > newest[folder][0]:
            newest[folder] = (rev, name)
    return sorted(n for _, n in newest.values())


async def tidy(deps: Deps, entry: RunEntry, emit: Callable[[str], None]) -> str:
    """Tidy one cancelled ticket. Returns what was done; raises if it has to be tried again."""
    rec = entry.record
    assert rec is not None
    key = rec.ticket_key
    repo, github = deps.repo, deps.github
    feature, delivery = f"feature/{key}", f"delivery/{key}"
    doc_root = f"docs/delivery/{key}"

    prs = [*await github.find_prs(feature), *await github.find_prs(delivery)]
    if any(p.merged for p in prs):
        return "a pull request of this ticket was merged; left as it is"

    done: list[str] = []
    for pr in prs:
        if pr.state == "open":
            await github.close_pr(
                pr.number,
                f"Closed by the delivery coordinator: {key} was cancelled in Jira. "
                f"The specification and plan stay on `{delivery}`.",
            )
            done.append(f"closed PR #{pr.number}")

    await repo.fetch()
    if await repo.remote_sha(feature):
        await github.delete_branch(feature)
        done.append(f"deleted {feature}")

    if await repo.remote_sha(delivery):
        names = await repo.ls_tree(f"origin/{delivery}", f"{doc_root}/")
        keep, drop = split_docs(doc_root, names)
        if not keep:
            await github.delete_branch(delivery)
            done.append(f"deleted {delivery} (no specification or plan was written)")
        elif await _trim_docs(deps, entry, delivery, doc_root, keep, drop):
            done.append(f"kept the specification and plan on {delivery}")

    await _remove_local(deps, entry, (feature, delivery), emit)
    await repo.fetch()
    return ", ".join(done) or "nothing left to tidy"


async def _trim_docs(
    deps: Deps, entry: RunEntry, branch: str, doc_root: str, keep: list[str], drop: list[str]
) -> bool:
    """Remove the other files from the delivery branch and mark the newest documents."""
    rec = entry.record
    assert rec is not None
    repo = deps.repo
    marked = latest_of_each(doc_root, keep)
    pending = []
    for name in marked:
        data = await repo.show_file(f"origin/{branch}", name)
        if data is not None and MARKER not in data.decode(errors="replace")[:200]:
            pending.append(name)
    if not pending and not drop:
        return False
    path = deps.cfg.repository.worktree_root / rec.ticket_key / rec.run_id / "cancel"
    shutil.rmtree(path, ignore_errors=True)
    await repo.add_worktree(path, start=f"origin/{branch}")
    try:
        for name in drop:
            (path / name).unlink(missing_ok=True)
        for name in pending:
            file = path / name
            file.write_text(banner(rec.ticket_key) + file.read_text())
        pub = Publisher(deps.cfg, deps.jira, deps.github, repo, entry.journal, rec.run_id)
        await pub.commit_and_push(
            path,
            branch,
            "cancel-tidy",
            f"{rec.ticket_key}: ticket cancelled; keep only the specification and plan",
            paths=[doc_root],
        )
    finally:
        await repo.remove_worktree(path)
    return True


async def _remove_local(
    deps: Deps, entry: RunEntry, branches: tuple[str, ...], emit: Callable[[str], None]
) -> None:
    """Remove the run's worktrees (saving unpushed changes first) and the local branches."""
    rec = entry.record
    assert rec is not None
    repo = deps.repo
    ticket_dir: Path = deps.cfg.repository.worktree_root / rec.ticket_key
    for run_dir in sorted(c for c in ticket_dir.iterdir() if c.is_dir()) if ticket_dir.is_dir() else []:
        for child in sorted(c for c in run_dir.iterdir() if c.is_dir()):
            if (child / ".git").exists():
                dest = entry.journal.dir / f"leftover-{run_dir.name}-{child.name}.patch"
                kept = await save_unpushed(repo, child, dest)
                if kept:
                    emit(f"{rec.ticket_key}: {kept}")
            try:
                await repo.remove_worktree(child)
            except GitError as exc:
                emit(f"{rec.ticket_key}: could not remove {child}: {exc}")
        if not any(run_dir.iterdir()):
            run_dir.rmdir()
    if ticket_dir.is_dir() and not any(ticket_dir.iterdir()):
        ticket_dir.rmdir()
    for branch in branches:
        # Fails harmlessly when an older run's worktree still has the branch checked out.
        await repo.git("branch", "-D", branch, check=False)
