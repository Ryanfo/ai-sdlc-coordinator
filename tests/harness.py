"""End-to-end harness: real supervisor + real Git + fake Jira/GitHub + fake claude."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conftest import APPROVER, DEV, STATUS_IDS, base_sections, render_config
from delivery.claude import ClaudeRunner
from delivery.config import Config, load_config
from delivery.git import ManagedRepo
from delivery.interactive import InteractiveRunner
from delivery.journal import JournalStore
from delivery.models import PROPERTY_KEY, SharedExecutionRecord
from delivery.ownership import RepoLocks
from delivery.plugin import load_plugin
from delivery.runtime import Deps
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from fakes.github import FakeGitHub
from fakes.jira import FakeJira
from gitutil import make_origin

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "delivery"
FAKE = Path(__file__).parent / "fakes" / "fake_claude.py"
REVIEWER = "reviewer"


@dataclass
class World:
    tmp: Path
    cfg: Config
    jira: FakeJira
    github: FakeGitHub
    repo: ManagedRepo
    deps: Deps
    scenario_path: Path
    origin: Path

    def scenario(self, data: dict[str, Any]) -> None:
        self.scenario_path.write_text(json.dumps(data))

    def invocations(self) -> list[dict[str, Any]]:
        d = self.scenario_path.parent / "invocations"
        return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.exists() else []

    def envelope(self, procedure: str) -> dict[str, Any]:
        """The input envelope of the latest run of ``procedure``."""
        inv = [i for i in self.invocations() if f"/delivery:{procedure}" in " ".join(i["argv"])][-1]
        prompt = inv["argv"][inv["argv"].index("-p") + 1]
        return dict(json.loads(Path(prompt.split(" ", 1)[1].split("\n", 1)[0]).read_text()))

    def record(self, key: str) -> SharedExecutionRecord:
        return SharedExecutionRecord.model_validate(self.jira.issues[key].properties[PROPERTY_KEY])

    def comments(self, key: str) -> list[str]:
        return [c.body_text for c in self.jira.comments_by_key[key]]

    def last_comment(self, key: str) -> str:
        return self.comments(key)[-1]

    def new_ticket(self, key: str, assignee: str = DEV, **kw: Any) -> None:
        self.jira.create(
            key,
            kw.pop("summary", f"Feature {key}"),
            kw.pop("description", "Users need search. AC1: title search is case-insensitive."),
            assignee,
            **kw,
        )

    def submit(self, key: str, author: str = DEV) -> None:
        self.jira.human_move(key, Status.READY_REFINEMENT, author)

    def move(self, key: str, target: Status, author: str = APPROVER) -> None:
        """A decision: the Jira move alone."""
        self.jira.human_move(key, target, author)

    def decide(self, key: str, target: Status, text: str, author: str = APPROVER) -> None:
        """Say something in a comment (what to change, an answer), then make the move."""
        self.jira.human_comment(key, author, text)
        self.jira.human_move(key, target, author)

    def token(self, key: str, kind: str) -> str:
        rec = self.record(key)
        live = [g for g in rec.gates if g.kind.value == kind and g.state.value != "superseded"]
        return max(live, key=lambda g: g.revision).token


def make_world(
    tmp: Path,
    *,
    account: str = DEV,
    checks: dict[str, list[str]] | None = None,
    jira: FakeJira | None = None,
    github: FakeGitHub | None = None,
    origin: Path | None = None,
    name: str = "w",
    extra: dict[str, dict[str, Any]] | None = None,
    interactive: bool = False,
) -> World:
    base = tmp / name
    base.mkdir(parents=True, exist_ok=True)
    origin = origin or make_origin(tmp)
    scenario_path = base / "claude" / "scenario.json"
    scenario_path.parent.mkdir(parents=True)
    scenario_path.write_text("{}")
    wrapper = base / "bin" / "claude"
    wrapper.parent.mkdir()
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" --scenario "{scenario_path}" "$@"\n')
    wrapper.chmod(0o755)
    sections = base_sections(base, account)
    sections["repository"]["url"] = "https://github.com/example/app.git"
    sections["claude"].update({"plugin_path": str(PLUGIN), "executable": str(wrapper)})
    sections["runtime"].update({"heartbeat_seconds": 5, "check_timeout_seconds": 60, "timeout_seconds": 60})
    sections["checks.commands"] = checks or {"unit": ["true"]}
    sections["checks.ci"] = {"required_names": ["unit"]}
    sections["approvals"] = {"jira_account_ids": [APPROVER], "github_logins": [REVIEWER]}
    # The fake Jira's clock is not real time: reminders are tested on their own.
    sections["reminders"] = {"after_hours": 0}
    if interactive:
        # A private tmux server per test; no terminal windows.
        sections["claude.interactive"] = {
            "enabled": True,
            "window": "none",
            "socket": f"dlvtest-{tmp.name}"[-40:],
        }
    for sec, body in (extra or {}).items():
        sections.setdefault(sec, {}).update(body)
    (base / "delivery.toml").write_text(render_config(sections, {"config_version": 1}))
    cfg = load_config(base / "delivery.toml")
    jira = jira or FakeJira(STATUS_IDS, me=account)
    github = github or FakeGitHub(origin, auto_ci={"unit": "success"})
    locks = RepoLocks(cfg.runtime.state_dir / "locks")
    # The managed clone uses the local origin path in place of the GitHub URL.
    repo = ManagedRepo(str(origin), "main", cfg.repository.worktree_root, locks)
    store = JournalStore(cfg.runtime.state_dir, cfg.identity_key)
    runner: ClaudeRunner | InteractiveRunner = (
        InteractiveRunner(
            str(wrapper),
            cfg.claude.interactive,
            cfg.runtime.state_dir,
            worktree_root=cfg.repository.worktree_root,
        )
        if cfg.claude.interactive.enabled
        else ClaudeRunner(str(wrapper))
    )
    deps = Deps(cfg, jira, github, repo, runner, store, load_plugin(PLUGIN), locks)
    return World(tmp, cfg, jira, github, repo, deps, scenario_path, origin)


async def drain(sup: Supervisor, timeout: float = 60) -> None:
    while sup.sessions:
        tasks = [s.task for s in sup.sessions.values()]
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            raise AssertionError(f"sessions still running: {[t.get_name() for t in pending]}")


async def step(sup: Supervisor) -> list[str]:
    """One poll then wait for every dispatched session to reach its stage outcome."""
    if not sup.deps.repo.git_dir.exists():
        await sup.deps.repo.ensure()
    report = await sup.poll_once()
    await drain(sup)
    return report.started
