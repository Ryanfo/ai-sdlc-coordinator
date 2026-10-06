"""The shared project file, the default plugin path and comment-preserving config edits."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from conftest import base_sections, render_config
from delivery import setup
from delivery.config import ConfigError, bundled_plugin_path, load_config, split_personal
from delivery.setup import export_project


def _full(tmp_path: Path) -> Path:
    sections = base_sections(tmp_path)
    del sections["claude"]["plugin_path"]
    sections["jira"]["email"] = "dev@example.com"
    path = tmp_path / "full.toml"
    path.write_text(render_config(sections, {"config_version": 1}))
    return path


def test_plugin_path_defaults_to_this_installation(tmp_path: Path) -> None:
    cfg = load_config(_full(tmp_path))
    assert cfg.claude.plugin_path == bundled_plugin_path()
    assert (cfg.claude.plugin_path / ".claude-plugin" / "plugin.json").exists()


def test_a_project_file_and_a_short_personal_config_give_the_same_settings(tmp_path: Path) -> None:
    full = load_config(_full(tmp_path))
    project = tmp_path / "team" / "PILOT.toml"
    export_project(_full(tmp_path), project)
    team = tomllib.loads(project.read_text())
    assert "identity" not in team and "email" not in team["jira"]
    assert "checkout_path" not in team["repository"] and "state_dir" not in team.get("runtime", {})
    personal = tmp_path / "me.toml"
    sections = {
        "identity": base_sections(tmp_path)["identity"],
        "jira": {"email": "dev@example.com"},
        "repository": {"checkout_path": str(tmp_path / "app"), "worktree_root": str(tmp_path / "worktrees")},
        # This machine's room and alerts are personal too (the test config turns them off).
        "runtime": {
            "state_dir": str(tmp_path / "state"),
            "min_free_disk_gb": 0,
            "hold_on_memory_pressure": False,
            "keep_awake": False,
        },
        "notifications": {"desktop": False},
    }
    personal.write_text(render_config(sections, {"config_version": 1, "project": "team/PILOT.toml"}))
    shared = load_config(personal)
    assert shared.project_path == project
    assert "keep_awake" not in team["runtime"] and "desktop" not in team.get("notifications", {})
    assert shared.model_dump() == full.model_dump()
    assert shared.digest() == full.digest()


def test_personal_values_win_and_the_project_file_holds_nothing_personal(tmp_path: Path) -> None:
    project = tmp_path / "p.toml"
    export_project(_full(tmp_path), project)
    personal = tmp_path / "me.toml"
    sections = {
        "identity": base_sections(tmp_path)["identity"],
        "jira": {"email": "dev@example.com", "required_label": "mine"},
        "repository": {"checkout_path": str(tmp_path / "app"), "worktree_root": str(tmp_path / "w")},
        "runtime": {"state_dir": str(tmp_path / "s"), "poll_seconds": 30},
    }
    personal.write_text(render_config(sections, {"config_version": 1, "project": str(project)}))
    cfg = load_config(personal)
    assert cfg.jira.required_label == "mine" and cfg.runtime.poll_seconds == 30
    assert cfg.jira.project_key == "PILOT"  # from the project file

    project.write_text(project.read_text() + '\n[identity]\nworker_id = "x"\n')
    with pytest.raises(ConfigError) as err:
        load_config(personal)
    assert err.value.path == project and "identity is personal" in err.value.problems[0]

    personal.write_text(personal.read_text().replace(str(project), str(tmp_path / "missing.toml")))
    with pytest.raises(ConfigError, match="not found"):
        load_config(personal)


def test_split_personal() -> None:
    team, mine = split_personal(
        {
            "config_version": 1,
            "identity": {"worker_id": "a"},
            "jira": {"email": "e@x.io", "base_url": "https://x.atlassian.net"},
            "claude": {"model": "opus", "interactive": {"enabled": True}},
        }
    )
    assert team == {"jira": {"base_url": "https://x.atlassian.net"}, "claude": {"model": "opus"}}
    assert mine["identity"] == {"worker_id": "a"} and mine["claude"] == {"interactive": {"enabled": True}}


def test_set_top_and_dumps() -> None:
    text = setup.set_top('# mine\nconfig_version = 1\n\n[jira]\nemail = "a@b.co"\n', "project", "/p.toml")
    assert tomllib.loads(text) == {"config_version": 1, "project": "/p.toml", "jira": {"email": "a@b.co"}}
    assert text.index("project") < text.index("[jira]") and text.startswith("# mine")
    assert tomllib.loads(setup.set_top(text, "project", "/q.toml"))["project"] == "/q.toml"
    data = {
        "jira": {"base_url": "https://x", "fields": {"resume_stage": "customfield_1"}},
        "checks": {"commands": {"unit": ["npm", "test"]}},
        "claude": {"models": {"plan-ticket": "opus"}, "max_turns": 10},
    }
    out = setup.dumps(data, "header\n\nmore")
    assert tomllib.loads(out) == data and out.startswith("# header\n#\n# more\n")
