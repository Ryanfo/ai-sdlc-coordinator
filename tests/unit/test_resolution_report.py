from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from delivery.models import GateRecord, GateState, ResolutionDecision
from delivery.ports import JiraComment
from delivery.resolution import (
    Answered,
    asked_in_session,
    attribute,
    check_next_steps,
    current_tokens,
    ticket_state_lines,
    typed_by_developer,
    write_briefing,
)


def _transcript(tmp_path: Path, *entries: dict) -> Path:  # type: ignore[type-arg]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def _ask(tool_id: str, question: str) -> dict:  # type: ignore[type-arg]
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "AskUserQuestion",
                    "input": {"questions": [{"question": question, "header": "Q"}]},
                }
            ]
        },
    }


def test_answers_are_read_from_a_real_claude_code_transcript(tmp_path: Path) -> None:
    # Shapes recorded from Claude Code 2.1.289.
    t = _transcript(
        tmp_path,
        _ask("toolu_1", "Do you prefer red or blue?"),
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": 'Your questions have been answered: "Do you prefer red or blue?"="Red". '
                        "You can now continue with these answers in mind.",
                    }
                ]
            },
            "toolUseResult": {"answers": {"Do you prefer red or blue?": "Red"}, "annotations": {}},
        },
    )
    assert asked_in_session(t) == [Answered("Do you prefer red or blue?", "Red")]


def test_answers_fall_back_to_the_result_text_and_ignore_unanswered_or_other_tools(tmp_path: Path) -> None:
    t = _transcript(
        tmp_path,
        _ask("a", "Which?"),
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "a", "content": '"Which?"="Option B".'}]
            },
        },
        _ask("b", "Declined?"),
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "b", "content": "Interrupted."}]},
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "zz", "content": '"x"="y"'}]},
        },
    )
    assert asked_in_session(t) == [Answered("Which?", "Option B")]
    assert asked_in_session(tmp_path / "missing.jsonl") == []


def test_typed_messages_exclude_the_opening_prompt_and_slash_commands() -> None:
    events = [
        {"event": "start"},
        {"event": "prompt", "prompt": "/delivery:resolve-blocker /x/envelope.json"},
        {"event": "prompt", "prompt": "use the second one"},
        {"event": "prompt", "prompt": "/compact"},
        {"event": "prompt", "prompt": "  "},
    ]
    assert typed_by_developer(events) == ["use the second one"]


def _d(i: str, by: str, decision: str = "Option B") -> ResolutionDecision:
    return ResolutionDecision(id=i, question="Which?", decision=decision, decided_by=by)  # type: ignore[arg-type]


def test_a_developer_decision_needs_an_answer_or_message_in_the_session() -> None:
    rows = attribute([_d("D1", "developer")], [Answered("Which?", "Option B")], [])
    assert rows[0]["decided_by"] == "developer"
    assert rows[0]["basis"].startswith("answered when asked in the session")
    rows = attribute([_d("D1", "developer")], [], ["go with option b please"])
    assert rows[0]["decided_by"] == "developer" and rows[0]["basis"].startswith("told Claude")
    rows = attribute([_d("D1", "developer")], [], [])
    assert rows[0]["decided_by"] == "claude" and "shows no answer or message" in rows[0]["basis"]


def test_each_answer_backs_one_decision_only() -> None:
    rows = attribute(
        [_d("D1", "developer", "Option B"), _d("D2", "developer", "Something else")],
        [Answered("Which?", "Option B")],
        [],
    )
    assert [r["decided_by"] for r in rows] == ["developer", "claude"]


def test_claudes_own_decisions_say_so() -> None:
    d = ResolutionDecision(
        id="D1", question="Name?", decision="x", decided_by="claude", rationale="convention"
    )
    assert (
        attribute([d], [Answered("Which?", "x")], ["x"])[0]["basis"] == "Claude's own judgement: convention"
    )


def test_briefing_has_the_blocker_logs_and_comments(tmp_path: Path) -> None:
    path = write_briefing(
        tmp_path / "in" / "briefing.md",
        key="K-1",
        summary="Search",
        blocked_stage="development",
        blocker_kind="worker_blocked",
        blocker_reason="index missing",
        next_action="Fix and resume",
        blocked_run={"run": "r1", "state": "blocked"},
        comments=[("Ana, Mon 09:00", "please hurry")],
        logs=[("unit.err.log", "boom")],
        transcript_tail=["> Bash: npm test"],
    )
    text = path.read_text()
    for needle in (
        "index missing",
        "Fix and resume",
        "run: r1",
        "npm test",
        "unit.err.log",
        "boom",
        "please hurry",
    ):
        assert needle in text
    assert oct(path.stat().st_mode & 0o777) == "0o600"


# --------------------------------------------------------------------------- next steps
# The cases below are what went wrong on SDLC-13: a release record naming another ticket's commit,
# a correction posted as a quoted sentence, and then one with a token the ticket never had.


GOOD = "48583e88df470ea0dba878b32cf59faa026d1bf7"
WRONG = "71c5eaab5a2eb945411b9d872012340950193b46"
EXPECT = {
    "tokens": {"SPEC": "PILOT-1-SPEC-v2", "RELEASE": "PILOT-1-RELEASE-v1"},
    "round_token": None,
    "blocked_actions": {
        "resume release verification": "release_verification",
        "resume development": "development",
        "request resolution": None,
        "cancel": None,
    },
    "resume_stage": "release_verification",
    "release_environment": "local-pilot",
}


def _step(kind: str, text: str, **kw: str) -> dict:  # type: ignore[type-arg]
    return {
        "kind": kind,
        "text": text,
        "who": "the developer",
        "why": "fixes it",
        "verified_by": "checked",
        **kw,
    }


def _blocked(*steps: dict) -> dict:  # type: ignore[type-arg]
    return {"outcome": "blocked", "resolution": {"next_steps": list(steps)}}


def _record(ref: str = "PILOT-1-RELEASE-v1", commit: str = GOOD, env: str = "local-pilot") -> str:
    return f"RECORD RELEASE {ref}\ncommit: {commit}\nenvironment: {env}\nmerged-pr: 8"


def test_a_correct_record_comment_and_resume_action_are_accepted() -> None:
    steps = [_step("jira_comment", _record()), _step("jira_action", "Resume release verification")]
    assert check_next_steps(_blocked(*steps), EXPECT) is None
    # Wrapped in a code fence as people paste it from the ticket: still the same comment.
    assert check_next_steps(_blocked(_step("jira_comment", f"```\n{_record()}\n```")), EXPECT) is None


def test_the_quoted_sentence_from_the_first_session_is_refused() -> None:
    quoted = f"'RECORD RELEASE PILOT-1-RELEASE-v1' with 'commit: {GOOD}', 'environment: local-pilot'"
    problem = check_next_steps(_blocked(_step("jira_comment", quoted)), EXPECT)
    assert problem and "would not be recognised" in problem and problem.startswith("next step 1")


def test_a_token_the_ticket_does_not_have_is_refused() -> None:
    problem = check_next_steps(_blocked(_step("jira_comment", _record(ref="PILOT-1-RELEASE-v2"))), EXPECT)
    assert problem and "PILOT-1-RELEASE-v2 is not the current RELEASE token" in problem
    assert "PILOT-1-RELEASE-v1" in problem  # and says which one to use


def test_a_record_needs_a_full_sha_and_the_configured_environment() -> None:
    assert "40-character" in (
        check_next_steps(_blocked(_step("jira_comment", _record(commit="48583e8"))), EXPECT) or ""
    )
    assert "environment: local-pilot" in (
        check_next_steps(_blocked(_step("jira_comment", _record(env="prod"))), EXPECT) or ""
    )


def test_an_action_must_exist_on_a_blocked_ticket_for_the_stage_that_paused() -> None:
    assert "not an action Jira offers" in (
        check_next_steps(_blocked(_step("jira_action", "Approve release")), EXPECT) or ""
    )
    problem = check_next_steps(_blocked(_step("jira_action", "Resume development")), EXPECT)
    assert problem and "paused in release_verification" in problem
    assert check_next_steps(_blocked(_step("jira_action", "Cancel")), EXPECT) is None


def test_a_blocked_resolution_must_say_what_to_do_and_a_completed_one_must_not_ask_anything() -> None:
    assert "must list next_steps" in (
        check_next_steps({"outcome": "blocked", "resolution": {}}, EXPECT) or ""
    )
    problem = check_next_steps(
        {"outcome": "completed", "resolution": {"next_steps": [_step("other", "do it")]}}, EXPECT
    )
    assert problem and "not resolved" in problem
    assert check_next_steps({"outcome": "completed", "resolution": {}}, EXPECT) is None


def test_every_problem_is_reported_with_its_step_number() -> None:
    bad = _blocked(
        _step("jira_comment", _record()), _step("jira_comment", "nonsense"), _step("jira_action", "Nope")
    )
    problem = check_next_steps(bad, EXPECT) or ""
    assert "next step 2" in problem and "next step 3" in problem and "next step 1" not in problem


def _gate(kind: str, rev: int, state: str) -> GateRecord:
    return GateRecord(
        token=f"PILOT-1-{kind}-v{rev}",
        kind=kind,  # type: ignore[arg-type]
        ticket_key="PILOT-1",
        revision=rev,
        published_at=dt.datetime(2026, 10, 3, tzinfo=dt.UTC),
        approvers=[],
        state=GateState(state),
    )


def _comment(i: int, hour: int, body: str) -> JiraComment:
    when = dt.datetime(2026, 10, 5, hour, tzinfo=dt.UTC)
    return JiraComment(str(i), "acct", when, when, body, "Ryan")


def test_the_briefing_shows_which_record_the_coordinator_actually_uses() -> None:
    gates = [_gate("SPEC", 1, "superseded"), _gate("SPEC", 2, "approved"), _gate("RELEASE", 1, "approved")]
    assert current_tokens(gates) == {"SPEC": "PILOT-1-SPEC-v2", "RELEASE": "PILOT-1-RELEASE-v1"}
    comments = [
        _comment(1, 10, _record(commit=WRONG)),
        _comment(2, 11, f"'RECORD RELEASE PILOT-1-RELEASE-v1' with 'commit: {GOOD}'"),
        _comment(3, 12, _record(ref="PILOT-1-RELEASE-v2")),
        _comment(4, 13, "looks fine to me"),
        _comment(5, 14, "RECORD RELEASE PILOT-1-RELEASE-v1\ncommit: " + GOOD + "\nenvironment: local-pilot"),
    ]
    text = "\n".join(
        ticket_state_lines(
            gates=gates,
            comments=comments,
            resume_stage="release_verification",
            round_token=None,
            blocked_actions=EXPECT["blocked_actions"],  # type: ignore[arg-type]
        )
    )
    assert "- RELEASE: PILOT-1-RELEASE-v1 (approved)" in text and "- SPEC: PILOT-1-SPEC-v2" in text
    first = next(ln for ln in text.splitlines() if "#1 " in ln)
    assert "superseded by a later comment" in first
    assert "NOT RECOGNISED" in next(ln for ln in text.splitlines() if "#2 " in ln)
    assert "IGNORED, PILOT-1-RELEASE-v2 is not the current RELEASE token (PILOT-1-RELEASE-v1)" in text
    assert "CURRENT, the newest for its token" in next(ln for ln in text.splitlines() if "#5 " in ln)
    assert "looks fine" not in text
    assert "Resume release verification" not in text  # actions are listed by their lower-case keys
