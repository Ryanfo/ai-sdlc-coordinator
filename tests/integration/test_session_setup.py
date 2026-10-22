"""Sessions that run commands start with the application's dependencies already installed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import World, make_world, step

KEY = "PILOT-1"


def _setup(record: Path) -> list[str]:
    """Stands in for `yarn install`: says where it ran and rewrites a tracked file."""
    return ["sh", "-c", f'pwd -P >> "{record}" && echo rewritten >> README.md']


def _invocation(w: World, procedure: str) -> dict:  # type: ignore[type-arg]
    return [i for i in w.invocations() if i["procedure"] == procedure][-1]


def _settings(w: World, procedure: str) -> dict:  # type: ignore[type-arg]
    path = sorted(Path(w.cfg.runtime.state_dir).rglob(f"settings-{procedure}.json"))[-1]
    return dict(json.loads(path.read_text()))


async def test_dependencies_are_installed_before_development_without_touching_tracked_files(
    tmp_path: Path,
) -> None:
    record = tmp_path / "installed-in"
    setup = _setup(record)
    w = make_world(
        tmp_path,
        extra={"checks": {"setup": setup}, "claude": {"network_domains": ["registry.example.test"]}},
    )
    w.scenario({"implement-ticket": [{"edit": {"src/app.ts": "export const x = 2;\n"}}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.move(KEY, Status.READY_PLANNING)
        await step(sup)
        w.move(KEY, Status.READY_DEVELOPMENT)
        await step(sup)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.CODE_REVIEW, w.last_comment(KEY)

    # The first install was in the development session's own working copy (the coordinator's
    # checks install again later, in their own trees).
    implement = _invocation(w, "implement-ticket")
    assert record.read_text().splitlines()[0] == str(Path(implement["cwd"]).resolve())
    # The setup's change to a tracked file was put back: only Claude's change is in the candidate.
    readme = subprocess.run(
        ["git", "--git-dir", str(w.origin), "show", f"feature/{KEY}:README.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert readme == "app\n"

    env = w.envelope("implement-ticket")
    assert env["setup_command"] == setup and env["check_commands"] == {"unit": ["true"]}
    domains = _settings(w, "implement-ticket")["sandbox"]["network"]["allowedDomains"]
    assert domains == ["registry.npmjs.org", "registry.example.test"]
    # Sessions that only read get neither the install nor the network.
    assert _settings(w, "refine-ticket")["sandbox"]["network"]["allowedDomains"] == []


async def test_a_failed_install_does_not_stop_development(tmp_path: Path) -> None:
    w = make_world(tmp_path, extra={"checks": {"setup": ["sh", "-c", "exit 3"]}})
    w.scenario({"implement-ticket": [{"edit": {"src/app.ts": "export const x = 2;\n"}}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.move(KEY, Status.READY_PLANNING)
        await step(sup)
        w.move(KEY, Status.READY_DEVELOPMENT)
        await step(sup)
    assert _invocation(w, "implement-ticket")
    logs = list(Path(w.cfg.runtime.state_dir).rglob("session-implement-ticket-setup.log"))
    assert logs, "the failed install's output is kept"
