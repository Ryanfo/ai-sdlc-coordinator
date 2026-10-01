"""Coordinator-run checks. These, not the worker's claims, are authoritative evidence."""

from __future__ import annotations

from pathlib import Path

from delivery.models import CheckResult
from delivery.proc import ProcessStartError, base_child_env, run_process
from delivery.resources import port_env


async def run_checks(
    commands: dict[str, list[str]],
    names: list[str],
    *,
    worktree: Path,
    log_dir: Path,
    target: str,
    sha: str | None,
    ports: dict[str, int],
    tmp_dir: Path,
    timeout: float,
    tree_sha: str | None = None,
    base_sha: str | None = None,
) -> list[CheckResult]:
    """Run configured checks in order. A missing command or start failure is a failure."""
    env = base_child_env({"CI": "1", "TMPDIR": str(tmp_dir), "NODE_ENV": "test", **port_env(ports)})
    results: list[CheckResult] = []
    for name in names:
        argv = commands.get(name)
        log = log_dir / f"{target}-{name}.log"
        if not argv:
            results.append(
                CheckResult(
                    name=name,
                    source="coordinator",
                    target=target,
                    sha=sha,
                    conclusion="missing",
                )
            )
            continue
        try:
            res = await run_process(
                argv,
                cwd=worktree,
                env=env,
                timeout=timeout,
                stdout_path=log,
                stderr_path=log.with_suffix(".err.log"),
            )
        except ProcessStartError as exc:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(str(exc))
            results.append(
                CheckResult(
                    name=name,
                    source="coordinator",
                    target=target,
                    sha=sha,
                    conclusion="error",
                    log_path=str(log),
                )
            )
            continue
        conclusion = "timed_out" if res.timed_out else ("passed" if res.returncode == 0 else "failed")
        results.append(
            CheckResult(
                name=name,
                source="coordinator",
                target=target,
                sha=sha,
                tree_sha=tree_sha,
                base_sha=base_sha,
                conclusion=conclusion,
                exit_code=res.returncode,
                log_path=str(log),
                producer="delivery-coordinator",
                duration_seconds=round(res.duration, 2),
            )
        )
    return results


def all_passed(results: list[CheckResult]) -> bool:
    return bool(results) and all(r.conclusion == "passed" for r in results)
