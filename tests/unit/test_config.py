from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from conftest import ConfigFactory, base_sections, render_config
from delivery.config import Config, ConfigError, load_config, template_text
from delivery.workflow import OPTIONAL_STATUSES, Action, Status


def test_loads_and_resolves_relative_paths_against_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfgdir = tmp_path / "cfg"
    cfgdir.mkdir()
    sections = base_sections(tmp_path)
    sections["runtime"]["state_dir"] = "../state-rel"
    sections["repository"]["worktree_root"] = "wt"
    (cfgdir / "d.toml").write_text(render_config(sections, {"config_version": 1}))
    monkeypatch.chdir("/")
    cfg = load_config(cfgdir / "d.toml")
    assert cfg.runtime.state_dir == tmp_path / "state-rel"
    assert cfg.repository.worktree_root == cfgdir / "wt"


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    sections = base_sections(tmp_path)
    sections["runtime"]["max_parallel_runs"] = 1
    (tmp_path / "c.toml").write_text(render_config(sections, {"config_version": 1}))
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "c.toml")
    assert any("runtime.max_parallel_runs" in p and "unknown key" in p for p in exc.value.problems)


def test_no_session_count_setting_exists_anywhere_in_schema() -> None:
    schema = str(Config.model_json_schema()).lower()
    for forbidden in ("max_parallel", "parallel_runs", "max_sessions", "concurrency", "workers"):
        assert forbidden not in schema


def test_paid_api_fallback_cannot_be_enabled(make_config: ConfigFactory) -> None:
    with pytest.raises(ConfigError) as exc:
        make_config(overrides={"claude": {"allow_paid_api_fallback": True}})
    assert "paid API fallback" in " ".join(exc.value.problems)


def test_secret_values_are_not_echoed_in_errors(make_config: ConfigFactory) -> None:
    with pytest.raises(ConfigError) as exc:
        make_config(overrides={"jira": {"token_env": "ATATT3xFfGF0supersecretvalue"}})
    assert "supersecret" not in str(exc.value)


def test_state_dir_inside_checkout_is_rejected(make_config: ConfigFactory, tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        make_config(overrides={"runtime": {"state_dir": str(tmp_path / "app" / "state")}})
    assert "outside the application checkout" in " ".join(exc.value.problems)


def test_status_ids_must_be_ids_and_unique(make_config: ConfigFactory) -> None:
    with pytest.raises(ConfigError) as exc:
        make_config(overrides={"workflow.statuses": {"backlog": "Backlog"}})
    assert "workflow inspect" in " ".join(exc.value.problems)
    with pytest.raises(ConfigError):
        make_config(overrides={"workflow.statuses": {"backlog": "10001"}})


def test_unknown_status_key_rejected(make_config: ConfigFactory) -> None:
    with pytest.raises(ConfigError):
        make_config(overrides={"workflow.statuses": {"ready_for_anything": "99999"}})


def test_action_names_default_to_setup_instructions(make_config: ConfigFactory) -> None:
    cfg = make_config(overrides={"workflow.actions": {"approve_plan": "Plan OK"}})
    assert cfg.workflow.action_name(Action.APPROVE_PLAN) == "Plan OK"
    assert cfg.workflow.action_name(Action.SUBMIT_REFINEMENT) == "Submit for refinement"


def test_identity_key_is_per_site_and_account(make_config: ConfigFactory, tmp_path: Path) -> None:
    a = make_config()
    b = make_config(account="someone-else-01", name="b.toml")
    assert a.identity_key != b.identity_key
    assert a.digest() != b.digest()


def test_template_is_valid_toml_and_loads(tmp_path: Path) -> None:
    text = template_text()
    tomllib.loads(text)
    p = tmp_path / "t.toml"
    p.write_text(text)
    cfg = load_config(p)
    assert cfg.workflow.missing_statuses() == [s for s in Status if s not in OPTIONAL_STATUSES]
    assert "max_parallel" not in text
    assert "ATATT" not in text


def test_template_symlink_in_repo_matches_package_data() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / "config" / "delivery.example.toml").read_text() == template_text()


def test_models_default_and_per_procedure_overrides(make_config: ConfigFactory) -> None:
    cfg = make_config(
        overrides={"claude": {"model": "sonnet"}, "claude.models": {"implement-ticket": "opus"}}
    )
    assert cfg.claude.model_for("implement-ticket") == "opus"
    assert cfg.claude.model_for("review-ticket") == "sonnet"
    assert make_config().claude.model_for("plan-ticket") is None  # Claude Code's own default


@pytest.mark.parametrize(
    "overrides",
    [
        {"claude.models": {"implement": "opus"}},  # not a procedure name
        {"claude.models": {"plan-ticket": "opus --dangerously-skip-permissions"}},
        {"claude": {"model": "-p"}},
    ],
)
def test_model_settings_are_validated(
    make_config: ConfigFactory, overrides: dict[str, dict[str, str]]
) -> None:
    with pytest.raises(ConfigError):
        make_config(overrides=overrides)


def test_a_config_from_before_the_release_stages_were_removed_still_loads(
    make_config: ConfigFactory,
) -> None:
    cfg = make_config(
        overrides={
            "release": {"smoke_commands": {"smoke": ["npm", "run", "smoke"]}},
            "workflow.statuses": {"release_review": "19998", "verifying_release": "19999"},
            "workflow.actions": {"start_release_verification": "Start the check", "approve_release": "OK"},
        }
    )
    assert "verifying_release" not in {s.value for s in cfg.workflow.statuses}
