"""Resolving a Blocked ticket with the developer: Request resolution, the session, the report.

Real supervisor, Git and tmux; fake Jira, GitHub and an interactive fake Claude that asks the
developer questions (AskUserQuestion entries in its transcript) and writes the result.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import DEV
from delivery.models import PROPERTY_KEY
from delivery.session_hook import read_events
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import World, make_world, step

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
KEY = "PILOT-1"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = make_world(tmp_path, interactive=True)
    yield w
    subprocess.run(["tmux", "-L", w.cfg.claude.interactive.socket, "kill-server"], capture_output=True)


def _envelopes(w: World, procedure: str) -> list[dict]:  # type: ignore[type-arg]
    out = []
    for inv in sorted(w.invocations(), key=lambda i: i["started"]):
        if inv.get("procedure") == procedure:
            match = re.match(r"^/delivery:[a-z-]+ (\S+)", inv["argv"][0])
            assert match
            out.append(json.loads(Path(match.group(1)).read_text()))
    return out


def _code_blocks(w: World, key: str) -> list[str]:
    """The copyable blocks in the latest comment as Jira receives them (ADF code blocks)."""
    adf = w.jira.comments_by_key[key][-1].body_adf or {}
    return [
        "".join(t.get("text", "") for t in node.get("content", []))
        for node in adf.get("content", [])
        if node.get("type") == "codeBlock"
    ]


async def _blocked_in_development(w: World, sup: Supervisor) -> None:
    """Get PILOT-1 to Blocked: development reports a blocker and leaves an unfinished change."""
    w.new_ticket(KEY)
    w.submit(KEY)
    await step(sup)
    w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
    await step(sup)
    w.decide(KEY, f"APPROVE PLAN {w.token(KEY, 'PLAN')}", Status.READY_DEVELOPMENT)
    await step(sup)
    assert w.jira.status_of(KEY) is Status.BLOCKED, w.last_comment(KEY)
    assert w.record(KEY).pause is not None


DEV_BLOCKED = {
    "outcome": "blocked",
    "blocker_reason": "the search index is not built in this environment",
    "edit": {"src/search.ts": "export const search = () => [];\n"},
}


async def test_resolving_a_blocker_records_what_was_done_and_who_decided(world: World) -> None:
    w = world
    w.scenario(
        {
            "implement-ticket": [DEV_BLOCKED, {}],
            "resolve-blocker": [
                {
                    "summary": "The index was never built; building it on start-up fixes it.",
                    "ask": [{"question": "Build the index on start-up or lazily?", "answer": "On start-up"}],
                    "edit": {"src/search.ts": "export const search = () => buildIndex();\n"},
                    "override": {
                        "resolution": {
                            "actions": ["Built the index on start-up in src/search.ts"],
                            "decisions": [
                                {
                                    "id": "D1",
                                    "question": "Build the index on start-up or lazily?",
                                    "decision": "On start-up",
                                    "decided_by": "developer",
                                },
                                {
                                    "id": "D2",
                                    "question": "Name of the builder?",
                                    "decision": "buildIndex",
                                    "decided_by": "claude",
                                    "rationale": "matches the existing naming",
                                },
                            ],
                            "follow_ups": ["Add the index to the deploy checklist"],
                        }
                    },
                }
            ],
        }
    )
    async with Supervisor(w.deps) as sup:
        await _blocked_in_development(w, sup)
        w.jira.human_move(KEY, Status.READY_RESOLUTION, DEV)  # Request resolution
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_DEVELOPMENT, w.last_comment(KEY)

        report = w.last_comment(KEY)
        assert "Blocker resolved: development resumes" in report
        assert "Built the index on start-up" not in report  # what Claude did stays in the session
        assert "| D1 |" in report and "| D2 |" in report
        row1 = next(ln for ln in report.splitlines() if ln.startswith("| D1 |"))
        row2 = next(ln for ln in report.splitlines() if ln.startswith("| D2 |"))
        assert "Claude" not in row1.split("|")[3] and "answered when asked in the session" in row1
        assert "Claude" in row2.split("|")[3] and "matches the existing naming" in row2
        assert "Add the index to the deploy checklist" in report
        rec = w.record(KEY)
        assert rec.pause is None
        assert w.jira.issues[KEY].fields.get("customfield_10050") in (None, {"value": None})

        # The resumed development run starts from the resolution's change and is told what was decided.
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_VERIFICATION, w.last_comment(KEY)
    first, second = _envelopes(w, "implement-ticket")
    assert second["prior_work"]["files"] == ["src/search.ts"]
    notes = [n["body"] for n in second["notes"]]
    assert any("On start-up" in n and "Decision D1 by the developer" in n for n in notes)
    resolution = _envelopes(w, "resolve-blocker")[0]
    assert resolution["resolution"]["blocked_stage"] == "development"
    assert resolution["resolution"]["code_changes_carried"] is True
    assert "search index is not built" in Path(resolution["resolution"]["briefing_path"]).read_text()
    # Later runs of the stage do not repeat the note.
    assert first["notes"] == []


async def test_a_decision_claude_attributes_to_the_developer_without_evidence_is_recorded_as_claudes(
    world: World,
) -> None:
    w = world
    w.scenario(
        {
            "implement-ticket": [DEV_BLOCKED],
            "resolve-blocker": [
                {
                    "override": {
                        "resolution": {
                            "actions": ["Chose a fix"],
                            "decisions": [
                                {
                                    "id": "D1",
                                    "question": "Which fix?",
                                    "decision": "Option B",
                                    "decided_by": "developer",
                                }
                            ],
                        }
                    }
                }
            ],
        }
    )
    async with Supervisor(w.deps) as sup:
        await _blocked_in_development(w, sup)
        w.jira.human_move(KEY, Status.READY_RESOLUTION, DEV)
        await step(sup)
    report = w.comments(KEY)[-1]
    row = next(ln for ln in report.splitlines() if ln.startswith("| D1 |"))
    assert row.split("|")[3].strip() == "Claude"
    assert "shows no answer or message from them" in row


async def test_an_unresolved_blocker_returns_to_blocked_with_the_reason_and_keeps_the_changes(
    world: World,
) -> None:
    w = world
    w.scenario(
        {
            "implement-ticket": [DEV_BLOCKED, {}],
            "resolve-blocker": [
                {
                    "outcome": "blocked",
                    "blocker_reason": "the index needs a credential only the developer has",
                    "edit": {"src/partial.ts": "export const partial = 1;\n"},
                    "override": {
                        "resolution": {
                            "actions": ["Traced it to a missing credential"],
                            "next_steps": [
                                {
                                    "kind": "command",
                                    "text": "export INDEX_KEY=<your key> && npm run build:index",
                                    "why": "builds the index the search needs",
                                    "verified_by": "npm run build:index fails without INDEX_KEY, and works with it",
                                },
                                {
                                    "kind": "jira_action",
                                    "text": "Resume development",
                                    "why": "starts development again once the index exists",
                                    "verified_by": "Resume development is the action for the stage that paused",
                                },
                            ],
                        }
                    },
                }
            ],
        }
    )
    async with Supervisor(w.deps) as sup:
        await _blocked_in_development(w, sup)
        w.jira.human_move(KEY, Status.READY_RESOLUTION, DEV)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.BLOCKED, w.last_comment(KEY)
        comment = w.last_comment(KEY)
        assert "Blocker not resolved" in comment
        assert "the index needs a credential only the developer has" in comment
        assert "Traced it to a missing credential" not in comment
        assert "Resume development" in comment and "Request resolution" in comment
        assert "What to do now" in comment
        assert "1. Run this command (the developer)." in comment
        assert _code_blocks(w, KEY) == ["export INDEX_KEY=<your key> && npm run build:index"]
        assert "2. Choose Resume development in Jira (moves into Ready for development)" in comment
        assert "Checked: npm run build:index fails without INDEX_KEY" in comment
        pause = w.record(KEY).pause
        assert pause is not None and pause.resume_stage.value == "development"
        assert w.jira.issues[KEY].fields["customfield_10050"] == {"value": "development"}

        # A person can still just Resume: development continues from the unfinished changes.
        w.jira.human_move(KEY, Status.READY_DEVELOPMENT, DEV)
        await step(sup)
    envelopes = _envelopes(w, "implement-ticket")
    assert "src/partial.ts" in envelopes[-1]["prior_work"]["files"]


async def test_resolution_without_interactive_sessions_explains_itself(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"implement-ticket": [DEV_BLOCKED]})
    async with Supervisor(w.deps) as sup:
        await _blocked_in_development(w, sup)
        w.jira.human_move(KEY, Status.READY_RESOLUTION, DEV)
        await step(sup)
    assert w.jira.status_of(KEY) is Status.BLOCKED
    assert "[claude.interactive]" in w.last_comment(KEY)
    assert not [i for i in w.invocations() if i.get("procedure") == "resolve-blocker"]


async def test_only_the_assignee_or_an_approver_can_request_resolution(world: World) -> None:
    w = world
    w.scenario({"implement-ticket": [DEV_BLOCKED], "resolve-blocker": [{}]})
    async with Supervisor(w.deps) as sup:
        await _blocked_in_development(w, sup)
        w.jira.human_move(KEY, Status.READY_RESOLUTION, "someone-else-9")
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_RESOLUTION
    assert not [i for i in w.invocations() if i.get("procedure") == "resolve-blocker"]


async def test_a_rejected_decision_is_not_something_a_resolution_session_can_fix(world: World) -> None:
    w = world
    w.scenario({"implement-ticket": [DEV_BLOCKED], "resolve-blocker": [{}]})
    async with Supervisor(w.deps) as sup:
        await _blocked_in_development(w, sup)
        props = w.jira.issues[KEY].properties[PROPERTY_KEY]
        props["pause"]["blocker_kind"] = "invalid_decision"
        w.jira.human_move(KEY, Status.READY_RESOLUTION, DEV)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_RESOLUTION
    assert "decision was rejected" in w.last_comment(KEY)
    assert not [i for i in w.invocations() if i.get("procedure") == "resolve-blocker"]


async def test_a_blocked_refinement_is_resolved_and_refines_again(world: World) -> None:
    w = world
    w.scenario(
        {
            "refine-ticket": [
                {"outcome": "blocked", "blocker_reason": "the brief links a design that cannot be opened"},
                {},
            ],
            "resolve-blocker": [
                {
                    "summary": "The design link was private; the developer shared it.",
                    "override": {
                        "resolution": {
                            "actions": ["Confirmed the link opens once shared"],
                            "decisions": [
                                {
                                    "id": "D1",
                                    "question": "Use the shared design?",
                                    "decision": "Yes",
                                    "decided_by": "claude",
                                    "rationale": "the developer said it is shared",
                                }
                            ],
                        }
                    },
                }
            ],
        }
    )
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        assert w.jira.status_of(KEY) is Status.BLOCKED, w.last_comment(KEY)
        w.jira.human_move(KEY, Status.READY_RESOLUTION, DEV)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.READY_REFINEMENT, w.last_comment(KEY)
        assert "Blocker resolved: refinement resumes" in w.last_comment(KEY)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.SPECIFICATION_REVIEW, w.last_comment(KEY)
    refinements = _envelopes(w, "refine-ticket")
    notes = [n["body"] for n in refinements[-1]["notes"]]
    assert any("Decision D1 by Claude" in n and "the developer said it is shared" in n for n in notes)
    assert _envelopes(w, "resolve-blocker")[0]["resolution"]["code_changes_carried"] is False


async def test_changes_that_the_stage_would_not_carry_are_not_kept_and_are_said_so(world: World) -> None:
    w = world
    w.scenario(
        {
            "refine-ticket": [{"outcome": "blocked", "blocker_reason": "unclear brief"}],
            "resolve-blocker": [{"edit": {"src/oops.ts": "export {};\n"}}],
        }
    )
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.jira.human_move(KEY, Status.READY_RESOLUTION, DEV)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.BLOCKED
    comment = w.last_comment(KEY)
    assert "Blocker not resolved" in comment and "does not carry code changes" in comment


async def test_a_step_the_coordinator_would_not_recognise_is_refused_in_the_session_until_fixed(
    world: World,
) -> None:
    w = world
    wrong = {
        "kind": "jira_comment",
        "text": "APPROVE PLAN PILOT-1-PLAN-v2",  # the ticket has no v2: it would be ignored
        "why": "approves the plan again",
        "verified_by": "read the plan",
    }
    right = {**wrong, "text": "APPROVE PLAN PILOT-1-PLAN-v1"}
    w.scenario(
        {
            "implement-ticket": [DEV_BLOCKED],
            "resolve-blocker": [
                {
                    "outcome": "blocked",
                    "blocker_reason": "the plan approval was never recorded",
                    "override": {"resolution": {"actions": ["Read the ticket"], "next_steps": [wrong]}},
                    "after_block_override": {
                        "resolution": {"actions": ["Read the ticket"], "next_steps": [right]}
                    },
                }
            ],
        }
    )
    async with Supervisor(w.deps) as sup:
        await _blocked_in_development(w, sup)
        w.jira.human_move(KEY, Status.READY_RESOLUTION, DEV)
        await step(sup)
        assert w.jira.status_of(KEY) is Status.BLOCKED
    comment = w.last_comment(KEY)
    assert _code_blocks(w, KEY) == ["APPROVE PLAN PILOT-1-PLAN-v1"] and "PLAN-v2" not in comment
    assert "1. Paste this comment on PILOT-1 (the developer)." in comment
    run = w.deps.store.latest_run(KEY)
    assert run is not None
    events = read_events(run.journal.dir / "sessions" / "resolve-blocker")
    refused = [e for e in events if e.get("event") == "stop" and e.get("result") == "blocked"]
    assert refused and "PILOT-1-PLAN-v2 is not the current PLAN token" in refused[0]["detail"]
    assert "PILOT-1-PLAN-v1" in refused[0]["detail"]
