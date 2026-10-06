from __future__ import annotations

import json
from pathlib import Path

from delivery.models import ResolutionDecision
from delivery.resolution import Answered, asked_in_session, attribute, typed_by_developer, write_briefing


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
