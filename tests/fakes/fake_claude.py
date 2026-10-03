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


def write_outputs(b: dict, procedure: str, ticket: str, envelope: dict) -> list[dict]:
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
    return artifacts


def build_result(
    b: dict, procedure: str, ticket: str, envelope: dict, plugin_dir: str, artifacts: list[dict]
) -> dict:
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
    return result


def interactive(argv: list[str], scenario_path: Path) -> int:
    """Stand-in for an interactive session (no -p), run by the coordinator inside tmux.

    Runs the hooks from --settings exactly as Claude Code would, writes a transcript in Claude
    Code's format, and then reads lines typed into the pane: ``EDIT <path> <text>`` changes a
    file in the worktree, ``/exit`` ends the session, anything else gets a short reply.
    """
    import subprocess

    prompt = argv[0]
    settings = json.loads(Path(arg(argv, "--settings") or "").read_text())
    hooks = settings.get("hooks") or {}
    session = arg(argv, "--session-id") or str(uuid.uuid4())
    plugin_dir = arg(argv, "--plugin-dir") or ""
    transcripts = scenario_path.parent / "transcripts"
    transcripts.mkdir(exist_ok=True)
    transcript = transcripts / f"{session}.jsonl"
    log_dir = scenario_path.parent / "invocations"
    log_dir.mkdir(exist_ok=True)

    def t(entry: dict) -> None:
        with transcript.open("a") as fh:
            fh.write(json.dumps({"sessionId": session, "uuid": str(uuid.uuid4()), **entry}) + "\n")

    def say(text: str, **extra: object) -> None:
        t(
            {
                "type": "assistant",
                "message": {"id": str(uuid.uuid4()), "content": [{"type": "text", "text": text}]},
                **extra,
            }
        )

    def hook(name: str, **payload: object) -> dict | None:
        reply = None
        for group in hooks.get(name, []):
            for h in group.get("hooks", []):
                # Hook commands are shell strings, run through a shell as Claude Code does.
                res = subprocess.run(  # noqa: S602
                    h["command"],
                    shell=True,
                    input=json.dumps({"session_id": session, "transcript_path": str(transcript), **payload}),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.stdout.strip():
                    reply = json.loads(res.stdout)
        return reply

    hook("SessionStart", source="startup")
    m = re.match(r"^/delivery:([a-z-]+) (\S+)", prompt)
    if not m:
        t({"type": "system", "subtype": "informational", "content": f"Unknown command: {prompt.split()[0]}"})
        return wait_for_input(hook, t, say)
    procedure, envelope_path = m.group(1), Path(m.group(2))
    envelope = json.loads(envelope_path.read_text())
    ticket = envelope.get("ticket_key", "doctor-probe")
    b = next_behaviour(scenario_path, ticket, procedure)
    (log_dir / f"{time.time_ns()}-{ticket}-{procedure}-{uuid.uuid4().hex[:8]}.json").write_text(
        json.dumps(
            {
                "argv": argv,
                "cwd": os.getcwd(),
                "env_keys": sorted(os.environ),
                "pid": os.getpid(),
                "procedure": procedure,
                "ticket": ticket,
                "interactive": True,
                "started": time.time(),
            }
        )
    )
    hook("UserPromptSubmit", prompt=prompt)
    t({"type": "user", "message": {"role": "user", "content": prompt}})
    if b.get("no_plugin"):
        t(
            {
                "type": "system",
                "subtype": "informational",
                "content": f"Unknown command: /delivery:{procedure}",
            }
        )
        return wait_for_input(hook, t, say)
    t(
        {
            "type": "user",
            "isMeta": True,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Base directory for this skill: {plugin_dir}/skills/{procedure}\n\n# {procedure}",
                    }
                ],
            },
        }
    )
    result_path = Path(re.search(r"JSON object to (\S+?);", prompt).group(1))  # type: ignore[union-attr]
    if b.get("usage_limit"):
        say(
            "Claude usage limit reached. Your limit resets at 5pm.",
            isApiErrorMessage=True,
            error="rate_limit",
        )
        hook("Stop", stop_hook_active=False)
        return wait_for_input(hook, t, say)
    artifacts = write_outputs(b, procedure, ticket, envelope)
    result = build_result(b, procedure, ticket, envelope, plugin_dir, artifacts)
    if b.get("deny"):
        t(
            {
                "type": "assistant",
                "message": {
                    "id": "d1",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_d1",
                            "name": "Bash",
                            "input": {"command": "gh auth status"},
                        }
                    ],
                },
            }
        )
        t(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_d1",
                            "is_error": True,
                            "content": "Permission to use Bash with command gh auth status has been denied.",
                        }
                    ]
                },
            }
        )
    forget = int(b.get("forget_result", 0))  # stops before writing the result this many times
    if not forget:
        result_path.write_text(json.dumps(result))
    say(f"{procedure} done")
    reply = hook("Stop", stop_hook_active=False)
    while reply and reply.get("decision") == "block":
        t({"type": "user", "isMeta": True, "message": {"content": f"Stop hook feedback:\n{reply['reason']}"}})
        forget -= 1
        if forget <= 0 and not b.get("never_result"):
            result_path.write_text(json.dumps(result))
        say("Wrote the result.")
        reply = hook("Stop", stop_hook_active=True)
    return wait_for_input(hook, t, say)


def wait_for_input(hook, t, say) -> int:  # type: ignore[no-untyped-def]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "/exit":
            hook("SessionEnd", reason="prompt_input_exit")
            return 0
        hook("UserPromptSubmit", prompt=line)
        t({"type": "user", "message": {"role": "user", "content": line}})
        if line.startswith("EDIT "):
            _, rel, text = line.split(" ", 2)
            target = Path(os.getcwd()) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text + "\n")
            say(f"Changed {rel}.")
        else:
            say(f"You said: {line}")
        hook("Stop", stop_hook_active=False)
    hook("SessionEnd", reason="other")
    return 0


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
    if "-p" not in argv:
        return interactive(argv, scenario_path)
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
    (log_dir / f"{time.time_ns()}-{ticket}-{procedure}-{uuid.uuid4().hex[:8]}.json").write_text(
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

    artifacts = write_outputs(b, procedure, ticket, envelope)
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

    result = build_result(b, procedure, ticket, envelope, plugin_dir, artifacts)
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
