from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from delivery.claude import (
    ClaudeInvocation,
    ClaudeStatus,
    classify,
    env_auth_conflicts,
    settings_auth_conflicts,
    version_in_range,
    worker_env,
)
from delivery.models import Outcome, result_json_schema
from delivery.permissions import PROCEDURE_ROLES, Role, build_profile
from delivery.plugin import PROCEDURES, load_plugin
from delivery.stages import OutputInvalid, is_protected, safe_output_file, validate_result
from delivery.workflow import Stage

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "delivery"
INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "s",
    "plugins": [{"name": "delivery", "path": "/p"}],
}


def stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


def ok_result(**kw: object) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "x",
        "structured_output": {"a": 1},
        **kw,
    }


def test_classify_success_requires_plugin_and_structured_output() -> None:
    assert classify(stream(INIT, ok_result()), "", 0, Path("/p")).status is ClaudeStatus.OK
    no_plugin = {**INIT, "plugins": []}
    assert classify(stream(no_plugin, ok_result()), "", 0, Path("/p")).status is ClaudeStatus.PLUGIN_MISSING
    err = {**INIT, "plugin_errors": [{"plugin": "delivery", "type": "load", "message": "bad"}]}
    assert classify(stream(err, ok_result()), "", 0, Path("/p")).status is ClaudeStatus.PLUGIN_MISSING
    no_so = ok_result(structured_output=None)
    assert classify(stream(INIT, no_so), "", 0, Path("/p")).status is ClaudeStatus.MALFORMED


def test_classify_exit_success_alone_is_not_success() -> None:
    assert classify("garbage", "", 0, Path("/p")).status is ClaudeStatus.MALFORMED
    assert classify(stream(INIT), "boom", 1, Path("/p")).status is ClaudeStatus.ERROR
    turns = ok_result(subtype="error_max_turns", is_error=True)
    assert classify(stream(INIT, turns), "", 1, Path("/p")).status is ClaudeStatus.MAX_TURNS


@pytest.mark.parametrize(
    "text",
    [
        "Failed to authenticate: OAuth session expired and could not be refreshed",  # observed on this Mac
        "Not logged in · Please run /login",
        "Invalid API key",
    ],
)
def test_classify_auth_failures(text: str) -> None:
    res = {"type": "result", "subtype": "success", "is_error": True, "result": text}
    assert classify(stream(INIT, res), "", 0, Path("/p")).status is ClaudeStatus.AUTH


def test_classify_usage_limit_never_suggests_fallback() -> None:
    res = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "Claude usage limit reached. Your limit resets at 5pm.",
    }
    out = classify(stream(INIT, res), "", 1, Path("/p"))
    assert out.status is ClaudeStatus.USAGE_LIMIT and "no paid fallback" in out.detail


def test_permission_denials_are_captured() -> None:
    out = classify(stream(INIT, ok_result(permission_denials=[{"tool_name": "Bash"}])), "", 0, Path("/p"))
    assert out.permission_denials == [{"tool_name": "Bash"}]


def test_worker_env_strips_credentials_and_provider_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "ANTHROPIC_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "JIRA_API_TOKEN",
        "SSH_AUTH_SOCK",
        "CLAUDE_CODE_USE_BEDROCK",
        "ANTHROPIC_BASE_URL",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.setenv(k, "x" * 20)
    env = worker_env({"TMPDIR": "/t"})
    assert set(env) <= {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SHELL",
        "TERM",
        "TZ",
        "TMPDIR",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    }


def test_auth_conflicts_detected(tmp_path: Path) -> None:
    assert env_auth_conflicts({"ANTHROPIC_API_KEY": "k"})
    assert env_auth_conflicts({"CLAUDE_CODE_USE_VERTEX": "1"})
    assert env_auth_conflicts({"ANTHROPIC_BASE_URL": "https://gateway.corp"})
    assert not env_auth_conflicts({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"})
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"apiKeyHelper": "/bin/key", "env": {"ANTHROPIC_AUTH_TOKEN": "t"}}))
    assert len(settings_auth_conflicts([s])) == 2


def test_version_range() -> None:
    assert version_in_range("2.1.278 (Claude Code)", ">=2.1.0,<3")
    assert not version_in_range("1.0.9", ">=2.1.0,<3")
    assert not version_in_range("3.0.0", ">=2.1.0,<3")


def test_invocation_never_bypasses_permissions(tmp_path: Path) -> None:
    inv = ClaudeInvocation(
        "r",
        "refine-ticket",
        tmp_path / "e.json",
        tmp_path,
        PLUGIN,
        result_json_schema(),
        tmp_path / "s.json",
        ("Read",),
        (tmp_path,),
        60,
        tmp_path / "o",
        tmp_path / "e",
        max_turns=5,
    )
    argv = inv.argv("claude")
    assert "--restricted" in argv and "--strict-mcp-config" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert not any("dangerously" in a or a == "bypassPermissions" or a == "--bare" for a in argv)
    assert argv[argv.index("-p") + 1].startswith("/delivery:refine-ticket ")


@pytest.mark.parametrize("role", list(Role))
def test_profiles_deny_credentials_and_dangerous_commands(role: Role, tmp_path: Path) -> None:
    wt, out, inp, tmp = (tmp_path / n for n in ("wt", "out", "in", "tmp"))
    p = build_profile(role, worktree=wt, output_dir=out, inputs_dir=inp, tmp_dir=tmp)
    deny = p.settings["permissions"]["deny"]
    assert "Read(~/.ssh/**)" in deny and "Read(~/.config/gh/**)" in deny
    # Only the resolver has a person in the session to ask (AskUserQuestion is denied under dontAsk).
    expected_mode = "default" if role is Role.RESOLVER else "dontAsk"
    assert p.settings["permissions"]["defaultMode"] == expected_mode == p.permission_mode
    assert p.settings["permissions"]["disableBypassPermissionsMode"] == "disable"
    sb = p.settings["sandbox"]
    assert sb["enabled"] and sb["failIfUnavailable"] and sb["allowUnsandboxedCommands"] is False
    assert "~/.ssh" in sb["filesystem"]["denyRead"]
    if role in (Role.IMPLEMENTER, Role.RESOLVER, Role.VERIFIER):
        assert "Bash" in p.tools
        assert sb["network"]["allowLocalBinding"] is True  # tests may start a local server
        assert "allowMachLookup" not in sb["network"]  # would disable sandboxed auto-allow
    else:
        assert sb["network"]["allowLocalBinding"] is False and sb["network"]["allowedDomains"] == []
    if role in (Role.IMPLEMENTER, Role.RESOLVER, Role.VERIFIER):
        assert "Bash(gh *)" in deny and "Bash(git push *)" in deny and "Bash(curl *)" in deny
    else:
        assert "Bash" not in p.tools
    abs_wt = "/" + str(wt.resolve())
    allow = p.settings["permissions"]["allow"]
    if role in (Role.IMPLEMENTER, Role.RESOLVER):
        assert f"Edit({abs_wt}/**)" in allow
        assert f"Edit({abs_wt}/.github/**)" in deny
    elif role is Role.VERIFIER:
        # File tools cannot edit (no allow rule under dontAsk); tests may write build output.
        assert f"Edit({abs_wt}/**)" not in allow and f"Edit({abs_wt}/**)" not in deny
        assert f"Edit({abs_wt}/.github/**)" in deny
    else:
        assert f"Edit({abs_wt}/**)" in deny  # read-only worktree for review and authoring


def test_only_the_resolver_can_ask_the_developer_questions(tmp_path: Path) -> None:
    wt, out, inp, tmp = (tmp_path / n for n in ("wt", "out", "in", "tmp"))
    for role in Role:
        p = build_profile(role, worktree=wt, output_dir=out, inputs_dir=inp, tmp_dir=tmp)
        asks = "AskUserQuestion" in p.tools
        assert asks is (role is Role.RESOLVER)
        assert ("AskUserQuestion" in p.settings["permissions"]["allow"]) is asks


def test_every_procedure_has_role_and_contract() -> None:
    info = load_plugin(PLUGIN)
    assert set(info.contracts) == set(PROCEDURES) == set(PROCEDURE_ROLES)
    assert all(c.startswith("delivery.") for c in info.contracts.values())
    assert (PLUGIN / "skills" / "smoke-test" / "SKILL.md").exists()


def test_exported_schemas_match_models() -> None:
    ref = PLUGIN / "references" / "schemas" / "stage-result.schema.json"
    assert json.loads(ref.read_text()) == result_json_schema(), "run `delivery schemas` after model changes"


# --------------------------------------------------------------------------- output safety


def test_safe_output_file_rejects_escape_symlink_missing_and_oversize(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    (root / "ok.md").write_text("x")
    assert safe_output_file(root, "ok.md").name == "ok.md"
    for bad in ("/etc/passwd", "../x", "a/../../x", "~/x", ""):
        with pytest.raises(OutputInvalid):
            safe_output_file(root, bad)
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    os.symlink(secret, root / "link.md")
    with pytest.raises(OutputInvalid, match="symlink"):
        safe_output_file(root, "link.md")
    with pytest.raises(OutputInvalid, match="not written"):
        safe_output_file(root, "missing.md")
    (root / "big.md").write_bytes(b"x" * 2_100_000)
    with pytest.raises(OutputInvalid, match="exceeds"):
        safe_output_file(root, "big.md")


def test_validate_result_identity_and_fabricated_contract() -> None:
    base = {
        "schema_version": 1,
        "contract_id": "delivery.refine-ticket/v1",
        "run_id": "PILOT-1-refinement-x",
        "ticket_key": "PILOT-1",
        "stage": "refinement",
        "procedure": "refine-ticket",
        "input_revision": "a" * 64,
        "outcome": "completed",
        "summary": "ok",
    }
    kw = {
        "contract_id": "delivery.refine-ticket/v1",
        "procedure": "refine-ticket",
        "run_id": "PILOT-1-refinement-x",
        "ticket": "PILOT-1",
        "stage": Stage.REFINEMENT,
        "input_revision": "a" * 64,
    }
    assert validate_result(base, **kw).outcome is Outcome.COMPLETED  # type: ignore[arg-type]
    for change in (
        {"contract_id": "x/v1"},
        {"ticket_key": "PILOT-2"},
        {"stage": "planning"},
        {"input_revision": "b" * 64},
        {"outcome": "approved"},
        {"questions": [{"id": "Q1", "question": "a"}, {"id": "Q1", "question": "b"}]},
    ):
        with pytest.raises(OutputInvalid):
            validate_result({**base, **change}, **kw)  # type: ignore[arg-type]
    with pytest.raises(OutputInvalid):
        validate_result(None, **kw)  # type: ignore[arg-type]


def test_protected_paths() -> None:
    for p in (".github/workflows/ci.yml", "docs/delivery/PILOT-1/x.md", ".claude/settings.json", "CLAUDE.md"):
        assert is_protected(p)
    for p in ("src/app.ts", "docs/readme.md", "github/x"):
        assert not is_protected(p)


@pytest.mark.parametrize(
    ("reply", "ok", "detail"),
    [
        (
            '{"is_error": false, "result": "OK", "modelUsage": {"claude-opus-5-5": {}}}',
            True,
            "runs as claude-opus-5-5",
        ),
        (
            '{"is_error": true, "result": "There\'s an issue with the selected model (x)."}',
            False,
            "issue with the selected",
        ),
    ],
)
async def test_model_check_reports_usable_and_unusable_models(
    tmp_path: Path, reply: str, ok: bool, detail: str
) -> None:
    from delivery.claude import model_check

    exe = tmp_path / "claude"
    exe.write_text(
        f"#!/bin/sh\necho '{reply}'\n" if "'" not in reply else "#!/bin/sh\ncat <<'J'\n" + reply + "\nJ\n"
    )
    exe.chmod(0o755)
    got_ok, got = await model_check(str(exe), "opus", tmp_path)
    assert got_ok is ok and detail in got
