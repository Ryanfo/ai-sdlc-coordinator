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
        ([], ["up"]),
        (["--dry-run"], ["run", "--dry-run"]),
        (["-v", "--once"], ["-v", "run", "--once"]),
        (["--config", "x.toml"], ["up", "--config", "x.toml"]),
        (["--foreground"], ["up", "--foreground"]),
        (["-v"], ["-v", "up"]),
        (["attach", "PILOT-1"], ["attach", "PILOT-1"]),
        (["attach"], ["attach"]),
        (["stop"], ["stop"]),
        (["restart"], ["restart"]),
        (["logs"], ["logs"]),
        (["setup"], ["setup"]),
        (["clean", "--yes"], ["clean", "--yes"]),
        (["status", "--json"], ["status", "--json"]),
        (["--help"], ["--help"]),
    ],
)
def test_coordinator_alone_starts_the_supervisor(argv: list[str], expected: list[str]) -> None:
    from delivery.cli import coordinator_args, parser

    sub = next(a for a in parser()._actions if a.dest == "command")
    assert coordinator_args(argv, set(sub.choices)) == expected  # type: ignore[attr-defined]


def test_preview_config(tmp_path: Path) -> None:
    from conftest import base_sections, render_config
    from delivery.config import ConfigError, load_config

    sections = base_sections(tmp_path)
    (tmp_path / "plugin").mkdir()
    (tmp_path / "c.toml").write_text(render_config(sections, {"config_version": 1}))
    pc = load_config(tmp_path / "c.toml").preview
    assert not pc.enabled and pc.setup is None and pc.open_browser and pc.url == "http://localhost:{port}/"
    sections["preview"] = {"command": ["npm", "run", "dev"], "url": "localhost:{port}"}
    (tmp_path / "c.toml").write_text(render_config(sections, {"config_version": 1}))
    with pytest.raises(ConfigError, match="http"):
        load_config(tmp_path / "c.toml")
    sections["preview"] = {"command": ["npm", ""]}
    (tmp_path / "c.toml").write_text(render_config(sections, {"config_version": 1}))
    with pytest.raises(ConfigError, match="non-empty"):
        load_config(tmp_path / "c.toml")


def test_preview_launcher_runs_setup_then_the_app_and_keeps_its_output(tmp_path: Path) -> None:
    import subprocess

    from delivery.preview import expand, launcher

    wt = tmp_path / "work tree"
    wt.mkdir()
    log = tmp_path / "preview.log"
    script = tmp_path / "preview.sh"
    script.write_text(
        launcher(["sh", "-c", "echo setup > done.txt"], expand(["echo", "app on {port}"], 4321), wt, log)
    )
    res = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, check=False)
    assert res.returncode == 0 and "app on 4321" in res.stdout
    assert (wt / "done.txt").read_text() == "setup\n" and "app on 4321" in log.read_text()
    # A failing setup never starts the app.
    script.write_text(launcher(["false"], ["echo", "app"], wt, log))
    assert "app" not in subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True).stdout
    # Arguments are quoted, never run through the shell.
    assert "'$(rm -rf x)'" in launcher([], ["echo", "$(rm -rf x)"], wt, log)


def test_answers_only_when_something_listens() -> None:
    import socket

    from delivery.preview import answers

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert not answers(f"http://127.0.0.1:{port}/", timeout=0.5)


def test_the_closing_message_ends_an_interactive_prompt(tmp_path: Path) -> None:
    import dataclasses

    from delivery.claude import ClaudeInvocation
    from delivery.config import InteractiveConfig
    from delivery.interactive import InteractiveRunner

    inv = ClaudeInvocation(
        run_id="r",
        procedure="refine-ticket",
        envelope_path=tmp_path / "envelope.json",
        cwd=tmp_path,
        plugin_dir=tmp_path,
        schema={},
        settings_path=tmp_path / "settings.json",
        tools=("Read",),
        add_dirs=(),
        timeout=60,
        stdout_path=tmp_path / "out.jsonl",
        stderr_path=tmp_path / "err.log",
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "schema.json",
        closing="ask whether there is anything else.",
    )
    runner = InteractiveRunner(
        "claude", InteractiveConfig(enabled=True, window="none"), tmp_path, opener=None
    )
    assert runner.prompt(inv).endswith("that file.\n\nask whether there is anything else.")
    assert runner.prompt(dataclasses.replace(inv, closing="")).endswith("that file.")


def test_doctor_explains_the_app_preview(tmp_path: Path) -> None:
    from conftest import base_sections, render_config
    from delivery.config import load_config
    from delivery.doctor import Report, check_preview

    sections = base_sections(tmp_path)
    (tmp_path / "plugin").mkdir()

    def level(**sec: dict[str, Any]) -> tuple[str, str]:
        (tmp_path / "c.toml").write_text(render_config({**sections, **sec}, {"config_version": 1}))
        report = Report()
        check_preview(load_config(tmp_path / "c.toml"), report)
        (check,) = report.checks
        return check.level, check.detail

    assert level()[0] == "info"
    app = {"command": ["sh", "-c", "serve"], "setup": []}
    warn, detail = level(preview=app)
    assert warn == "warn" and "not kept open" in detail
    interactive = {"enabled": True, "window": "none"}
    ok, detail = level(preview=app, **{"claude.interactive": interactive})
    assert ok == "ok" and "sh -c serve in the session's worktree; opens http://localhost:<port>/" in detail
    missing = {"command": ["no-such-dev-server"]}
    assert level(preview=missing, **{"claude.interactive": interactive})[0] == "warn"


def test_change_requests_and_development_get_a_closing_message() -> None:
    from delivery.stages import change_ids, closing_note

    assert change_ids({"Q1": "answer", "F10": "x", "F2": "y", "F2@123": "z", "R1": "check"}) == [
        "F2",
        "F10",
        "R1",
    ]
    note = closing_note("implement-ticket", ["F1", "R1"])
    assert "change requests from Jira: F1, R1" in note
    assert "The changes requested in Jira have been actioned" in note
    assert "pushes them as the next candidate" in note
    assert "type /exit to end this session" in note and "start then, not before" in note
    assert "close this window" not in note, "closing the window does not start verification"
    spec = closing_note("refine-ticket", ["F1"], document=Path("/out/specification.md"))
    assert "The changes requested in Jira have been actioned" in spec
    assert "edit /out/specification.md in place" in spec and "next revision of the specification" in spec
    assert closing_note("verify-ticket", ["F1"]) == ""
    assert closing_note("refine-ticket", []) == "", "a first draft is not a change request"
    # Development always asks, so further changes go to the session that made the candidate.
    first = closing_note("implement-ticket", [])
    assert "actioned" not in first and "Are there any further changes you'd like to make?" in first
    assert "starting the app" not in first
    assert "starting the app" in closing_note("implement-ticket", [], preview=True)
