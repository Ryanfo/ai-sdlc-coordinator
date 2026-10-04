"""Acceptance review: how to try the approved candidate, what to check, and the app itself.

Real supervisor, Git (and tmux for the app); fake Jira, GitHub and Claude.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

from delivery.acceptance import AcceptanceStore, guide_text
from delivery.cli import main
from delivery.supervisor import Supervisor
from delivery.try_app import TryError, try_candidate
from delivery.workflow import Status
from harness import REVIEWER, World, make_world, step

KEY = "PILOT-1"
GUIDE = (
    "# Acceptance guide\n\n## Before you start\nNothing.\n\n"
    "## AC1: title search\n1. Open the task list\n2. Type `milk` in the search box\n"
    "3. **You should see**: only the task called Milk\n"
)
SERVER = [sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"]
needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


def _get(url: str) -> str | None:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=2) as r:
            return str(r.read().decode())
    except OSError:
        return None


async def _until(sup: Supervisor, cond: Callable[[], bool], timeout: float = 30) -> None:
    for _ in range(int(timeout * 5)):
        await sup.acceptance.tick(full=False)
        if cond():
            return
        await asyncio.sleep(0.2)
    raise AssertionError("condition not met in time")


async def _to_code_review(w: World, sup: Supervisor) -> str:
    w.new_ticket(KEY)
    w.submit(KEY)
    await step(sup)
    w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
    await step(sup)
    w.decide(KEY, f"APPROVE PLAN {w.token(KEY, 'PLAN')}", Status.READY_DEVELOPMENT)
    await step(sup)
    await step(sup)
    assert w.jira.status_of(KEY) is Status.CODE_REVIEW, w.last_comment(KEY)
    rec = w.record(KEY)
    assert rec.pr_number and rec.candidate_sha
    return rec.candidate_sha


def _approve_code(w: World) -> None:
    rec = w.record(KEY)
    assert rec.pr_number
    w.github.approve(rec.pr_number, REVIEWER)
    w.decide(KEY, f"APPROVE CODE {w.token(KEY, 'CODE')}", Status.ACCEPTANCE_REVIEW)


async def test_acceptance_review_says_how_to_try_the_candidate_and_what_to_check(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"verify-ticket": [{"outputs": {"acceptance-guide.md": GUIDE}}]})
    async with Supervisor(w.deps) as sup:
        await _to_code_review(w, sup)
        # Verification published its guide next to the report.
        ref = w.record(KEY).artefacts["acceptance_guide"]
        assert ref.startswith(f"docs/delivery/{KEY}/reviews/") and ref.split("@")[0].endswith(
            "acceptance-guide.md"
        )
        _approve_code(w)
        await sup.acceptance.tick(full=True)
        text = w.last_comment(KEY)
        assert "Ready for acceptance: candidate c1" in text
        assert "AC1: title search" in text and "only the task called Milk" in text
        assert "# Acceptance guide" not in text and "delivery provenance" not in text
        assert f"ACCEPT DELIVERY {w.token(KEY, 'ACCEPT')}" in text
        assert f"CHANGE ACCEPTANCE {w.token(KEY, 'ACCEPT')}" in text
        # No app is configured: nothing runs here and `delivery try` is not offered.
        assert "delivery try" not in text and "test-laptop" not in text
        # Once per entry into Acceptance review.
        before = len(w.comments(KEY))
        await sup.acceptance.tick(full=True)
        assert len(w.comments(KEY)) == before
        assert AcceptanceStore(w.cfg.runtime.state_dir).load(KEY) is not None
        # Accepted: it leaves Acceptance review and its record goes.
        w.decide(KEY, f"ACCEPT DELIVERY {w.token(KEY, 'ACCEPT')}", Status.READY_RELEASE_PREPARATION)
        await sup.acceptance.tick(full=True)
        assert AcceptanceStore(w.cfg.runtime.state_dir).load(KEY) is None


@needs_tmux
async def test_the_approved_candidate_runs_while_the_ticket_is_in_acceptance_review(tmp_path: Path) -> None:
    w = make_world(
        tmp_path,
        extra={
            "preview": {
                "command": SERVER,
                "setup": [],
                "seed": ["sh", "-c", "echo 'demo data on {port}' > seeded.txt"],
                "url": "http://127.0.0.1:{port}/",
            },
            "claude.interactive": {"socket": f"dlvacc-{tmp_path.name}"[-40:]},
        },
    )
    opened: list[str] = []

    async def browser(url: str) -> str | None:
        opened.append(url)
        return None

    def app():  # type: ignore[no-untyped-def]
        return AcceptanceStore(w.cfg.runtime.state_dir).load(KEY)

    try:
        async with Supervisor(w.deps) as sup:
            sup.acceptance.apps.opener = browser
            await _to_code_review(w, sup)
            _approve_code(w)
            await sup.acceptance.tick(full=True)
            text = w.last_comment(KEY)
            assert "On test-laptop" in text and f"delivery try {KEY} runs candidate c1" in text
            await _until(
                sup, lambda: (a := app()) is not None and a.preview is not None and a.preview.state == "ready"
            )
            st = app()
            assert st is not None and st.preview is not None
            url = st.preview.url
            assert opened == [url]
            # The exact candidate, with the seed run before the app started.
            assert _get(url + "src/pilot-1.ts") == "export const pilot_1 = true;\n"
            port = url.rsplit(":", 1)[1].strip("/")
            assert _get(url + "seeded.txt") == f"demo data on {port}\n"
            assert "acceptance-c1" in st.worktree and Path(st.worktree).is_dir()
            # `delivery clean` keeps its worktree while the review lasts.
            from delivery.cleanup import plan

            assert not [i for i in plan(w.cfg, set()).worktrees if "acceptance" in str(i.path)]

            # It stopped: `delivery preview` asks the coordinator to start it again.
            subprocess.run(
                ["tmux", "-L", w.cfg.claude.interactive.socket, "kill-session", "-t", f"={st.preview.name}"],
                capture_output=True,
            )
            await _until(
                sup,
                lambda: (a := app()) is not None and a.preview is not None and a.preview.state == "stopped",
            )
            assert main(["preview", KEY, "--config", str(w.cfg.source_path)]) == 0
            await _until(
                sup, lambda: (a := app()) is not None and a.preview is not None and a.preview.state == "ready"
            )
            again = app()
            assert again is not None and again.preview is not None and len(opened) == 2
            url = again.preview.url

            # Changes requested: it leaves Acceptance review, the app stops and its worktree goes.
            w.decide(
                KEY,
                f"CHANGE ACCEPTANCE {w.token(KEY, 'ACCEPT')}\nF1: search the description too",
                Status.CHANGES_REQUESTED,
            )
            await sup.acceptance.tick(full=True)
            assert app() is None and not Path(st.worktree).exists()
            assert _get(url) is None
    finally:
        subprocess.run(["tmux", "-L", w.cfg.claude.interactive.socket, "kill-server"], capture_output=True)


async def test_delivery_try_runs_the_candidate_and_tidies_up(tmp_path: Path) -> None:
    w = make_world(
        tmp_path,
        extra={"preview": {"command": SERVER, "setup": [], "url": "http://127.0.0.1:{port}/"}},
    )
    async with Supervisor(w.deps) as sup:
        candidate = await _to_code_review(w, sup)
    stop = asyncio.Event()
    seen: dict[str, str | None] = {}
    lines: list[str] = []

    async def browser(url: str) -> str | None:
        seen["file"] = await asyncio.to_thread(_get, url + "src/pilot-1.ts")
        stop.set()
        return None

    code = await try_candidate(
        w.cfg, KEY, repo=w.repo, jira=w.jira, out=lines.append, opener=browser, stop=stop
    )
    assert code == 0 and seen["file"] == "export const pilot_1 = true;\n"
    assert f"candidate c1 ({candidate[:12]})" in lines[0]
    assert "stopped the app and removed its worktree" in lines[-1]
    assert not list((w.cfg.repository.worktree_root / KEY).glob("try-*"))

    # Without Jira it runs the head of the ticket's feature branch.
    stop.clear()
    lines.clear()
    await try_candidate(w.cfg, KEY, repo=w.repo, jira=None, out=lines.append, opener=browser, stop=stop)
    assert f"the head of feature/{KEY}" in lines[0]
    with pytest.raises(TryError, match="no implementation candidate"):
        await try_candidate(w.cfg, "PILOT-99", repo=w.repo, jira=w.jira, out=lines.append, stop=stop)


async def test_delivery_try_needs_an_app_command(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    with pytest.raises(TryError, match=r"\[preview\] command"):
        await try_candidate(w.cfg, KEY, repo=w.repo, jira=w.jira)


def test_the_guide_quoted_in_jira_drops_the_header_and_title() -> None:
    raw = "<!-- delivery provenance (written by the coordinator) -->\n<!-- ticket: X -->\n\n" + GUIDE
    text = guide_text(raw)
    assert text.startswith("## Before you start") and "<!--" not in text
    long = guide_text("# Acceptance guide\n" + "\n".join(f"step {i} " + "x" * 80 for i in range(200)))
    assert len(long) < 8200 and long.endswith("(the full guide is linked above)")
