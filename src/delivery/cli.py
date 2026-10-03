"""``delivery`` command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from delivery import __version__
from delivery.config import Config, ConfigError, load_config, template_text
from delivery.control import send, socket_path
from delivery.journal import JournalStore
from delivery.models import ACTIVE_RUN_STATES, RunState
from delivery.workflow import Stage

EXIT_OK, EXIT_FAIL, EXIT_CONFIG, EXIT_BUSY = 0, 1, 2, 3


def _print(data: Any, as_json: bool, text: str) -> None:
    print(json.dumps(data, indent=2, default=str) if as_json else text)


def _load(args: argparse.Namespace) -> Config:
    return load_config(Path(args.config))


def _git_identity(checkout: Path) -> tuple[str, str]:
    def get(key: str) -> str:
        if not (checkout / ".git").exists():
            return ""
        return subprocess.run(
            ["git", "-C", str(checkout), "config", key], capture_output=True, text=True, check=False
        ).stdout.strip()

    return get("user.name") or "delivery coordinator", get("user.email") or "delivery-coordinator@localhost"


def _claude_runner(cfg: Config) -> Any:
    """Print mode (``claude -p``), or interactive sessions in tmux when configured."""
    from delivery.claude import ClaudeRunner
    from delivery.interactive import InteractiveRunner

    if cfg.claude.interactive.enabled:
        return InteractiveRunner(
            cfg.claude.executable,
            cfg.claude.interactive,
            cfg.runtime.state_dir,
            worktree_root=cfg.repository.worktree_root,
        )
    return ClaudeRunner(cfg.claude.executable)


def build_deps(cfg: Config) -> Any:
    from delivery.git import ManagedRepo
    from delivery.github import GhClient
    from delivery.jira import JiraClient
    from delivery.ownership import RepoLocks
    from delivery.plugin import load_plugin
    from delivery.runtime import Deps

    locks = RepoLocks(cfg.runtime.state_dir / "locks")
    name, email = _git_identity(cfg.repository.checkout_path)
    repo = ManagedRepo(
        cfg.repository.url,
        cfg.repository.base_branch,
        cfg.repository.worktree_root,
        locks,
        author_name=name,
        author_email=email,
        reference=cfg.repository.checkout_path,
    )
    from delivery.credentials import resolve_figma
    from delivery.figma import FigmaClient

    figma_token = resolve_figma(cfg) if cfg.figma.enabled else None
    return Deps(
        cfg,
        JiraClient(cfg),
        GhClient(cfg.repository.slug),
        repo,
        _claude_runner(cfg),
        JournalStore(cfg.runtime.state_dir, cfg.identity_key),
        load_plugin(cfg.claude.plugin_path),
        locks,
        figma=FigmaClient(figma_token) if figma_token else None,
    )


# --------------------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser()
    if path.exists() and not args.force:
        print(f"{path} already exists; refusing to overwrite (use --force)", file=sys.stderr)
        return EXIT_CONFIG
    from delivery.setup import bundled_plugin_path

    text = template_text()
    plugin = bundled_plugin_path()
    if plugin:
        text = text.replace("/absolute/path/to/delivery-platform/plugins/delivery", str(plugin))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    print(
        f"Wrote {path}. Fill in the values, then run `delivery workflow inspect --config {path}` and "
        f"`delivery doctor --config {path}`."
    )
    return EXIT_OK


def cmd_setup(args: argparse.Namespace) -> int:
    from delivery.setup import SetupDeps, TerminalPrompter, run_setup, tilde

    if not sys.stdin.isatty():
        print("`delivery setup` asks questions: run it in a terminal.", file=sys.stderr)
        return EXIT_CONFIG
    result = run_setup(Path(args.config), SetupDeps(TerminalPrompter()))
    if result is None:
        return EXIT_FAIL
    print("\nChecking everything with `delivery doctor`...\n")
    doctor = argparse.Namespace(config=str(result.path), claude_probe=False, json=False)
    ready = asyncio.run(_doctor(doctor)) == EXIT_OK
    default = Path(os.environ.get("DELIVERY_CONFIG") or Path.home() / "delivery.local.toml").expanduser()
    flag = "" if result.path == default.absolute() else f" --config {tilde(result.path)}"
    steps = [f"  - {n}" for n in result.notes]
    if not ready:
        steps.append(f"  - Fix the FAIL items above, then check again: delivery doctor{flag}")
    steps += [
        f"  - Once, prove Claude's sandbox on this laptop (uses a little of your plan): "
        f"delivery doctor --claude-probe{flag}",
        f"  - Start the coordinator: coordinator{flag}",
        f"  - Change an answer later: delivery setup{flag}",
    ]
    print("\nNext\n" + "\n".join(steps))
    return EXIT_OK


async def _doctor(args: argparse.Namespace) -> int:
    from delivery.doctor import claude_probe, render, run_doctor
    from delivery.github import GhClient
    from delivery.jira import JiraClient, JiraCredentialsMissing

    cfg = _load(args)
    problem = ""
    try:
        jira: JiraClient | None = JiraClient(cfg)
    except JiraCredentialsMissing as exc:
        jira, problem = None, str(exc)
    gh = GhClient(cfg.repository.slug) if shutil.which("gh") else None
    try:
        report = await run_doctor(cfg, jira, gh, problem)
        if args.claude_probe:
            await claude_probe(cfg, report)
    finally:
        if jira:
            await jira.close()
    _print(report.as_json(), args.json, render(report))
    return EXIT_OK if report.ready else EXIT_FAIL


async def _workflow_inspect(args: argparse.Namespace) -> int:
    from delivery.doctor import inspect_workflow
    from delivery.jira import JiraClient

    cfg = _load(args)
    jira = JiraClient(cfg)
    try:
        wf = await inspect_workflow(cfg, jira)
    finally:
        await jira.close()
    if args.json:
        _print(wf.__dict__, True, "")
        return EXIT_OK if not (wf.missing or wf.ambiguous or wf.problems) else EXIT_FAIL
    lines = [f"Project {cfg.jira.project_key}: {len(wf.statuses)} statuses"]
    if wf.missing:
        lines.append(f"MISSING statuses: {', '.join(wf.missing)}")
    for k, ids in wf.ambiguous.items():
        lines.append(f"AMBIGUOUS {k}: several statuses share the name ({ids}); rename them")
    for k, v in wf.mismatched_config.items():
        lines.append(f"CONFIG MISMATCH {k}: config {v['config']} vs Jira {v['jira']}")
    lines += [f"WARN {w}" for w in wf.category_warnings]
    for st, info in wf.transitions_checked.items():
        state = "ok" if not (info["missing"] or info["unexpected"]) else "PROBLEM"
        lines.append(f"transitions from {st} (sampled {info['sample']}): {state}")
        lines += [f"  missing: {m}" for m in info["missing"]]
        lines += [f"  unexpected: {u}" for u in info["unexpected"]]
    lines.append(f"Delivery resume stage field: {wf.resume_field or 'NOT FOUND'}")
    lines += ["", "Paste into your config:", "", wf.toml()]
    print("\n".join(lines))
    return EXIT_OK if not (wf.missing or wf.ambiguous or wf.problems) else EXIT_FAIL


async def _run(args: argparse.Namespace) -> int:
    from delivery.ownership import LockHeld
    from delivery.supervisor import Supervisor

    cfg = _load(args)
    missing = cfg.workflow.missing_statuses()
    if missing:
        print(
            f"{len(missing)} workflow statuses are unmapped; run `delivery workflow inspect` first.",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    deps = build_deps(cfg)
    sup = Supervisor(deps, dry_run=args.dry_run, emit=lambda s: print(s, flush=True))
    try:
        await sup.__aenter__()
    except LockHeld as exc:
        print(
            f"Another supervisor already runs for this identity: {exc.holder}. "
            "Use `delivery status`; never start a second one.",
            file=sys.stderr,
        )
        return EXIT_BUSY
    sup.install_signal_handlers()
    try:
        if args.dry_run:
            report = await sup.poll_once()
            data = {
                "would_start": report.started,
                "waiting": report.waiting,
                "skipped": report.skipped,
                "discovered": report.discovered,
                "error": report.error,
            }
            text = "\n".join(
                [
                    f"[dry-run] discovered: {[d['ticket'] for d in report.discovered]}",
                    *(f"[dry-run] would start {t}" for t in report.started),
                    *(f"[dry-run] waiting {w['ticket']}: {w['reason']}" for w in report.waiting),
                    *(f"[dry-run] skipped {s['ticket']}: {s['reason']}" for s in report.skipped),
                    "[dry-run] no Claude call, comment, push or transition was made.",
                ]
            )
            _print(data, args.json, text)
            return EXIT_OK
        from delivery import console

        print(console.supervisor_started(cfg, __version__), flush=True)
        await sup.run(once=args.once)
    finally:
        await sup.__aexit__(None, None, None)
    return EXIT_OK


async def _control(cfg: Config, request: dict[str, Any]) -> dict[str, Any] | None:
    store = JournalStore(cfg.runtime.state_dir, cfg.identity_key)
    rec = store.load_supervisor()
    path = (
        Path(rec.control_socket)
        if rec and rec.control_socket
        else socket_path(cfg.runtime.state_dir, cfg.identity_key)
    )
    if not path.exists():
        return None
    try:
        return await send(path, request)
    except (ConnectionError, FileNotFoundError, TimeoutError):
        return None


def _local_rows(cfg: Config) -> list[dict[str, Any]]:
    store = JournalStore(cfg.runtime.state_dir, cfg.identity_key)
    rows: list[dict[str, Any]] = []
    for e in store.iter_runs():
        r = e.record
        if e.error:
            rows.append(
                {
                    "ticket": e.ticket_key,
                    "run_id": e.run_id,
                    "state": "CORRUPT",
                    "next_action": f"{e.error.detail}; inspect before recovery",
                }
            )
            continue
        assert r is not None
        if r.state in ACTIVE_RUN_STATES or r.state in (
            RunState.INTERRUPTED,
            RunState.BLOCKED,
            RunState.AWAITING_HUMAN,
            RunState.FAILED,
        ):
            latest = store.latest_run(r.ticket_key)
            if latest and latest.run_id != e.run_id:
                continue
            rows.append(
                {
                    "ticket": r.ticket_key,
                    "stage": r.stage.value,
                    "run_id": r.run_id,
                    "session": r.session_label,
                    "worker": r.worker_id,
                    "state": r.state.value,
                    "held": r.held,
                    "started_at": r.started_at,
                    "next_action": r.next_action or r.reason,
                    "pending_ops": len(e.journal.pending_ops()),
                }
            )
    return sorted(rows, key=lambda x: str(x["ticket"]))


async def _status(args: argparse.Namespace) -> int:
    cfg = _load(args)
    live = await _control(cfg, {"cmd": "status"})
    rows = _local_rows(cfg)
    data = {"supervisor_running": live is not None, "live": live, "runs": rows}
    lines = [
        f"Supervisor: {'running' if live else 'not running'}"
        + (f", dispatch {'PAUSED' if live['dispatch_paused'] else 'active'}" if live else "")
    ]
    if live:
        lines.append(f"Active sessions ({len(live['sessions'])}):")
        lines += [
            f"  {s['ticket']:<12} {s['stage']:<20} {s['state']:<10} run {s['run_id']} "
            f"started {s['started_at']} pid {s['child_pid']}"
            for s in live["sessions"]
        ]
        if live.get("open_sessions"):
            lines.append(f"Claude sessions open for questions ({len(live['open_sessions'])}):")
            lines += [
                f"  {o['ticket']:<12} {o['procedure']:<20} delivery attach {o['ticket']}"
                + (f"  [waiting: {o['held']}]" if o.get("held") else "")
                for o in live["open_sessions"]
            ]
    lines.append("Latest run per ticket:")
    lines += [
        f"  {r['ticket']:<12} {r.get('stage', '-'):<20} {r['state']:<15} {r.get('next_action', '')}"
        + (f" [pending ops: {r['pending_ops']}]" if r.get("pending_ops") else "")
        for r in rows
    ]
    _print(data, args.json, "\n".join(lines))
    return EXIT_OK


async def _inspect(args: argparse.Namespace) -> int:
    from delivery.explain import jira_actions, latest_outcome
    from delivery.intake import IntakeEvaluator, load_context
    from delivery.jira import JiraClient
    from delivery.ownership import evaluate_eligibility

    cfg = _load(args)
    jira = JiraClient(cfg)
    try:
        ctx = await load_context(jira, cfg, args.ticket)
        elig = evaluate_eligibility(ctx.issue.view, cfg)
        intake = None
        if elig.stage:
            from delivery.github import GhClient

            intake = await IntakeEvaluator(cfg, GhClient(cfg.repository.slug)).evaluate(ctx, elig.stage)
        transitions = [(t.name, t.to_status_name) for t in await jira.transitions(args.ticket)]
    finally:
        await jira.close()
    store = JournalStore(cfg.runtime.state_dir, cfg.identity_key)
    entries = store.runs_for_ticket(args.ticket)
    runs = [
        {
            "run_id": e.run_id,
            "state": e.record.state.value if e.record else "CORRUPT",
            "stage": e.record.stage.value if e.record else None,
            "reason": e.record.reason if e.record else (e.error.detail if e.error else ""),
            "pending_ops": [o.op_type for o in e.journal.pending_ops()] if e.record else [],
            "dir": str(e.journal.dir),
        }
        for e in entries
    ]
    rec = ctx.record
    data = {
        "ticket": args.ticket,
        "status": ctx.status.value if ctx.status else ctx.issue.view.status_name,
        "assignee": ctx.issue.view.assignee_account_id,
        "eligible": elig.eligible,
        "eligibility_reasons": list(elig.reasons),
        "intake": intake.persisted() if intake else None,
        "gates": [g.model_dump(mode="json") for g in rec.gates],
        "pause": rec.pause.model_dump(mode="json") if rec.pause else None,
        "candidate": rec.candidate_sha,
        "pr": rec.pr_number,
        "artefacts": rec.artefacts,
        "footprint": rec.footprint_ref,
        "overlap_warnings": rec.overlap_warnings,
        "overlap_decisions": rec.overlap_decisions,
        "release": rec.release,
        "pending_feedback": rec.pending_feedback,
        "jira_actions": [{"name": n, "to": t} for n, t in transitions],
        "local_runs": runs,
    }
    lines = [
        f"{args.ticket}: {data['status']} (assignee {data['assignee']})",
        f"eligible: {elig.eligible}" + (f" ({'; '.join(elig.reasons)})" if elig.reasons else ""),
    ]
    if intake:
        lines.append(
            f"intake: {intake.kind.value} - {intake.reason}"
            + (f" -> {intake.next_action}" if intake.next_action else "")
        )
    lines.append(
        "gates: " + ", ".join(f"{g.token}={g.state.value}" for g in rec.gates) if rec.gates else "gates: none"
    )
    if rec.pause:
        lines.append(
            f"paused: {rec.pause.kind} resume={rec.pause.resume_stage.value} "
            f"{rec.pause.round_token or rec.pause.reason}"
        )
    lines.append(f"candidate: {rec.candidate_sha or '-'} PR #{rec.pr_number or '-'}")
    lines.append(f"overlap warnings: {', '.join(rec.overlap_warnings) or 'none'}")
    if entries:
        lines += ["", *latest_outcome(cfg, entries[-1])]
    actions = jira_actions(cfg, transitions)
    if actions:
        lines += ["", *actions]
    if runs:
        lines += ["", "Runs on this machine (oldest first):"]
        lines += [
            f"  {r['run_id']}  {r['state']}" + (f"  pending {r['pending_ops']}" if r["pending_ops"] else "")
            for r in runs
        ]
    _print(data, args.json, "\n".join(lines))
    return EXIT_OK


async def _ticket_command(args: argparse.Namespace, cmd: str) -> int:
    cfg = _load(args)
    request = {"cmd": cmd, "ticket": args.ticket, "resume": getattr(args, "resume", False)}
    live = await _control(cfg, request)
    if live is not None:
        _print(live, args.json, json.dumps(live, indent=2, default=str))
        return EXIT_OK if live.get("ok") else EXIT_FAIL
    if cmd == "stop":
        print("No supervisor is running; nothing to stop.")
        return EXIT_OK
    # No supervisor: act directly while holding the identity lock.
    from delivery.ownership import LockHeld
    from delivery.supervisor import Supervisor

    sup = Supervisor(build_deps(cfg), emit=print)
    try:
        await sup.__aenter__()
    except LockHeld as exc:
        print(f"supervisor lock held but socket unreachable: {exc.holder}", file=sys.stderr)
        return EXIT_BUSY
    try:
        if cmd == "recover":
            await sup.deps.repo.ensure()
            result = await sup.recover(args.ticket, args.resume)
            if sup.sessions:
                await asyncio.wait([s.task for s in sup.sessions.values()])
        else:
            result = await sup.handover(args.ticket)
    finally:
        await sup.__aexit__(None, None, None)
    _print(result, args.json, json.dumps(result, indent=2, default=str))
    return EXIT_OK if result.get("ok") else EXIT_FAIL


async def _dispatch(args: argparse.Namespace) -> int:
    cfg = _load(args)
    live = await _control(cfg, {"cmd": args.action, "reason": args.reason or ""})
    if live is None:
        store = JournalStore(cfg.runtime.state_dir, cfg.identity_key)
        store.init()
        from delivery.journal import SupervisorRecord

        rec = store.load_supervisor() or SupervisorRecord(
            identity_key=cfg.identity_key,
            worker_id=cfg.identity.worker_id,
            developer_account_id=cfg.identity.developer_jira_account_id,
        )
        rec = rec.model_copy(
            update={"dispatch_paused": args.action == "pause", "pause_reason": args.reason or ""}
        )
        store.save_supervisor(rec, f"dispatch_{args.action}")
        live = {
            "ok": True,
            "dispatch_paused": rec.dispatch_paused,
            "note": "applies when the supervisor starts",
        }
    _print(live, args.json, f"dispatch {'paused' if live.get('dispatch_paused') else 'active'}")
    return EXIT_OK


async def _workflow_verify(args: argparse.Namespace) -> int:
    from delivery.jira import JiraClient
    from delivery.workflow_check import CHECK_LABEL, render, verify_workflow

    cfg = _load(args)
    missing = cfg.workflow.missing_statuses()
    if missing:
        print(f"Map every status first ({len(missing)} missing); run `delivery workflow inspect`.")
        return EXIT_CONFIG
    if not args.yes:
        print(
            f"This creates two unassigned test tickets in {cfg.jira.project_key} labelled "
            f"{CHECK_LABEL!r}, walks them through every status, and leaves them in Done and "
            "Cancelled for you to delete. Re-run with --yes to proceed."
        )
        return EXIT_FAIL
    jira = JiraClient(cfg)
    try:
        report = await verify_workflow(cfg, jira, emit=(lambda _: None) if args.json else print)
    finally:
        await jira.close()
    _print(report.as_json(), args.json, render(report))
    return EXIT_OK if report.ok else EXIT_FAIL


def _keychain_target(cfg: Config) -> tuple[str, str] | None:
    service, email = cfg.jira.token_keychain_service, cfg.jira.email
    if not service or not email:
        print(
            "Set both under [jira] in the config first, for example:\n"
            '  email = "you@example.com"\n  token_keychain_service = "delivery-jira"',
            file=sys.stderr,
        )
        return None
    return service, email


async def _jira_whoami(cfg: Config, credentials: Any = None) -> str | None:
    """Return the authenticated display name, or print why Jira refused and return None."""
    from delivery.jira import JiraClient
    from delivery.ports import AuthError, IntegrationError

    jira = JiraClient(cfg, credentials=credentials)
    try:
        me = await jira.myself()
    except AuthError as exc:
        print(
            f"Jira refused the token ({exc.status}). Check it was copied in full and has not "
            "been revoked, then run `delivery credentials set` again.",
            file=sys.stderr,
        )
        return None
    except IntegrationError as exc:
        print(f"Could not reach Jira: {exc}", file=sys.stderr)
        return None
    finally:
        await jira.close()
    return f"{me.display_name} ({me.account_id}) using the token from {jira.credential_source}"


async def _figma_whoami(token: str) -> str | None:
    from delivery.figma import FigmaClient
    from delivery.ports import AuthError, IntegrationError

    client = FigmaClient(token)
    try:
        me = await client.me()
    except AuthError as exc:
        print(
            f"Figma refused the token ({exc.status}). It needs the scopes File content: read-only "
            "(file_content:read) and Current user: read (current_user:read), and must not have expired.",
            file=sys.stderr,
        )
        return None
    except IntegrationError as exc:
        print(f"Could not reach Figma: {exc}", file=sys.stderr)
        return None
    finally:
        await client.close()
    return f"{me.get('handle', '?')} ({me.get('email', '?')})"


def cmd_credentials(args: argparse.Namespace) -> int:
    import getpass

    from delivery.credentials import (
        FIGMA_TOKEN_SHAPE,
        SECURITY,
        TOKEN_SHAPE,
        JiraCredentials,
        keychain_available,
        keychain_write,
        resolve_figma,
    )

    cfg = _load(args)
    figma = args.target == "figma"
    if args.action == "check":
        if figma:
            token = resolve_figma(cfg)
            if not token:
                print("No Figma token is stored. Run `delivery credentials set figma`.")
                return EXIT_FAIL
            who = asyncio.run(_figma_whoami(token))
            print(f"Figma accepts the stored token: {who}." if who else "Not authenticated.")
        else:
            who = asyncio.run(_jira_whoami(cfg))
            print(f"Jira accepts the stored token: authenticated as {who}." if who else "Not authenticated.")
        return EXIT_OK if who else EXIT_FAIL
    if not keychain_available():
        print("The macOS Keychain is not available here; export the environment variables instead.")
        return EXIT_CONFIG
    if figma:
        service, account = cfg.figma.token_keychain_service, cfg.figma.token_account
    else:
        target = _keychain_target(cfg)
        if target is None:
            return EXIT_CONFIG
        service, account = target
    if args.action == "delete":
        r = subprocess.run(
            [SECURITY, "delete-generic-password", "-s", service, "-a", account],
            check=False,
            capture_output=True,
        )
        print("Removed the Keychain item." if r.returncode == 0 else "No Keychain item to remove.")
        return EXIT_OK
    label = "Figma personal access token" if figma else f"Jira API token for {account}"
    token = getpass.getpass(f"Paste the {label} (nothing is shown), then Enter: ").strip()
    shape = FIGMA_TOKEN_SHAPE if figma else TOKEN_SHAPE
    if not shape.match(token):
        print(
            f"That does not look like a token ({len(token)} characters); nothing was stored.",
            file=sys.stderr,
        )
        return EXIT_FAIL
    if figma:
        who = asyncio.run(_figma_whoami(token))
    else:
        who = asyncio.run(_jira_whoami(cfg, JiraCredentials(account, token, "the value just pasted")))
    if not who:
        print("Nothing was stored.", file=sys.stderr)
        return EXIT_FAIL
    try:
        keychain_write(service, account, token, shape)
    except (OSError, ValueError) as exc:
        print(f"The token was accepted but storing it failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    print(f"{'Figma' if figma else 'Jira'} accepted the token: authenticated as {who.split(' using ')[0]}.")
    print(f"Stored in your login Keychain (service {service!r}, account {account}); read back intact.")
    return EXIT_OK


def cmd_logs(args: argparse.Namespace) -> int:
    """Readable Claude session transcripts for a ticket's runs."""
    from delivery.models import ACTIVE_RUN_STATES
    from delivery.transcript import follow, render_file

    cfg = _load(args)
    store = JournalStore(cfg.runtime.state_dir, cfg.identity_key)
    runs = store.runs_for_ticket(args.ticket)
    if not runs:
        print(f"No runs recorded on this machine for {args.ticket}.")
        return EXIT_FAIL
    if args.list:
        for e in runs:
            r = e.record
            when = r.created_at.astimezone().strftime("%d %b %H:%M") if r else "?"
            state = f"{r.stage.value:<21} {r.state.value:<15}" if r else "CORRUPT"
            print(f"{when}  {state} {e.run_id}")
        return EXIT_OK
    if args.run:
        chosen = [e for e in runs if e.run_id == args.run]
    elif args.stage:
        chosen = [e for e in runs if e.record and e.record.stage.value == args.stage][-1:]
    else:
        chosen = runs[-1:]
    if not chosen:
        print("No matching run. Use --list to see them.", file=sys.stderr)
        return EXIT_FAIL
    entry = chosen[0]
    rec = entry.record
    logs = sorted((entry.journal.dir / "logs").glob("claude-*.jsonl"), key=lambda p: p.stat().st_mtime)
    print(f"{args.ticket}  run {entry.run_id}")
    if rec:
        print(f"Stage {rec.stage.value}, state {rec.state.value}" + (f": {rec.reason}" if rec.reason else ""))
        if rec.next_action:
            print(f"Next: {rec.next_action}")
    print(f"Folder: {entry.journal.dir}")
    if not logs:
        print("No Claude session has started for this run yet.")
        return EXIT_OK
    if args.raw:
        for p in logs:
            print(p)
        return EXIT_OK
    for i, p in enumerate(logs):
        print("\n" + "=" * 78 + f"\n {p.stem.removeprefix('claude-')}\n" + "=" * 78)
        last = i == len(logs) - 1
        if args.follow and last:

            def running(run_id: str = entry.run_id) -> bool:
                for e in store.iter_runs(args.ticket):
                    if e.run_id == run_id:
                        return bool(e.record and e.record.state in ACTIVE_RUN_STATES)
                return False

            try:
                follow(p, lambda line: print(line, flush=True), args.verbose_results, is_running=running)
            except KeyboardInterrupt:
                return EXIT_OK
        else:
            for line in render_file(p, args.verbose_results):
                print(line)
    return EXIT_OK


def _tmux(cfg: Config) -> Any:
    from delivery.tmux import for_config

    return for_config(cfg.claude.interactive, cfg.runtime.state_dir)


def _ticket_sessions(cfg: Config, ticket: str, procedure: str | None) -> list[str]:
    """The ticket's tmux sessions: Claude's, or with ``procedure`` "preview" its running app."""
    from delivery.preview import PROCEDURE as PREVIEW
    from delivery.tmux import session_name

    names = asyncio.run(_tmux(cfg).sessions())
    prefix = session_name(ticket, procedure) if procedure else session_name(ticket, "")
    found = [n for n in names if n.startswith(prefix)]
    return found if procedure == PREVIEW else [n for n in found if n != session_name(ticket, PREVIEW)]


def cmd_attach(args: argparse.Namespace) -> int:
    """Open a ticket's Claude session in this terminal (Ctrl-b d leaves it running)."""
    from delivery.open_sessions import SessionRegistry

    cfg = _load(args)
    tmux = _tmux(cfg)
    if not tmux.available():
        print(f"{cfg.claude.interactive.tmux!r} is not installed (brew install tmux).", file=sys.stderr)
        return EXIT_FAIL
    found = _ticket_sessions(cfg, args.ticket, args.procedure)
    if not found:
        others = asyncio.run(tmux.sessions())
        print(f"No Claude session is running for {args.ticket}.", file=sys.stderr)
        if others:
            print("Running: " + ", ".join(others), file=sys.stderr)
        return EXIT_FAIL
    open_names = {r.name for r in SessionRegistry(cfg.runtime.state_dir).all()}
    # Prefer the session that is working now over ones left open for questions.
    found.sort(key=lambda n: n in open_names)
    if len(found) > 1:
        print(f"{args.ticket} has {len(found)} sessions ({', '.join(found)}); opening {found[0]}.")
        print("Use --procedure to choose another.")
    argv = tmux.attach_argv(found[0])
    os.execvp(argv[0], argv)  # noqa: S606 (argument array, no shell: become the tmux client)
    return EXIT_OK  # not reached


def cmd_sessions(args: argparse.Namespace) -> int:
    """Claude sessions in tmux: working now, or left open for questions; and running apps."""
    from delivery.open_sessions import SessionRegistry

    cfg = _load(args)
    names = asyncio.run(_tmux(cfg).sessions())
    registry = SessionRegistry(cfg.runtime.state_dir).all()
    kept = {r.name: r for r in registry}
    apps = {r.preview.name: (r.ticket_key, r.preview) for r in registry if r.preview}
    rows = []
    for n in names:
        if n in apps:
            ticket, app = apps[n]
            rows.append(
                {
                    "tmux_session": n,
                    "state": f"app {app.state} at {app.url}",
                    "ticket": ticket,
                    "procedure": "preview",
                    "since": app.started_at.isoformat(),
                    "follow_ups": [],
                    "waiting": "",
                }
            )
            continue
        r = kept.get(n)
        follow_ups = [
            f"c{f['candidate']}" if "candidate" in f else f"v{f['revision']:03d}"
            for f in (r.followups if r else [])
        ]
        rows.append(
            {
                "tmux_session": n,
                "state": "open for questions" if r else "working",
                "ticket": r.ticket_key if r else n.rsplit("-", 2)[0],
                "procedure": r.procedure if r else "",
                "since": r.opened_at.isoformat() if r else "",
                "follow_ups": follow_ups,
                "waiting": r.held if r else "",
            }
        )
    if not rows:
        _print(rows, args.json, "No Claude sessions are running.")
        return EXIT_OK
    lines = [
        f"  {row['tmux_session']:<36} {row['state']:<20}"
        + (f" follow-ups {', '.join(row['follow_ups'])}" if row["follow_ups"] else "")
        + (f" [waiting: {row['waiting']}]" if row["waiting"] else "")
        for row in rows
    ]
    _print(rows, args.json, "\n".join(["Claude sessions (delivery attach <ticket>):", *lines]))
    return EXIT_OK


def cmd_preview(args: argparse.Namespace) -> int:
    """Open the app running from a ticket's development session, or ask for it to start again."""
    from delivery.open_sessions import SessionRegistry
    from delivery.preview import RESTART, answers, open_url
    from delivery.workflow import Stage

    cfg = _load(args)
    if not cfg.preview.enabled:
        print("No app preview is configured: add [preview] command = [...] to your config.", file=sys.stderr)
        return EXIT_FAIL
    found = [
        r
        for r in SessionRegistry(cfg.runtime.state_dir).for_ticket(args.ticket)
        if r.stage is Stage.DEVELOPMENT
    ]
    if not found:
        print(
            f"{args.ticket} has no development session open; the app runs from one while it is open.",
            file=sys.stderr,
        )
        return EXIT_FAIL
    rec = found[-1]
    p = rec.preview
    if p and p.state == "ready" and answers(p.url):
        problem = asyncio.run(open_url(p.url))
        print(
            f"{args.ticket}: the app is running at {p.url}"
            + (f" (could not open it: {problem})" if problem else "")
        )
        return EXIT_OK
    if p and p.state == "starting" and asyncio.run(_tmux(cfg).alive(p.name)):
        print(
            f"{args.ticket}: the app is still starting at {p.url}; your browser opens when it answers. "
            f"Its output: delivery attach {args.ticket} --procedure preview"
        )
        return EXIT_OK
    (Path(rec.session_dir) / RESTART).touch()
    print(
        f"{args.ticket}: asked the coordinator to start the app again; your browser opens when it "
        f"answers. Its output: delivery attach {args.ticket} --procedure preview"
    )
    return EXIT_OK


def cmd_close(args: argparse.Namespace) -> int:
    """End a ticket's open Claude sessions (as /exit would); the coordinator then tidies up."""
    from delivery.open_sessions import SessionRegistry

    cfg = _load(args)
    kept = {r.name for r in SessionRegistry(cfg.runtime.state_dir).all()}
    found = [n for n in _ticket_sessions(cfg, args.ticket, args.procedure) if n in kept]
    if not found:
        print(f"{args.ticket} has no Claude session open for questions.")
        return EXIT_FAIL
    for n in found:
        asyncio.run(_tmux(cfg).kill(n))
    print(
        f"Ended {', '.join(found)}. Within a few seconds the running coordinator publishes changes Claude "
        "finished making, keeps the conversation in the run's logs, saves anything else unpublished "
        "and removes the worktree. Verification of a development session's candidate starts then."
    )
    return EXIT_OK


def cmd_schemas(args: argparse.Namespace) -> int:
    from delivery.models import InputEnvelope, result_json_schema

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "stage-result.schema.json").write_text(json.dumps(result_json_schema(), indent=2) + "\n")
    (out / "input-envelope.schema.json").write_text(
        json.dumps(InputEnvelope.model_json_schema(), indent=2) + "\n"
    )
    print(f"wrote schemas to {out}")
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="delivery", description="Local Jira-driven AI SDLC supervisor.")
    p.add_argument("--version", action="version", version=f"delivery {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    default_config = os.environ.get("DELIVERY_CONFIG") or str(Path.home() / "delivery.local.toml")

    def with_config(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument(
            "--config",
            default=default_config,
            help="path to your local config file (default: $DELIVERY_CONFIG or ~/delivery.local.toml)",
        )
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    sp = sub.add_parser("setup", help="answer a few questions to write or update your config")
    sp.add_argument(
        "--config",
        default=default_config,
        help="config file to write (default: $DELIVERY_CONFIG or ~/delivery.local.toml)",
    )
    sp.set_defaults(func=cmd_setup)
    sp = sub.add_parser("init", help="write a commented config template (never overwrites)")
    sp.add_argument("--config", required=True)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)
    sp = with_config(sub.add_parser("doctor", help="read-only integration, workflow and safety checks"))
    sp.add_argument(
        "--claude-probe",
        action="store_true",
        help="also run a real Claude session to prove plugin loading and permission denials "
        "(uses a little subscription usage)",
    )
    sp.set_defaults(afunc=_doctor)
    wf = sub.add_parser("workflow", help="workflow mapping tools").add_subparsers(dest="wf", required=True)
    sp = with_config(wf.add_parser("inspect", help="resolve statuses/transitions and print a config block"))
    sp.set_defaults(afunc=_workflow_inspect)
    sp = with_config(
        wf.add_parser("verify", help="walk labelled test tickets through every status (writes to Jira)")
    )
    sp.add_argument("--yes", action="store_true", help="confirm creating and transitioning test tickets")
    sp.set_defaults(afunc=_workflow_verify)
    sp = with_config(
        sub.add_parser("credentials", help="store or check the Jira or Figma token (macOS Keychain)")
    )
    sp.add_argument("action", choices=["set", "check", "delete"])
    sp.add_argument("target", nargs="?", choices=["jira", "figma"], default="jira")
    sp.set_defaults(func=cmd_credentials)
    sp = with_config(sub.add_parser("run", help="run the supervisor in the foreground"))
    sp.add_argument("--once", action="store_true", help="one cycle: dispatch all eligible tickets and wait")
    sp.add_argument("--dry-run", action="store_true", help="discovery only: no Claude, writes or transitions")
    sp.set_defaults(afunc=_run)
    sp = with_config(sub.add_parser("status", help="sessions, states and next human actions"))
    sp.set_defaults(afunc=_status)
    for name, helptext in (
        ("inspect", "explain one ticket without changing anything"),
        ("recover", "reconcile one ticket; --resume continues held work"),
        ("handover", "stop and checkpoint one ticket for reassignment"),
        ("stop", "stop one ticket's session, leaving others running"),
    ):
        sp = with_config(sub.add_parser(name, help=helptext))
        sp.add_argument("ticket")
        if name == "recover":
            sp.add_argument("--resume", action="store_true")
        if name == "inspect":
            sp.set_defaults(afunc=_inspect)
        else:
            sp.set_defaults(afunc=lambda a, n=name: _ticket_command(a, n))
    sp = with_config(sub.add_parser("logs", help="readable Claude session transcripts for a ticket"))
    sp.add_argument("ticket")
    sp.add_argument("--list", action="store_true", help="list the ticket's runs")
    sp.add_argument("--stage", choices=[st.value for st in Stage], help="latest run of this stage")
    sp.add_argument("--run", help="a specific run ID (see --list)")
    sp.add_argument("--follow", "-f", action="store_true", help="keep printing as a running session works")
    sp.add_argument("--results", dest="verbose_results", action="store_true", help="also show tool output")
    sp.add_argument("--raw", action="store_true", help="print the raw log file paths (for jq)")
    sp.set_defaults(func=cmd_logs)
    sp = with_config(sub.add_parser("attach", help="open a ticket's Claude session in this terminal"))
    sp.add_argument("ticket")
    sp.add_argument(
        "--procedure",
        help="which session, if the ticket has several (e.g. implement-ticket); preview: the running app",
    )
    sp.set_defaults(func=cmd_attach)
    sp = with_config(
        sub.add_parser("sessions", help="Claude sessions in tmux: working or open for questions")
    )
    sp.set_defaults(func=cmd_sessions)
    sp = with_config(
        sub.add_parser("preview", help="open the app running from a ticket's development session")
    )
    sp.add_argument("ticket")
    sp.set_defaults(func=cmd_preview)
    sp = with_config(sub.add_parser("close", help="end a ticket's Claude session left open for questions"))
    sp.add_argument("ticket")
    sp.add_argument("--procedure")
    sp.set_defaults(func=cmd_close)
    sp = with_config(sub.add_parser("dispatch", help="pause or resume new launches"))
    sp.add_argument("action", choices=["pause", "resume"])
    sp.add_argument("--reason")
    sp.set_defaults(afunc=_dispatch)
    sp = sub.add_parser("schemas", help="export JSON schemas (development)")
    sp.add_argument("--out", default="plugins/delivery/references/schemas")
    sp.set_defaults(func=cmd_schemas)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if getattr(args, "func", None):
            return int(args.func(args))
        return int(asyncio.run(args.afunc(args)))
    except ConfigError as exc:
        print("Configuration problems:", file=sys.stderr)
        for p in exc.problems:
            print(f"  - {p}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:
        from delivery.jira import JiraCredentialsMissing

        if not isinstance(exc, JiraCredentialsMissing):
            raise
        print(f"Jira credentials: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return 130


def coordinator_args(argv: list[str], commands: set[str]) -> list[str]:
    """``coordinator`` alone starts the supervisor; ``coordinator <command>`` is ``delivery <command>``.

    Options without a command go to ``run`` (``coordinator --dry-run``), except ``-v`` which is
    the global verbose flag, and ``--help``/``--version`` which describe everything.
    """
    if any(a in commands for a in argv) or any(a in ("-h", "--help", "--version") for a in argv):
        return argv
    verbose = [a for a in argv if a in ("-v", "--verbose")]
    return [*verbose, "run", *[a for a in argv if a not in ("-v", "--verbose")]]


def coordinator_main(argv: list[str] | None = None) -> int:
    """Entry point of the ``coordinator`` command (see :func:`coordinator_args`)."""
    p = parser()
    sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    args = coordinator_args(sys.argv[1:] if argv is None else argv, set(sub.choices))
    return main(args)


if __name__ == "__main__":
    sys.exit(main())
