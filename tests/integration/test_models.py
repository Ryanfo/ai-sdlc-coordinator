"""Per-procedure model selection reaches each Claude session and is shown in Jira."""

from __future__ import annotations

from pathlib import Path

from delivery.supervisor import Supervisor
from delivery.workflow import Status
from harness import make_world, step

KEY = "PILOT-1"


def _model(argv: list[str]) -> str | None:
    return argv[argv.index("--model") + 1] if "--model" in argv else None


async def test_each_procedure_runs_on_its_configured_model(tmp_path: Path) -> None:
    w = make_world(
        tmp_path,
        extra={"claude": {"model": "sonnet"}, "claude.models": {"plan-ticket": "opus"}},
    )
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
        w.decide(KEY, f"APPROVE SPEC {w.token(KEY, 'SPEC')}", Status.READY_PLANNING)
        await step(sup)
    used = {
        inv["argv"][inv["argv"].index("-p") + 1].split()[0]: _model(inv["argv"]) for inv in w.invocations()
    }
    assert used == {"/delivery:refine-ticket": "sonnet", "/delivery:plan-ticket": "opus"}
    # The model is run configuration, not a decision: the ticket does not carry it.
    assert not any("Model" in c for c in w.comments(KEY) if "started." in c)


async def test_without_a_model_setting_no_model_flag_is_passed(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.new_ticket(KEY)
    w.submit(KEY)
    async with Supervisor(w.deps) as sup:
        await step(sup)
    assert [_model(i["argv"]) for i in w.invocations()] == [None]
    assert "Model" not in next(c for c in w.comments(KEY) if "started." in c)
