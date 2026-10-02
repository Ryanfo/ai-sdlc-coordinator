"""Validation of a worker's structured result against the envelope it was given.

Kept free of heavy imports: the interactive session's Stop hook (delivery.session_hook) runs
this on every stop to decide whether Claude has finished.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from delivery.models import StageResult
from delivery.workflow import Stage


class OutputInvalid(Exception):
    pass


def validate_result(
    raw: dict[str, Any] | None,
    *,
    contract_id: str,
    procedure: str,
    run_id: str,
    ticket: str,
    stage: Stage,
    input_revision: str,
) -> StageResult:
    if raw is None:
        raise OutputInvalid("no structured result")
    try:
        result = StageResult.model_validate(raw)
    except ValidationError as exc:
        raise OutputInvalid(
            f"result does not match the contract ({exc.error_count()} errors): "
            + "; ".join(e["msg"] for e in exc.errors()[:5])
        ) from None
    problems = []
    if result.contract_id != contract_id:
        problems.append(
            f"contract_id {result.contract_id!r} is not {contract_id!r} "
            "(procedure did not load or is a different version)"
        )
    if result.procedure != procedure:
        problems.append(f"procedure {result.procedure!r} is not {procedure!r}")
    if result.run_id != run_id or result.ticket_key != ticket:
        problems.append("run or ticket identity does not match the envelope")
    if result.stage is not stage:
        problems.append(f"stage {result.stage.value} is not {stage.value}")
    if result.input_revision != input_revision:
        problems.append("input_revision does not match the envelope")
    if problems:
        raise OutputInvalid("; ".join(problems))
    return result
