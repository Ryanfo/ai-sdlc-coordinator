#!/usr/bin/env python3
"""Scriptable stand-in for the Claude Code CLI (``claude -p ... --output-format stream-json``).

Invoked through a wrapper: ``fake_claude.py --scenario <file> <real claude argv...>``.
The scenario maps ``"<TICKET>:<procedure>"`` or ``"<procedure>"`` to a list of behaviours
consumed in order (the last one repeats). Every invocation is recorded for assertions.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path


def arg(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def next_behaviour(scenario_path: Path, ticket: str, procedure: str) -> dict:
    scenario = json.loads(scenario_path.read_text())
    seq = scenario.get(f"{ticket}:{procedure}") or scenario.get(procedure) or [{}]
    counters = scenario_path.parent / "counters"
    counters.mkdir(exist_ok=True)
    n = 0
    while True:
        try:
            fd = os.open(counters / f"{ticket}-{procedure}-{n}", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            n += 1
    return dict(seq[min(n, len(seq) - 1)])


DEFAULT_FILES = {
    "refine-ticket": ("specification.md", "specification", "# Specification\n\n- **AC1**: search works\n"),
    "plan-ticket": ("plan.md", "plan", "# Plan\n\n| AC1 | search.test.ts |\n"),
    "review-ticket": ("review.md", "review", "# Review\n\nAll criteria met.\n"),
    "verify-ticket": ("verification.md", "verification", "# Verification\n\nObserved.\n"),
    "prepare-release": ("release.md", "release", "# Release\n\nSmoke: open the app.\n"),
    "verify-release": ("release-verification.md", "release_verification", "# Release verification\n"),
}


def main() -> int:
    argv = sys.argv[1:]
    scenario_path = Path(argv[argv.index("--scenario") + 1])
    argv = argv[argv.index("--scenario") + 2 :]
    if argv and argv[0] == "auth":
        print(
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "subscriptionType": "pro",
                }
            )
        )
        return 0
    if "--version" in argv:
        print("2.1.278 (Claude Code)")
        return 0
    if "--help" in argv:
        print(
            " ".join(
                [
                    "--plugin-dir",
                    "--json-schema",
                    "--output-format",
                    "--permission-mode",
                    "--settings",
                    "--tools",
                    "--restricted",
                    "--strict-mcp-config",
                    "--no-session-persistence",
                    "--add-dir",
                    "--max-turns",
                    "--session-id",
                ]
            )
        )
        return 0
    prompt = arg(argv, "-p") or ""
    m = re.match(r"^/delivery:([a-z-]+) (\S+)", prompt)
    if not m:
        print("unknown prompt", file=sys.stderr)
        return 2
    procedure, envelope_path = m.group(1), Path(m.group(2))
    # Model --restricted: file tools only reach the working directory and --add-dir paths.
    allowed = [Path(os.getcwd()).resolve()] + [
        Path(argv[i + 1]).resolve() for i, a in enumerate(argv) if a == "--add-dir"
    ]
    if "--restricted" in argv and not any(d in envelope_path.resolve().parents for d in allowed):
        print(
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "s",
                    "plugins": [{"name": "delivery", "path": arg(argv, "--plugin-dir")}],
                }
            )
        )
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "x",
                    "permission_denials": [
                        {"tool_name": "Read", "tool_input": {"file_path": str(envelope_path)}}
                    ],
                    "structured_output": {
                        "schema_version": 1,
                        "contract_id": "x",
                        "run_id": "x",
                        "ticket_key": "PILOT-0",
                        "stage": "refinement",
                        "procedure": procedure,
                        "input_revision": "0" * 64,
                        "outcome": "blocked",
                        "summary": "envelope unreadable",
                        "blocker_reason": "could not read the envelope",
                    },
                }
            )
        )
        return 0
    envelope = json.loads(envelope_path.read_text())
    ticket = envelope["ticket_key"]
    b = next_behaviour(scenario_path, ticket, procedure)

    log_dir = scenario_path.parent / "invocations"
    log_dir.mkdir(exist_ok=True)
    (log_dir / f"{ticket}-{procedure}-{uuid.uuid4().hex[:8]}.json").write_text(
        json.dumps(
            {
                "argv": argv,
                "cwd": os.getcwd(),
                "env_keys": sorted(os.environ),
                "pid": os.getpid(),
                "procedure": procedure,
                "ticket": ticket,
                "started": time.time(),
            }
        )
    )

    if b.get("barrier"):
        # Wait until N sessions have started (proves concurrency), with a safety timeout.
        bdir = scenario_path.parent / "barrier"
        bdir.mkdir(exist_ok=True)
        (bdir / f"{ticket}-{procedure}-{os.getpid()}").touch()
        deadline = time.time() + float(b.get("barrier_timeout", 20))
        while len(list(bdir.iterdir())) < int(b["barrier"]):
            if time.time() > deadline:
                print("barrier timeout", file=sys.stderr)
                return 9
            time.sleep(0.05)
    if b.get("sleep"):
        time.sleep(float(b["sleep"]))

    plugin_dir = arg(argv, "--plugin-dir") or ""
    session = arg(argv, "--session-id") or str(uuid.uuid4())
    if not b.get("no_plugin"):
        init = {
            "type": "system",
            "subtype": "init",
            "session_id": session,
            "plugins": [{"name": "delivery", "path": plugin_dir}],
            "tools": (arg(argv, "--tools") or "").split(","),
        }
    else:
        init = {
            "type": "system",
            "subtype": "init",
            "session_id": session,
            "plugins": [],
            "plugin_errors": [{"plugin": "delivery", "type": "load", "message": "missing"}],
        }
    if b.get("malformed"):
        print("this is not json")
        return 0
    print(json.dumps(init))
    if b.get("usage_limit"):
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "Claude usage limit reached. Your limit resets at 5pm.",
                }
            )
        )
        return 1
    if b.get("auth_error"):
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "Not logged in · Please run /login",
                }
            )
        )
        return 1
    if b.get("exit_code"):
        print("boom", file=sys.stderr)
        return int(b["exit_code"])

    out_dir = Path(envelope["output"]["artifact_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = []
    if procedure in DEFAULT_FILES and not b.get("skip_output"):
        name, kind, text = DEFAULT_FILES[procedure]
        (out_dir / name).write_text(b.get("content", text))
        artifacts.append({"path": b.get("artifact_path", name), "kind": kind})
    for rel, text in (b.get("edit") or {}).items():
        p = Path(os.getcwd()) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        artifacts.append({"path": rel, "kind": "code"})
    if procedure == "implement-ticket" and not b.get("edit") and not b.get("no_changes"):
        p = Path(os.getcwd()) / "src" / f"{ticket.lower()}.ts"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"export const {ticket.replace('-', '_').lower()} = true;\n")
        artifacts.append({"path": f"src/{ticket.lower()}.ts", "kind": "code"})
    for rel, text in (b.get("tamper") or {}).items():
        (Path(os.getcwd()) / rel).write_text(text)
    if b.get("loop"):
        # Stuck: the same failing command with the same output, until the coordinator stops it.
        for i in range(int(b["loop"])):
            print(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": f"t{i}",
                                    "name": "Bash",
                                    "input": {"command": "npm run test:e2e"},
                                }
                            ]
                        },
                    }
                ),
                flush=True,
            )
            print(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": f"t{i}",
                                    "content": "Error: port 5173 in use",
                                }
                            ]
                        },
                    }
                ),
                flush=True,
            )
        time.sleep(60)
        return 0
    if b.get("max_turns"):
        # Ran out of turns part-way: edits above stay in the working copy, no final result.
        print(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Still working on the e2e tests."}]},
                }
            )
        )
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_max_turns",
                    "is_error": True,
                    "num_turns": 41,
                    "duration_ms": 1000,
                    "permission_denials": [],
                }
            )
        )
        return 1

    skill = Path(plugin_dir) / "skills" / procedure / "SKILL.md"
    contract = re.search(r"contract_id:\s*`([^`]+)`", skill.read_text()).group(1)  # type: ignore[union-attr]
    result = {
        "schema_version": 1,
        "contract_id": b.get("contract_id", contract),
        "run_id": envelope["run_id"],
        "ticket_key": ticket,
        "stage": envelope["stage"],
        "procedure": procedure,
        "input_revision": envelope["input_revision"],
        "outcome": b.get("outcome", "completed"),
        "summary": b.get("summary", f"{procedure} done for {ticket}"),
        "artifacts": artifacts,
        "questions": b.get("questions", []),
        "findings": b.get("findings", []),
        "evidence": b.get(
            "evidence",
            [
                {
                    "criterion_id": "AC1",
                    "description": "ok",
                    "status": "met" if procedure.startswith(("verify", "review")) else "defined",
                }
            ],
        ),
        "worker_checks": b.get("worker_checks", []),
        "blocker_reason": b.get("blocker_reason", ""),
    }
    if procedure == "plan-ticket":
        result["footprint"] = b.get(
            "footprint", {"paths": [f"src/{ticket.lower()}.ts"], "components": [f"comp-{ticket}"]}
        )
    if procedure == "prepare-release":
        result["release"] = {
            "candidate_sha": envelope["source"]["candidate_sha"],
            "smoke_steps": ["open"],
            "rollback_steps": ["revert"],
        }
    result.update(b.get("override", {}))
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "session_id": session,
                "num_turns": 3,
                "structured_output": result,
                "permission_denials": b.get("denials", []),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
