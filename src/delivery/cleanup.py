"""`coordinator clean`: find and remove what finished runs left on disk.

What it removes:

* worktrees of runs that have finished (waiting for a human, blocked, failed or done) and are
  not kept for a Claude session open for questions. A worktree with changes that were never
  pushed is saved as a patch in the run's folder first, so nothing is lost;
* empty folders under the worktree root;
* with ``--older-than DAYS``, the local logs of tickets whose runs all finished more than that
  many days ago. Jira and Git keep the durable record; these are only local copies.

What it never touches: runs that are still working, interrupted or held (resume or recover
them first), corrupt run records, and worktrees of sessions open for questions. Sessions whose
tmux session has gone are tidied by the running coordinator; when none is running, clean does
the same (it keeps the conversation and any changes, as the coordinator would).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from delivery.config import Config
from delivery.git import GitError, ManagedRepo
from delivery.journal import JournalStore, RunEntry
from delivery.models import RunState, utcnow
from delivery.open_sessions import OpenRecord, SessionRegistry

FINISHED = frozenset(
    {RunState.AWAITING_HUMAN, RunState.COMPLETED, RunState.FAILED, RunState.BLOCKED, RunState.CANCELLED}
)


@dataclass
class Item:
    path: Path
    size: int
    why: str
    run: RunEntry | None = None


@dataclass
class Plan:
    worktrees: list[Item] = field(default_factory=list)
    empty: list[Path] = field(default_factory=list)
    logs: list[Item] = field(default_factory=list)
    kept: list[Item] = field(default_factory=list)
    dead_sessions: list[OpenRecord] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(i.size for i in self.worktrees + self.logs)

    @property
    def empty_plan(self) -> bool:
        return not (self.worktrees or self.empty or self.logs or self.dead_sessions)


def size_of(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def plan(cfg: Config, live_sessions: set[str], older_than_days: int | None = None) -> Plan:
    """What clean would do. ``live_sessions`` are the tmux sessions that are still running."""
    store = JournalStore(cfg.runtime.state_dir, cfg.identity_key)
    runs = {e.run_id: e for e in store.iter_runs()}
    open_records = SessionRegistry(cfg.runtime.state_dir).all()
    held = {w for r in open_records if r.name in live_sessions for w in r.worktrees}
    from delivery.acceptance import AcceptanceStore

    accepting = {st.worktree for st in AcceptanceStore(cfg.runtime.state_dir).all()}
    out = Plan(dead_sessions=[r for r in open_records if r.name not in live_sessions])
    dead_held = {w for r in out.dead_sessions for w in r.worktrees}
    root = cfg.repository.worktree_root
    for ticket_dir in sorted(root.iterdir()) if root.is_dir() else []:
        if not ticket_dir.is_dir() or ticket_dir.name.startswith("_"):
            continue
        for run_dir in sorted(ticket_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            children = [c for c in run_dir.iterdir()]
            if not children:
                out.empty.append(run_dir)
                continue
            entry = runs.get(run_dir.name)
            state = entry.record.state if entry and entry.record else None
            item = Item(run_dir, size_of(run_dir), "", entry)
            if any(str(c) in held for c in children):
                item.why = "kept for a Claude session open for questions"
                out.kept.append(item)
            elif any(str(c) in accepting for c in children):
                item.why = "kept for the app of a ticket in Acceptance review"
                out.kept.append(item)
            elif any(str(c) in dead_held for c in children):
                item.why = "its open session has ended; tidied with that session"
                out.kept.append(item)
            elif entry is None:
                item.why = "no run of this name is recorded on this machine"
                out.worktrees.append(item)
            elif entry.error is not None:
                item.why = f"run record is corrupt ({entry.error.detail}); inspect it first"
                out.kept.append(item)
            elif state not in FINISHED or entry.journal.pending_ops():
                item.why = f"run is {state.value if state else '?'}; resume or recover it first"
                out.kept.append(item)
            else:
                item.why = f"run finished ({state.value})"
                out.worktrees.append(item)
        if ticket_dir.is_dir() and not any(ticket_dir.iterdir()):
            out.empty.append(ticket_dir)
    if older_than_days is not None:
        out.logs = _old_logs(store, older_than_days, {r.ticket_key for r in open_records})
    return out


def _old_logs(store: JournalStore, days: int, open_tickets: set[str]) -> list[Item]:
    cutoff = utcnow() - timedelta(days=days)
    by_ticket: dict[str, list[RunEntry]] = {}
    for e in store.iter_runs():
        by_ticket.setdefault(e.ticket_key, []).append(e)
    out = []
    for key, entries in sorted(by_ticket.items()):
        if key in open_tickets:
            continue
        if any(
            e.record is None or e.record.state not in FINISHED or e.journal.pending_ops() for e in entries
        ):
            continue
        last = max((e.record.updated_at for e in entries if e.record), default=None)
        if last is None or last > cutoff:
            continue
        folder = store.runs_dir / key
        out.append(Item(folder, size_of(folder), f"all runs finished; last {last.astimezone():%d %b %Y}"))
    return out


async def clean_expired(
    cfg: Config, repo: ManagedRepo, days: int, keep_tickets: set[str], emit: Callable[[str], None]
) -> int:
    """What the running coordinator removes by itself once a day (``runtime.retention_days``).

    Like ``coordinator clean --older-than DAYS``, but only what has been finished for longer than
    ``days``: worktrees of runs that ended before then (unpushed changes are kept as a patch
    first) and the logs of tickets whose runs all did. Nothing of a ticket in ``keep_tickets``,
    of a session open for questions, or of a run that is not finished is touched. Returns how
    many tickets lost something.
    """
    live = {r.name for r in SessionRegistry(cfg.runtime.state_dir).all()}
    p = plan(cfg, live, days)
    cutoff = utcnow() - timedelta(days=days)

    def old(item: Item) -> bool:
        rec = item.run.record if item.run else None
        return rec is not None and rec.updated_at < cutoff and rec.ticket_key not in keep_tickets

    p.worktrees = [i for i in p.worktrees if old(i)]
    p.logs = [i for i in p.logs if i.path.name not in keep_tickets]
    p.dead_sessions = []
    if not (p.worktrees or p.logs or p.empty):
        return 0
    await apply(cfg, repo, p, emit)
    return len({i.path.parent.name for i in p.worktrees} | {i.path.name for i in p.logs})


async def save_unpushed(repo: ManagedRepo, worktree: Path, dest: Path) -> str:
    """Keep uncommitted or unpushed changes of a worktree as a patch before it is removed."""
    try:
        base = await repo.worktree_head(worktree)
        branch = await repo.git("symbolic-ref", "-q", "--short", "HEAD", cwd=worktree, check=False)
        if branch.stdout.strip():
            # Commits the coordinator never pushed count too.
            pushed = await repo.git(
                "rev-parse", "--verify", "-q", f"origin/{branch.stdout.strip()}", cwd=worktree, check=False
            )
            base = pushed.stdout.strip() or base
        files = await repo.changed_paths(worktree, base)
    except (GitError, OSError):
        return ""
    if not files:
        return ""
    await repo.git("add", "-A", cwd=worktree)
    dest.parent.mkdir(parents=True, exist_ok=True)
    await repo.git("diff", "--cached", "--binary", f"--output={dest}", base, cwd=worktree)
    dest.chmod(0o600)
    return f"{len(files)} changed files saved to {dest}"


async def apply(cfg: Config, repo: ManagedRepo, p: Plan, emit: Callable[[str], None]) -> None:
    for item in p.worktrees:
        for child in sorted(c for c in item.path.iterdir() if c.is_dir()):
            if (child / ".git").exists():
                folder = (
                    item.run.journal.dir if item.run else cfg.runtime.state_dir / "leftovers" / item.path.name
                )
                kept = await save_unpushed(repo, child, folder / f"leftover-{child.name}.patch")
                if kept:
                    emit(f"  {item.path.parent.name}: {kept}")
            try:
                await repo.remove_worktree(child)
            except GitError as exc:
                emit(f"  could not remove {child}: {exc}")
        shutil.rmtree(item.path, ignore_errors=True)
    for path in p.empty:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    for item in p.logs:
        shutil.rmtree(item.path, ignore_errors=True)
        intake = cfg.runtime.state_dir / "intake" / item.path.name
        shutil.rmtree(intake, ignore_errors=True)
    root = cfg.repository.worktree_root
    for ticket_dir in sorted(root.iterdir()) if root.is_dir() else []:
        if ticket_dir.is_dir() and not ticket_dir.name.startswith("_") and not any(ticket_dir.iterdir()):
            ticket_dir.rmdir()
    if repo.git_dir.exists():
        await repo.git("worktree", "prune", check=False)
