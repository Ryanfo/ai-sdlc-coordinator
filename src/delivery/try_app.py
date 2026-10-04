"""`delivery try <ticket>`: run a ticket's candidate on this machine and open it in the browser.

For anyone with the delivery tools and the team's settings: the person deciding acceptance, a
code reviewer, the developer. No supervisor, Claude session or Jira transition is involved. It
reads the ticket's current candidate from its shared record in Jira (or, when Jira cannot be
read, the head of ``feature/<ticket>``), checks it out in a worktree of its own, runs
``[preview]`` setup, seed and command there with the app's output in this terminal, and opens
the browser once the app answers. Ctrl-C stops the app and removes the worktree.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable, Callable

from delivery.config import Config
from delivery.git import GitError, ManagedRepo
from delivery.journal import ensure_private_dir
from delivery.models import PROPERTY_KEY
from delivery.ports import IntegrationError, JiraPort
from delivery.preview import answers, expand, launcher, open_url
from delivery.proc import base_child_env
from delivery.resources import PortRegistry, ResourceExhausted


class TryError(Exception):
    """The candidate cannot be run (the message says why and what to do)."""


async def find_candidate(
    jira: JiraPort | None, repo: ManagedRepo, key: str, ref: str | None = None
) -> tuple[str, str]:
    """(commit, description) of what to run: ``ref``, the recorded candidate, or the branch head."""
    if ref:
        sha = await repo.resolve(ref) or await repo.resolve(f"origin/{ref}")
        if not sha:
            raise TryError(f"{ref} is not a commit or branch of the application repository")
        return sha, ref
    note = ""
    if jira is not None:
        try:
            raw = await jira.get_property(key, PROPERTY_KEY)
        except IntegrationError as exc:
            raw, note = None, f" (Jira could not be read: {exc})"
        if raw and raw.get("candidate_sha"):
            return str(raw["candidate_sha"]), f"candidate c{raw.get('candidate_number', '?')}"
    sha = await repo.remote_sha(f"feature/{key}")
    if sha:
        return sha, f"the head of feature/{key}{note}"
    raise TryError(f"{key} has no implementation candidate yet{note}")


async def try_candidate(
    cfg: Config,
    key: str,
    *,
    repo: ManagedRepo,
    jira: JiraPort | None,
    ref: str | None = None,
    keep: bool = False,
    out: Callable[[str], None] = print,
    opener: Callable[[str], Awaitable[str | None]] = open_url,
    stop: asyncio.Event | None = None,
) -> int:
    """Run the candidate until the app exits or ``stop`` is set (Ctrl-C cancels). Returns the
    app's exit code (0 when stopped)."""
    pc = cfg.preview
    if not pc.enabled:
        raise TryError(
            "no app command is configured: add [preview] command = [...] to your config (or the "
            'team\'s project file), for example ["npm", "run", "dev"]'
        )
    await repo.ensure()
    sha, what = await find_candidate(jira, repo, key, ref)
    pid = os.getpid()
    wt = cfg.repository.worktree_root / key / f"try-{sha[:12]}-{pid}" / "app"
    folder = ensure_private_dir(cfg.runtime.state_dir / "try" / f"{key}-{pid}")
    try:
        port = PortRegistry().allocate("try", ("app",))["app"]
    except ResourceExhausted as exc:
        raise TryError(str(exc)) from None
    out(f"{key}: checking out {what} ({sha[:12]}) in {wt}")
    try:
        await repo.add_worktree(wt, start=sha)
    except GitError as exc:
        raise TryError(f"could not check out {sha[:12]}: {exc}") from None
    log = folder / "app.log"
    script = folder / "app.sh"
    setup = cfg.checks.setup if pc.setup is None else pc.setup
    script.write_text(
        launcher(expand(setup, port), expand(pc.command, port), wt, log, seed=expand(pc.seed, port))
    )
    script.chmod(0o700)
    url = pc.url.replace("{port}", str(port))
    env = base_child_env({"PORT": str(port), "DELIVERY_PORT_APP": str(port), "BROWSER": "none"})
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", str(script), cwd=wt, env=env, start_new_session=True
    )
    out(f"{key}: starting the app at {url} (its output follows; Ctrl-C stops it)")
    ready = False
    try:
        while proc.returncode is None and not (stop and stop.is_set()):
            if not ready and await asyncio.to_thread(answers, url):
                ready = True
                problem = await opener(url) if pc.open_browser else None
                out(
                    f"{key}: the app is running at {url}"
                    + (f" (could not open your browser: {problem})" if problem else "")
                    + ". Ctrl-C stops it."
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=1)
        return proc.returncode or 0
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=10)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
        if keep:
            out(f"{key}: kept the worktree {wt}")
        else:
            with contextlib.suppress(GitError, OSError):
                await repo.remove_worktree(wt)
                for parent in (wt.parent, wt.parent.parent):
                    if parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()
            out(f"{key}: stopped the app and removed its worktree (the app's log: {log})")


__all__ = ["TryError", "find_candidate", "try_candidate"]
