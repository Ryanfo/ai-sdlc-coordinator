from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from delivery.config import Config, load_config
from delivery.workflow import Status

sys.path.insert(0, str(Path(__file__).parent))

DEV = "dev-account-0001"
OTHER_DEV = "dev-account-0002"
APPROVER = "approver-0001"
STATUS_IDS = {s: str(10000 + i) for i, s in enumerate(Status)}


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_config(sections: dict[str, dict[str, Any]], top: dict[str, Any]) -> str:
    lines = [f"{k} = {_toml_value(v)}" for k, v in top.items()]
    for name, body in sections.items():
        lines.append(f"\n[{name}]")
        lines.extend(f"{k} = {_toml_value(v)}" for k, v in body.items())
    return "\n".join(lines) + "\n"


def base_sections(tmp: Path, account: str = DEV) -> dict[str, dict[str, Any]]:
    return {
        "identity": {"developer_jira_account_id": account, "worker_id": "test-laptop"},
        "jira": {
            "base_url": "https://example.atlassian.net",
            "project_key": "PILOT",
            "supported_issue_types": ["Story", "Task", "Bug"],
        },
        "jira.fields": {"resume_stage": "customfield_10050"},
        "repository": {
            "url": "https://github.com/example/app.git",
            "base_branch": "main",
            "checkout_path": str(tmp / "app"),
            "worktree_root": str(tmp / "worktrees"),
        },
        "runtime": {"state_dir": str(tmp / "state"), "poll_seconds": 10},
        "claude": {"plugin_path": str(tmp / "plugin"), "executable": "claude"},
        "approvals": {"jira_account_ids": [APPROVER], "github_logins": ["reviewer"]},
        "checks.commands": {"unit": ["true"]},
        "checks.ci": {"required_names": ["unit"]},
        "workflow.statuses": {s.value: sid for s, sid in STATUS_IDS.items()},
    }


ConfigFactory = Callable[..., Config]


@pytest.fixture
def make_config(tmp_path: Path) -> ConfigFactory:
    def factory(
        account: str = DEV,
        overrides: dict[str, dict[str, Any]] | None = None,
        name: str = "delivery.local.toml",
        directory: Path | None = None,
    ) -> Config:
        d = directory or tmp_path
        sections = base_sections(d, account)
        for sec, body in (overrides or {}).items():
            sections.setdefault(sec, {}).update(body)
        path = d / name
        path.write_text(render_config(sections, {"config_version": 1}))
        return load_config(path)

    return factory
