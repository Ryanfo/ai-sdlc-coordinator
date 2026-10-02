from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import DEV, STATUS_IDS, ConfigFactory
from delivery import cli
from delivery.doctor import Report, check_config, inspect_workflow
from delivery.github import GhClient
from delivery.ports import NotFound
from delivery.workflow import Status
from fakes.jira import FakeJira

FAKE_GH = """#!{py}
import json, sys
args = sys.argv[1:]
path = [a for a in args if a.startswith("repos/") or a == "user"][0]
log = open({log!r}, "a"); log.write(json.dumps(args) + "\\n"); log.close()
body = sys.stdin.read() if "--input" in args else None
if path == "user":
    print(json.dumps({{"login": "dev-bot"}}))
elif "/rules/branches/" in path:
    print(json.dumps([[{{"type": "pull_request", "parameters": {{"required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": True}}}}, {{"type": "required_status_checks", "parameters":
        {{"strict_required_status_checks_policy": True, "required_status_checks": [{{"context": "unit"}}]}}}},
        {{"type": "non_fast_forward"}}, {{"type": "deletion"}}]]))
elif path.endswith("/check-runs?per_page=100"):
    print(json.dumps([{{"check_runs": [{{"id": 1, "name": "unit", "head_sha": "a", "status": "completed",
        "conclusion": "success", "app": {{"slug": "github-actions"}}}}]}},
        {{"check_runs": [{{"id": 2, "name": "e2e", "head_sha": "a", "status": "completed",
        "conclusion": "success", "app": {{"slug": "github-actions"}}}}]}}]))
elif path.endswith("/pulls/404"):
    sys.stderr.write("gh: Not Found (HTTP 404)\\n"); sys.exit(1)
elif path.endswith("/pulls") and "POST" in args:
    b = json.loads(body)
    print(json.dumps({{"number": 7, "html_url": "u", "state": "open", "head": {{"ref": b["head"], "sha": "h"}},
        "base": {{"ref": b["base"], "sha": "b"}}, "user": {{"login": "dev-bot"}}, "title": b["title"]}}))
elif path.endswith("/pulls/7"):
    print(json.dumps({{"number": 7, "html_url": "u", "state": "closed", "merged_at": "2026-10-01T10:00:00Z",
        "merge_commit_sha": "m", "merged_by": {{"login": "human"}}, "head": {{"ref": "feature/P-1", "sha": "h"}},
        "base": {{"ref": "main", "sha": "b"}}, "user": {{"login": "dev-bot"}}}}))
else:
    sys.stderr.write("unexpected " + path); sys.exit(1)
"""


@pytest.fixture
def gh(tmp_path: Path) -> tuple[GhClient, Path]:
    log = tmp_path / "gh.log"
    exe = tmp_path / "gh"
    exe.write_text(FAKE_GH.format(py=sys.executable, log=str(log)))
    exe.chmod(0o755)
    return GhClient("example/app", executable=str(exe)), log


async def test_gh_adapter_parses_and_paginates(gh: tuple[GhClient, Path]) -> None:
    client, log = gh
    assert await client.viewer_login() == "dev-bot"
    runs = await client.check_runs("a")
    assert [r.name for r in runs] == ["unit", "e2e"]
    pr = await client.create_pr("feature/P-1", "main", "P-1: x", "body")
    assert pr.number == 7 and pr.head_ref == "feature/P-1"
    merged = await client.get_pr(7)
    assert merged.merged and merged.merge_commit_sha == "m" and merged.merged_by == "human"
    with pytest.raises(NotFound):
        await client.get_pr(404)
    prot = await client.branch_protection("main")
    assert prot and prot.required_approving_reviews == 1 and prot.strict_up_to_date
    assert not prot.allow_force_pushes and not prot.allow_deletions and prot.source == "rulesets"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert all("--paginate" in c for c in calls if any("check-runs" in a for a in c))
    assert not any("merge" in a for c in calls for a in c if a.startswith("repos/") and a.endswith("/merge"))


def test_cli_init_never_overwrites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "delivery.local.toml"
    assert cli.main(["init", "--config", str(target)]) == 0
    text = target.read_text()
    assert "plugins/delivery" in text and "/absolute/path/to/delivery-platform" not in text
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    target.write_text("mine")
    assert cli.main(["init", "--config", str(target)]) == cli.EXIT_CONFIG
    assert target.read_text() == "mine"


def test_cli_reports_config_problems_without_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(
        'config_version = 1\n[identity]\ndeveloper_jira_account_id = "x"\nsecret = "ATATT3xFfGF0leak"\n'
    )
    assert cli.main(["status", "--config", str(bad)]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "Configuration problems" in err and "ATATT3" not in err


def test_cli_status_without_supervisor(
    make_config: ConfigFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_config()
    assert cli.main(["status", "--config", str(cfg.source_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["supervisor_running"] is False and data["runs"] == []


def test_cli_dispatch_pause_persists_without_supervisor(
    make_config: ConfigFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_config()
    assert cli.main(["dispatch", "pause", "--config", str(cfg.source_path), "--reason", "laptop busy"]) == 0
    from delivery.journal import JournalStore

    rec = JournalStore(cfg.runtime.state_dir, cfg.identity_key).load_supervisor()
    assert rec and rec.dispatch_paused and rec.pause_reason == "laptop busy"


def test_cli_run_refuses_unmapped_workflow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "t.toml"
    cli.main(["init", "--config", str(target)])
    assert cli.main(["run", "--config", str(target)]) == cli.EXIT_CONFIG
    assert "workflow inspect" in capsys.readouterr().err


def test_doctor_config_flags_placeholders_and_unmapped(tmp_path: Path) -> None:
    target = tmp_path / "t.toml"
    cli.main(["init", "--config", str(target)])
    from delivery.config import load_config

    report = Report()
    check_config(load_config(target), report)
    levels = {c.name: c.level for c in report.checks}
    assert levels["placeholders"] == "fail" and levels["status mapping"] == "fail"
    assert not report.ready


async def test_workflow_inspect_resolves_ids_and_flags_wrong_transitions(make_config: ConfigFactory) -> None:
    cfg = make_config()
    jira = FakeJira(STATUS_IDS, me=DEV)
    jira.create("PILOT-1", "s", "d", DEV, status=Status.SPECIFICATION_REVIEW)
    jira.create("PILOT-2", "s", "d", DEV, status=Status.BACKLOG)
    jira.drop_routes.add((Status.SPECIFICATION_REVIEW, Status.READY_REFINEMENT))  # missing change route
    wf = await inspect_workflow(cfg, jira)
    assert wf.resolved["ready_refinement"] == STATUS_IDS[Status.READY_REFINEMENT]
    assert not wf.missing and not wf.ambiguous and wf.resume_field == "customfield_10050"
    assert wf.transitions_checked["backlog"]["missing"] == []
    assert any(
        "Request specification changes" in m
        for m in wf.transitions_checked["specification_review"]["missing"]
    )
    assert "[workflow.statuses]" in wf.toml() and f'backlog = "{STATUS_IDS[Status.BACKLOG]}"' in wf.toml()


async def test_git_push_check_is_a_dry_run(tmp_path: Path, make_config: ConfigFactory) -> None:
    from delivery.doctor import check_git_push
    from gitutil import make_origin, sh

    origin = make_origin(tmp_path)
    cfg = make_config()
    report = Report()
    await check_git_push(cfg, report, url=str(origin))
    assert report.checks[-1].level == "ok" and "nothing was pushed" in report.checks[-1].detail
    heads = sh("--git-dir", str(origin), "for-each-ref", "--format=%(refname)", "refs/heads", cwd=tmp_path)
    assert heads.splitlines() == ["refs/heads/main"]  # the dry run created no branch
    await check_git_push(cfg, report, url=str(tmp_path / "missing.git"))
    assert report.checks[-1].level == "fail" and "gh auth setup-git" in report.checks[-1].action
