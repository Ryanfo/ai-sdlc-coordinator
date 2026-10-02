"""Interactive-session pieces: the Stop hook, transcript facts, follow-up routes, folder trust."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from delivery.interactive import Mirror, hook_settings
from delivery.session_hook import EXPECT, HANDED_OFF, handle, read_events
from delivery.workflow import (
    FOLLOW_UP_STATUSES,
    Action,
    IllegalTransition,
    Status,
    coordinator_route,
)

RUN = "PILOT-1-refinement-x"
REV = "a" * 64


def _session(tmp: Path, **expect: Any) -> Path:
    sdir = tmp / "session"
    sdir.mkdir()
    (sdir / EXPECT).write_text(
        json.dumps(
            {
                "contract_id": "delivery.refine-ticket/v1",
                "procedure": "refine-ticket",
                "run_id": RUN,
                "ticket": "PILOT-1",
                "stage": "refinement",
                "input_revision": REV,
                "result_path": str(tmp / "result.json"),
                "schema_path": str(tmp / "schema.json"),
                **expect,
            }
        )
    )
    return sdir


def _result(**over: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract_id": "delivery.refine-ticket/v1",
        "run_id": RUN,
        "ticket_key": "PILOT-1",
        "stage": "refinement",
        "procedure": "refine-ticket",
        "input_revision": REV,
        "outcome": "completed",
        "summary": "done",
        **over,
    }


def test_stop_is_refused_until_the_result_is_valid_then_gives_up(tmp_path: Path) -> None:
    sdir = _session(tmp_path)
    reply = handle("stop", sdir, {})
    assert reply is not None and reply["decision"] == "block"
    assert "has not been written" in reply["reason"] and str(tmp_path / "result.json") in reply["reason"]
    (tmp_path / "result.json").write_text(json.dumps(_result(run_id="someone-else")))
    assert handle("stop", sdir, {}) is not None
    assert handle("stop", sdir, {}) is not None
    # Three refusals in a row: let it stop and report the problem instead of looping forever.
    assert handle("stop", sdir, {}) is None
    assert [e["result"] for e in read_events(sdir)] == ["blocked", "blocked", "blocked", "missing"]
    assert "identity does not match" in read_events(sdir)[-1]["detail"]


def test_a_person_typing_starts_a_fresh_count_and_a_valid_result_passes(tmp_path: Path) -> None:
    sdir = _session(tmp_path)
    for _ in range(3):
        handle("stop", sdir, {})
    handle("prompt", sdir, {"prompt": "please carry on"})
    assert handle("stop", sdir, {}) is not None  # blocked again, not given up
    (tmp_path / "result.json").write_text(json.dumps(_result()))
    assert handle("stop", sdir, {}) is None
    assert read_events(sdir)[-1]["result"] == "valid"
    assert any(e.get("prompt") == "please carry on" for e in read_events(sdir))


def test_an_api_error_is_reported_not_blocked(tmp_path: Path) -> None:
    sdir = _session(tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "message": {"content": [{"type": "text", "text": "Claude usage limit reached."}]},
            }
        )
        + "\n"
    )
    assert handle("stop", sdir, {"transcript_path": str(transcript)}) is None
    ev = read_events(sdir)[-1]
    assert ev["result"] == "api_error" and "usage limit" in ev["detail"]


def test_after_hand_off_stops_are_only_recorded(tmp_path: Path) -> None:
    sdir = _session(tmp_path)
    (sdir / HANDED_OFF).touch()
    assert handle("stop", sdir, {}) is None
    assert "result" not in read_events(sdir)[-1]


def test_diagnostic_results_need_only_their_keys(tmp_path: Path) -> None:
    sdir = _session(tmp_path, contract_id="delivery.smoke-test/v1", required_keys=["contract_id", "attempts"])
    (tmp_path / "result.json").write_text(json.dumps({"contract_id": "delivery.smoke-test/v1"}))
    assert handle("stop", sdir, {}) is not None
    (tmp_path / "result.json").write_text(
        json.dumps({"contract_id": "delivery.smoke-test/v1", "attempts": []})
    )
    assert handle("stop", sdir, {}) is None


def test_hooks_cover_every_event_and_point_at_the_session(tmp_path: Path) -> None:
    hooks = hook_settings(tmp_path / "s x")
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}
    cmd = hooks["Stop"][0]["hooks"][0]["command"]
    assert "-m delivery.session_hook stop" in cmd and "'" in cmd  # the path with a space is quoted


def _mirror(tmp: Path, entries: list[dict[str, Any]]) -> Mirror:
    transcript = tmp / "transcript.jsonl"
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
    m = Mirror(tmp / "logs" / "claude-x.jsonl")
    m.attach(str(transcript))
    m.pump()
    return m


def test_transcript_proves_the_plugin_skill_ran(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "skills" / "refine-ticket").mkdir(parents=True)
    base = {"type": "user", "isMeta": True}
    ok = _mirror(
        tmp_path,
        [
            {
                **base,
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Base directory for this skill: {plugin}/skills/refine-ticket\n\n# x",
                        }
                    ]
                },
            }
        ],
    )
    assert ok.plugin_ran(plugin, "refine-ticket")
    assert not ok.plugin_ran(plugin, "plan-ticket")
    assert not ok.plugin_ran(tmp_path / "elsewhere", "refine-ticket")
    assert ok.count == 1 and (tmp_path / "logs" / "claude-x.jsonl").read_text().count("\n") == 1


def test_transcript_facts_unknown_skill_denials_and_api_errors(tmp_path: Path) -> None:
    m = _mirror(
        tmp_path,
        [
            {
                "type": "system",
                "subtype": "informational",
                "content": "Unknown command: /delivery:refine-ticket",
            },
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr list"}}
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "is_error": True,
                            "content": "Permission to use Bash with command gh pr list has been denied.",
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "message": {
                    "id": "m2",
                    "content": [{"type": "text", "text": "Not logged in · Please run /login"}],
                },
            },
        ],
    )
    assert m.unknown_skill() == "Unknown command: /delivery:refine-ticket"
    assert m.denials() == [
        {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "gh pr list"}}
    ]
    assert m.trailing_api_error() == "Not logged in · Please run /login"
    assert m.turns() == 2


def test_tool_output_mentioning_unknown_commands_is_not_a_missing_plugin(tmp_path: Path) -> None:
    m = _mirror(
        tmp_path,
        [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t", "content": "Unknown command: /delivery:x"}
                    ]
                },
            }
        ],
    )
    assert m.unknown_skill() is None


@pytest.mark.parametrize("src", [Status.CODE_REVIEW, Status.ACCEPTANCE_REVIEW, Status.CHANGES_REQUESTED])
def test_follow_ups_return_review_statuses_to_ready_for_verification(src: Status) -> None:
    assert coordinator_route(src, Action.SUBMIT_FOLLOW_UP).target is Status.READY_VERIFICATION
    assert src in FOLLOW_UP_STATUSES


@pytest.mark.parametrize("src", [Status.VERIFYING, Status.READY_DEVELOPMENT, Status.DONE, Status.BLOCKED])
def test_follow_ups_never_interrupt_other_statuses(src: Status) -> None:
    with pytest.raises(IllegalTransition):
        coordinator_route(src, Action.SUBMIT_FOLLOW_UP)


def test_interactive_config_defaults(tmp_path: Path) -> None:
    from conftest import base_sections, render_config
    from delivery.config import load_config

    sections = base_sections(tmp_path)
    (tmp_path / "plugin").mkdir()
    (tmp_path / "c.toml").write_text(render_config(sections, {"config_version": 1}))
    cfg = load_config(tmp_path / "c.toml")
    ic = cfg.claude.interactive
    assert not ic.enabled and not ic.follow_ups and ic.keep_open and ic.socket == "delivery"
    sections["claude.interactive"] = {"enabled": True, "window": "none"}
    (tmp_path / "c.toml").write_text(render_config(sections, {"config_version": 1}))
    assert load_config(tmp_path / "c.toml").claude.interactive.follow_ups


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
async def test_tmux_sessions_get_exactly_the_given_environment(tmp_path: Path) -> None:
    from delivery.tmux import Tmux, session_name

    t = Tmux(config=tmp_path / "tmux.conf", socket=f"dlvunit-{tmp_path.name}"[-40:])
    name = session_name("PILOT-1", "implement-ticket")
    try:
        await t.start(name, tmp_path, ["/bin/sh", "-c", "env > env.txt; sleep 30"], {"ONLY": "this"})
        for _ in range(50):
            if (tmp_path / "env.txt").exists() and (tmp_path / "env.txt").read_text():
                break
            import asyncio

            await asyncio.sleep(0.1)
        env = (tmp_path / "env.txt").read_text()
        assert "ONLY=this" in env and "HOME=" not in env and "TMUX" not in env
        assert await t.alive(name) and not await t.alive("PILOT-1")  # exact names only
        assert name in await t.sessions()
    finally:
        await t.kill(name)
    assert not await t.alive(name)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], ["run"]),
        (["--dry-run"], ["run", "--dry-run"]),
        (["-v", "--once"], ["-v", "run", "--once"]),
        (["--config", "x.toml"], ["run", "--config", "x.toml"]),
        (["attach", "PILOT-1"], ["attach", "PILOT-1"]),
        (["status", "--json"], ["status", "--json"]),
        (["--help"], ["--help"]),
    ],
)
def test_coordinator_alone_starts_the_supervisor(argv: list[str], expected: list[str]) -> None:
    from delivery.cli import coordinator_args, parser

    sub = next(a for a in parser()._actions if a.dest == "command")
    assert coordinator_args(argv, set(sub.choices)) == expected  # type: ignore[attr-defined]
