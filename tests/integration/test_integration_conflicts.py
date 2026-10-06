"""Merge conflicts during verification: with another ticket (merge order) or with the base."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import DEV
from delivery.supervisor import Supervisor
from delivery.workflow import Status
from gitutil import external_commit, sh
from harness import World, make_world, step


async def _to_development(w: World, sup: Supervisor, *keys: str) -> None:
    for key in keys:
        w.new_ticket(key)
        w.submit(key)
    await step(sup)
    for key in keys:
        w.decide(key, f"APPROVE SPEC {w.token(key, 'SPEC')}", Status.READY_PLANNING)
    await step(sup)
    for key in keys:
        w.decide(key, f"APPROVE PLAN {w.token(key, 'PLAN')}", Status.READY_DEVELOPMENT)


def _run_file(w: World, key: str, stage: str, name: str) -> list[Path]:
    """The file from each run of ``stage``, oldest run first. Run IDs only resolve to the second
    (then a random suffix), so two runs started in the same second do not sort by name."""
    found = Path(w.cfg.runtime.state_dir).rglob(f"runs/{key}/*{stage}*/inputs/{name}")
    return sorted(found, key=lambda p: p.stat().st_mtime_ns)


async def test_conflicting_tickets_both_reach_code_review_with_the_conflict_flagged(tmp_path: Path) -> None:
    # Two tickets edit the same lines differently. Neither candidate is wrong; failing both
    # would deadlock them, each blaming the other.
    w = make_world(tmp_path)
    fp = {"footprint": {"paths": ["src/app.ts"]}}
    w.scenario(
        {
            "plan-ticket": [fp],
            "PILOT-1:implement-ticket": [{"edit": {"src/app.ts": "export const x = 2;\n"}}],
            "PILOT-2:implement-ticket": [{"edit": {"src/app.ts": "export const x = 3;\n"}}],
        }
    )
    async with Supervisor(w.deps) as sup:
        await _to_development(w, sup, "PILOT-1", "PILOT-2")
        await step(sup)  # both candidates published
        await step(sup)  # both verified; each integration tree meets the other's candidate
        for key, other in (("PILOT-1", "PILOT-2"), ("PILOT-2", "PILOT-1")):
            assert w.jira.status_of(key) is Status.CODE_REVIEW, w.last_comment(key)
            gate = next(c for c in w.comments(key) if "Candidate ready for code review" in c)
            assert "Merge conflicts" in gate and "resolve them in the PR when you merge" in gate
            assert f"{other}'s candidate" in gate and "src/app.ts" in gate
    # The approved specification and plan reach Claude as two different files.
    env = json.loads(_run_file(w, "PILOT-1", "development", "envelope-implement-ticket.json")[0].read_text())
    paths = {a["kind"]: Path(a["path"]) for a in env["approved_artefacts"]}
    assert paths["specification"] != paths["plan"]
    assert "# Specification" in paths["specification"].read_text()
    assert "# Plan" in paths["plan"].read_text()


async def test_conflict_with_base_is_flagged_and_never_blocks(tmp_path: Path) -> None:
    w = make_world(tmp_path, checks={"unit": ["sh", "-c", "! grep -rq bug src"]})
    finding = {"severity": "minor", "description": "Name the constant."}
    w.scenario(
        {
            "plan-ticket": [{"footprint": {"paths": ["src/app.ts"]}}],
            "implement-ticket": [
                {"edit": {"src/app.ts": "export const x = 2; // bug\n"}},
                {"outcome": "blocked", "blocker_reason": "unsure which constant", "no_changes": True},
                {"edit": {"src/app.ts": "export const x = 2;\n"}},
            ],
            "review-ticket": [{"findings": [{"id": "F1", **finding}, {"id": "F2", **finding}]}],
        }
    )
    async with Supervisor(w.deps) as sup:
        await _to_development(w, sup, "PILOT-1")
        await step(sup)  # candidate c1
        external_commit(tmp_path, w.origin, "main", "src/app.ts", "export const x = 9;\n", "other")
        await step(sup)  # verification: c1 fails its check; main's conflict is only flagged
        assert w.jira.status_of("PILOT-1") is Status.CHANGES_REQUESTED
        failed = w.last_comment("PILOT-1")
        assert "R1: coordinator check unit (candidate) failed" in failed
        assert (
            "conflicts in" not in failed.split("Why it failed")[1].split("Merge conflicts")[0]
        )  # not a reason
        assert "Merge conflicts" in failed
        assert "Submit implementation changes" in failed and "Submit follow-up changes" in failed
        assert "SUBMIT CHANGES PILOT-1-CODE-c1" in failed

        # Choosing Submit follow-up changes by hand verifies the same code again, and says so.
        w.jira.human_move("PILOT-1", Status.READY_VERIFICATION, DEV)
        await step(sup)
        started = next(c for c in reversed(w.comments("PILOT-1")) if "Verification started" in c)
        assert "unchanged since its last verification" in started
        assert w.jira.status_of("PILOT-1") is Status.CHANGES_REQUESTED

        # Fix only F1 (the coordinator's R-items always go along). The session stops on a
        # blocker; after Resume the next session still gets the same items.
        w.jira.human_comment("PILOT-1", DEV, "SUBMIT CHANGES PILOT-1-CODE-c1\nF1: rename it")
        w.jira.human_move("PILOT-1", Status.READY_DEVELOPMENT, DEV)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.BLOCKED, w.last_comment("PILOT-1")
        assert "Next action" in w.last_comment("PILOT-1")
        w.jira.human_comment("PILOT-1", DEV, "FOR CLAUDE development\nUse the constant x.")
        w.jira.human_move("PILOT-1", Status.READY_DEVELOPMENT, DEV)
        await step(sup)  # main still conflicts: development carries on without it
        assert w.record("PILOT-1").candidate_number == 2, w.last_comment("PILOT-1")
        assert "was not merged in" in w.last_comment("PILOT-1")
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.CODE_REVIEW, w.last_comment("PILOT-1")
        gate = w.last_comment("PILOT-1")
        assert (
            "Merge conflicts" in gate
            and "resolve them in the PR when you merge" in gate
            and "src/app.ts" in gate
        )
    envs = _run_file(w, "PILOT-1", "development", "envelope-implement-ticket.json")
    items = [set(json.loads(e.read_text())["feedback_items"]) for e in envs[1:]]
    assert items == [{"F1", "R1", "R2"}] * 2  # F2 was not selected
    notes = json.loads(envs[-1].read_text())["notes"]
    assert [n["body"] for n in notes] == ["Use the constant x."]


async def test_latest_base_merged_cleanly_is_published_when_nothing_else_changes(tmp_path: Path) -> None:
    w = make_world(tmp_path, checks={"unit": ["test", "-f", "src/ready.ts"]})
    w.scenario({"implement-ticket": [{}, {"no_changes": True}]})
    async with Supervisor(w.deps) as sup:
        await _to_development(w, sup, "PILOT-1")
        await step(sup)
        await step(sup)  # c1 fails: it needs something only main can provide
        assert w.jira.status_of("PILOT-1") is Status.CHANGES_REQUESTED
        external_commit(tmp_path, w.origin, "main", "src/ready.ts", "export {};\n", "fix")
        # Updating the branch with the latest main is the whole change.
        w.jira.human_move("PILOT-1", Status.READY_DEVELOPMENT, DEV)
        await step(sup)
        rec = w.record("PILOT-1")
        assert rec.candidate_number == 2, w.last_comment("PILOT-1")
        assert rec.candidate_sha == sh("rev-parse", "refs/heads/feature/PILOT-1", cwd=w.origin)
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.CODE_REVIEW, w.last_comment("PILOT-1")


async def test_claude_resolves_a_conflict_with_the_base_when_changes_are_requested(tmp_path: Path) -> None:
    w = make_world(tmp_path, checks={"unit": ["sh", "-c", "! grep -rq bug src"]})
    w.scenario(
        {
            "plan-ticket": [{"footprint": {"paths": ["src/app.ts"]}}],
            "implement-ticket": [
                {"edit": {"src/app.ts": "export const x = 2; // bug\n"}},
                {"edit": {"src/extra.ts": "export const extra = 1;\n"}},
            ],
            # Keeps main's value and this ticket's intent, without the conflict markers.
            "resolve-conflicts": [{"edit": {"src/app.ts": "export const x = 9;\n"}}],
        }
    )
    async with Supervisor(w.deps) as sup:
        await _to_development(w, sup, "PILOT-1")
        await step(sup)  # candidate c1
        main = external_commit(tmp_path, w.origin, "main", "src/app.ts", "export const x = 9;\n", "other")
        await step(sup)  # verification fails on the check; the conflict with main is flagged
        assert w.jira.status_of("PILOT-1") is Status.CHANGES_REQUESTED
        assert "Or have Claude do it" in w.last_comment("PILOT-1")
        w.jira.human_move("PILOT-1", Status.READY_DEVELOPMENT, DEV)
        await step(sup)  # merges main, Claude resolves the conflict, then makes the changes
        rec = w.record("PILOT-1")
        assert rec.candidate_number == 2, w.last_comment("PILOT-1")
        ready = w.last_comment("PILOT-1")
        assert "Claude resolved the conflicts in src/app.ts" in ready
        assert "was not merged into this candidate" not in ready
        head = rec.candidate_sha
        assert head
        assert sh("merge-base", "--is-ancestor", main, head, cwd=w.origin) == ""  # main is in it
        assert sh("show", f"{head}:src/app.ts", cwd=w.origin) == "export const x = 9;"
        assert sh("show", f"{head}:src/extra.ts", cwd=w.origin) == "export const extra = 1;"
        resolve = json.loads(
            _run_file(w, "PILOT-1", "development", "envelope-resolve-conflicts.json")[0].read_text()
        )
        assert list(resolve["feedback_items"]) == ["R1"] and "src/app.ts" in resolve["feedback_items"]["R1"]
        await step(sup)
        assert w.jira.status_of("PILOT-1") is Status.CODE_REVIEW, w.last_comment("PILOT-1")
        assert "Merge conflicts to resolve when merging" not in w.last_comment("PILOT-1")


async def test_conflict_help_can_be_turned_off(tmp_path: Path) -> None:
    w = make_world(
        tmp_path,
        checks={"unit": ["sh", "-c", "! grep -rq bug src"]},
        extra={"flow": {"resolve_conflicts": False}},
    )
    w.scenario(
        {
            "implement-ticket": [
                {"edit": {"src/app.ts": "export const x = 2; // bug\n"}},
                {"edit": {"src/extra.ts": "export const extra = 1;\n"}},
            ]
        }
    )
    async with Supervisor(w.deps) as sup:
        await _to_development(w, sup, "PILOT-1")
        await step(sup)
        external_commit(tmp_path, w.origin, "main", "src/app.ts", "export const x = 9;\n", "other")
        await step(sup)
        assert "Or have Claude do it" not in w.last_comment("PILOT-1")
        w.jira.human_move("PILOT-1", Status.READY_DEVELOPMENT, DEV)
        await step(sup)
        assert "was not merged in" in w.last_comment("PILOT-1")
    assert not [i for i in w.invocations() if "/delivery:resolve-conflicts" in " ".join(i["argv"])]
