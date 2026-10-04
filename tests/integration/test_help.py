"""`coordinator help <KEY>`: the briefing it gathers and the Claude session it opens."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ConfigFactory
from delivery import cli, console, diagnose
from delivery.config import ConfigError
from delivery.journal import JournalStore
from delivery.models import RunState
from delivery.supervisor import Supervisor
from harness import make_world, step

KEY = "PILOT-1"


async def test_briefing_gathers_the_ticket_its_comments_runs_and_log(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        live = {"sessions": [], "open_sessions": [], "dispatch_paused": True, "pause_reason": "lunch"}
        b = await diagnose.write_briefing(
            w.cfg, KEY, "why is it not moving?", jira=w.jira, github=w.github, live=live
        )
    text = b.path.read_text()
    assert b.path.stat().st_mode & 0o077 == 0 and not b.problems
    assert "## The developer's question\n\nwhy is it not moving?" in text
    assert "New work is PAUSED: lunch" in text
    assert f"{KEY}: specification_review" in text and '"gates"' in text
    assert "Specification v001 ready for review" in text  # the coordinator's gate comment
    assert "Ready for refinement -> Refining" in text
    run = JournalStore(w.cfg.runtime.state_dir, w.cfg.identity_key).runs_for_ticket(KEY)[-1]
    assert b.run_dirs == [run.journal.dir]
    assert str(run.journal.dir / "claude-refine-ticket.json") in text
    assert str(run.journal.dir / "output" / "refine-ticket" / "specification.md") in text
    assert "claude-refine-ticket.txt" in text  # readable transcript written for Claude
    assert "coordinator help" not in text.split("## Recent Jira comments")[0].split("```")[1]


async def test_briefing_is_written_even_when_jira_cannot_be_reached(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    b = await diagnose.write_briefing(
        w.cfg, KEY, "", jira=None, github=w.github, live=None, jira_problem="no Jira token"
    )
    text = b.path.read_text()
    assert len(b.problems) == 1 and "no Jira token" in b.problems[0]
    assert "Could not gather this" in text and "None given" in text
    assert "did not answer on its control socket" in text
    assert f"No runs of {KEY} on this machine" in text
    assert "## Looking further" in text


def test_session_opens_the_skill_with_read_only_commands_allowed(make_config: ConfigFactory) -> None:
    cfg = make_config()
    b = diagnose.Briefing(Path("/tmp/b/briefing.md"), Path("/tmp/b"))
    argv = diagnose.session_argv(cfg, b)
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--plugin-dir") + 1] == str(cfg.claude.plugin_path)
    assert argv[-2:] == ["--", "/delivery:diagnose-ticket /tmp/b/briefing.md"]
    allowed = argv[argv.index("--allowedTools") + 1 : argv.index("--")]
    assert "Bash(coordinator inspect:*)" in allowed and "Bash(gh pr view:*)" in allowed
    for changes in ("recover", "stop", "handover", "restart", "dispatch", "guidance add", "clean"):
        assert not any(f"coordinator {changes}" in a for a in allowed)
    assert "-p" not in argv  # interactive: the developer talks to it
    cfg = make_config(overrides={"claude": {"help_model": "sonnet"}})
    assert diagnose.session_argv(cfg, b)[argv.index("--model") + 1] == "sonnet"
    with pytest.raises(ConfigError):
        make_config(overrides={"claude": {"help_model": "not a model!"}})


def test_session_stays_on_the_subscription_and_uses_this_config(make_config: ConfigFactory) -> None:
    cfg = make_config()
    env = diagnose.session_env(
        cfg,
        {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "sk-x",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_BASE_URL": "https://proxy.example",
            "DELIVERY_CONFIG": "/elsewhere.toml",
        },
    )
    assert env["PATH"] == "/bin" and env["DELIVERY_CONFIG"] == str(cfg.source_path)
    assert not {"ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_BASE_URL"} & set(env)


def test_help_without_a_ticket_is_the_usual_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["help"], prog="coordinator") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("usage: coordinator") and "ask Claude what is wrong with a ticket" in out


async def test_a_failed_run_points_at_help(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.scenario({"refine-ticket": [{"exit_code": 1}]})
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
    rec = JournalStore(w.cfg.runtime.state_dir, w.cfg.identity_key).runs_for_ticket(KEY)[-1].record
    assert rec is not None
    finished = console.session_finished(w.cfg, rec, "Feature")
    trouble = rec.state in (RunState.BLOCKED, RunState.FAILED)
    assert (f"coordinator help {KEY}" in finished) is trouble
    assert trouble, rec.state
