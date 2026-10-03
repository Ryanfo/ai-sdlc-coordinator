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
from delivery.config import Config, ConfigError, default_config_path, load_config, template_text
from delivery.control import send, socket_path
from delivery.journal import JournalStore
from delivery.models import ACTIVE_RUN_STATES, RunState
from delivery.workflow import Stage

EXIT_OK, EXIT_FAIL, EXIT_CONFIG, EXIT_BUSY = 0, 1, 2, 3
log = logging.getLogger("delivery")


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


def build_repo(cfg: Config) -> Any:
    from delivery.git import ManagedRepo
    from delivery.ownership import RepoLocks

    name, email = _git_identity(cfg.repository.checkout_path)
    return ManagedRepo(
        cfg.repository.url,
        cfg.repository.base_branch,
        cfg.repository.worktree_root,
        RepoLocks(cfg.runtime.state_dir / "locks"),
        author_name=name,
        author_email=email,
        reference=cfg.repository.checkout_path,
    )


def build_deps(cfg: Config) -> Any:
    from delivery.github import GhClient
    from delivery.jira import JiraClient
    from delivery.plugin import load_plugin
    from delivery.runtime import Deps

    repo = build_repo(cfg)
    locks = repo.locks
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
    from delivery.setup import PERSONAL_TEMPLATE, toml_value

    path = Path(args.config).expanduser()
    if path.exists() and not args.force:
        print(f"{path} already exists; refusing to overwrite (use --force)", file=sys.stderr)
        return EXIT_CONFIG
    if args.project:
        project = Path(args.project).expanduser().resolve()
        if not project.is_file():
            print(f"{project} not found", file=sys.stderr)
            return EXIT_CONFIG
        text = PERSONAL_TEMPLATE.format(project=toml_value(str(project)))
    else:
        text = template_text()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    print(
        f"Wrote {path}. `coordinator setup` fills it in step by step; or edit it yourself, then run "
        "`delivery workflow inspect` and `delivery doctor`."
    )
    return EXIT_OK


def cmd_setup(args: argparse.Namespace) -> int:
    from delivery.setup import SetupDeps, TerminalPrompter, run_setup, tilde

    if not sys.stdin.isatty():
        print("`delivery setup` asks questions: run it in a terminal.", file=sys.stderr)
        return EXIT_CONFIG
    project = Path(args.project).expanduser().absolute() if args.project else None
    if project and not project.is_file():
        print(f"{project} not found", file=sys.stderr)
        return EXIT_CONFIG
    result = run_setup(Path(args.config), SetupDeps(TerminalPrompter()), project)
    if result is None:
        return EXIT_FAIL
    print("\nChecking everything with `delivery doctor`...\n")
    doctor = argparse.Namespace(config=str(result.path), claude_probe=False, json=False)
    ready = asyncio.run(_doctor(doctor)) == EXIT_OK
    default = default_config_path().expanduser()
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


def _error(text: str) -> None:
    """A problem the person must see: on stderr and in the coordinator log."""
    print(text, file=sys.stderr)
    log.error(text, extra={"file_only": True})


async def _run(args: argparse.Namespace) -> int:
    from delivery import logfile
    from delivery.jira import JiraCredentialsMissing

    cfg = _load(args)
    path = logfile.log_path(cfg.runtime.state_dir, cfg.identity_key)
    handler = None if args.dry_run else logfile.attach(path, args.verbose)
    try:
        return await _supervise(args, cfg, path)
    except JiraCredentialsMissing as exc:
        log.error("Jira credentials: %s", exc, extra={"file_only": True})
        raise
    except Exception:
        log.exception("the coordinator stopped because of an internal error", extra={"file_only": True})
        raise
    finally:
        if handler is not None:
            logfile.detach(handler)


async def _supervise(args: argparse.Namespace, cfg: Config, log_file: Path) -> int:
    from delivery import logfile
    from delivery.ownership import LockHeld
    from delivery.supervisor import Supervisor

    missing = cfg.workflow.missing_statuses()
    if missing:
        _error(
            f"{len(missing)} workflow statuses are unmapped; "
            "run `coordinator setup` (or `delivery workflow inspect`)."
        )
        return EXIT_CONFIG
    deps = build_deps(cfg)
    emit = (lambda s: print(s, flush=True)) if args.dry_run else logfile.emitter()
    sup = Supervisor(deps, dry_run=args.dry_run, emit=emit)
    try:
        await sup.__aenter__()
    except LockHeld as exc:
        _error(
            f"Another coordinator already runs for this identity: {exc.holder}. "
            "`coordinator status` shows it; `coordinator stop` stops it. Never start a second one."
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

        background = os.environ.get("DELIVERY_BACKGROUND") == "1"
        emit(console.supervisor_started(cfg, __version__, log_file, background))
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
    from delivery import background

    cfg = _load(args)
    live = await _control(cfg, {"cmd": "status"})
    rows = _local_rows(cfg)
    st = background.state(cfg)
    changed = background.code_changed_since(cfg, st)
    waiting = (live or {}).get("claude_unavailable")
    data = {
        "supervisor_running": live is not None,
        "in_background": st.in_background,
        "code_changed_at": changed.isoformat() if changed else None,
        "live": live,
        "runs": rows,
    }
    lines = [
        f"Coordinator: {background.describe(cfg, st)}"
        + (f"; new work {'PAUSED' if live['dispatch_paused'] else 'active'}" if live else "")
    ]
    if changed:
        lines.append(
            f"  Its code changed at {changed:%H:%M} after it started: "
            "`coordinator restart` to use the new code."
        )
    if waiting:
        lines.append(
            f"  Waiting for Claude ({'login' if waiting.get('kind') == 'auth' else 'usage limit'}) since "
            f"{waiting.get('since', '?')[:16]}; next check {waiting.get('next_check', '?')[11:16]} UTC. "
            "New work waits; waiting runs continue by themselves."
        )
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
    from delivery.credentials import SECURITY, keychain_available, resolve_figma

    cfg = _load(args)
    figma = args.target == "figma"
    if args.action == "set":
        if not keychain_available():
            print("The macOS Keychain is not available here; export the environment variables instead.")
            return EXIT_CONFIG
        return EXIT_OK if store_token(cfg, figma) else EXIT_FAIL
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
    r = subprocess.run(
        [SECURITY, "delete-generic-password", "-s", service, "-a", account],
        check=False,
        capture_output=True,
    )
    print("Removed the Keychain item." if r.returncode == 0 else "No Keychain item to remove.")
    return EXIT_OK


def store_token(
    cfg: Config,
    figma: bool,
    secret: Any = None,
    out: Any = None,
) -> bool:
    """Ask for a token at a hidden prompt, check it with Jira or Figma, then keep it in the Keychain."""
    import getpass

    from delivery.credentials import FIGMA_TOKEN_SHAPE, TOKEN_SHAPE, JiraCredentials, keychain_write

    ask = secret or getpass.getpass
    say = out or (lambda text: print(text, file=sys.stderr))
    if figma:
        service, account = cfg.figma.token_keychain_service, cfg.figma.token_account
    else:
        target = _keychain_target(cfg)
        if target is None:
            return False
        service, account = target
    label = "Figma personal access token" if figma else f"Jira API token for {account}"
    token = ask(f"Paste the {label} (nothing is shown), then Enter: ").strip()
    shape = FIGMA_TOKEN_SHAPE if figma else TOKEN_SHAPE
    if not shape.match(token):
        say(f"That does not look like a token ({len(token)} characters); nothing was stored.")
        return False
    if figma:
        who = asyncio.run(_figma_whoami(token))
    else:
        who = asyncio.run(_jira_whoami(cfg, JiraCredentials(account, token, "the value just pasted")))
    if not who:
        say("Nothing was stored.")
        return False
    try:
        keychain_write(service, account, token, shape)
    except (OSError, ValueError) as exc:
        say(f"The token was accepted but storing it failed: {exc}")
        return False
    print(f"{'Figma' if figma else 'Jira'} accepted the token: authenticated as {who.split(' using ')[0]}.")
    print(f"Stored in your login Keychain (service {service!r}, account {account}); read back intact.")
    return True


def _coordinator_log(cfg: Config, args: argparse.Namespace) -> int:
    """The coordinator's own log: what its terminal showed, warnings and errors."""
    from delivery import logfile

    path = logfile.log_path(cfg.runtime.state_dir, cfg.identity_key)
    if args.raw:
        print(path)
        return EXIT_OK
    if not path.exists():
        print(f"No coordinator log yet ({path}); it starts with the coordinator.")
        return EXIT_OK if args.follow else EXIT_FAIL
    for line in logfile.tail(path, args.lines):
        print(line)
    if args.follow:
        try:
            for line in logfile.follow(path):
                print(line, flush=True)
        except KeyboardInterrupt:
            return EXIT_OK
    else:
        print(f"\n(last {args.lines} lines of {path}; --follow to keep watching)")
    return EXIT_OK


def cmd_logs(args: argparse.Namespace) -> int:
    """Readable Claude session transcripts for a ticket's runs, or the coordinator's own log."""
    from delivery.models import ACTIVE_RUN_STATES
    from delivery.transcript import follow, render_file

    cfg = _load(args)
    if not args.ticket:
        return _coordinator_log(cfg, args)
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
    """Open a ticket's Claude session, or the coordinator itself, in this terminal (Ctrl-b d
    leaves it running)."""
    from delivery import background
    from delivery.open_sessions import SessionRegistry

    cfg = _load(args)
    if not args.ticket:
        st = background.state(cfg)
        if not st.in_background:
            print(f"The coordinator is {background.describe(cfg, st)}.")
            print("`coordinator` starts it in the background and shows it here.")
            return EXIT_FAIL
        _warn_code_changed(cfg, st)
        background.exec_attach(background.attach_argv(cfg))
        return EXIT_OK  # not reached
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
    background.exec_attach(tmux.attach_argv(found[0]))
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


# --------------------------------------------------------------------------- the coordinator itself


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _warn_code_changed(cfg: Config, st: Any) -> None:
    from delivery import background

    changed = background.code_changed_since(cfg, st)
    if changed:
        print(
            f"Note: the coordinator's code changed at {changed:%H:%M}, after this coordinator started. "
            "`coordinator restart` uses the new code (running sessions are saved and continue)."
        )


def _foreground(args: argparse.Namespace) -> int:
    run_args = argparse.Namespace(**{**vars(args), "dry_run": False, "once": False, "json": False})
    return int(asyncio.run(_run(run_args)))


def _start(args: argparse.Namespace, attach: bool) -> int:
    from delivery import background

    cfg = _load(args)
    if cfg.workflow.missing_statuses():
        print("The Jira workflow is not mapped yet: run `coordinator setup` first.", file=sys.stderr)
        return EXIT_CONFIG
    if not background.server(cfg).available():
        print(
            f"{cfg.claude.interactive.tmux} is not installed, so the coordinator runs in this terminal and "
            "stops when it closes (`brew install tmux` lets it run in the background)."
        )
        return _foreground(args)
    st = background.state(cfg)
    if st.running and not st.in_background:
        print(f"The coordinator is {background.describe(cfg, st)}.")
        print("Use that terminal, or `coordinator stop` and then `coordinator` to run it in the background.")
        return EXIT_OK
    if st.running:
        _warn_code_changed(cfg, st)
    else:
        if st.stopped_unexpectedly:
            print(
                "Last time the coordinator stopped unexpectedly (`coordinator logs` shows its last messages)."
            )
        print("Starting the coordinator in the background...")
        if not background.start(cfg, verbose=args.verbose):
            return EXIT_FAIL
    if attach and _interactive_terminal():
        background.exec_attach(background.attach_argv(cfg))
    print(
        "The coordinator is running in the background. `coordinator attach` shows it, "
        "`coordinator status` summarises it and `coordinator stop` stops it."
    )
    return EXIT_OK


def cmd_up(args: argparse.Namespace) -> int:
    """`coordinator` alone: start the coordinator in the background if needed and show it here."""
    if args.foreground:
        return _foreground(args)
    return _start(args, attach=True)


def cmd_start(args: argparse.Namespace) -> int:
    return _start(args, attach=False)


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop one ticket's session, or with no ticket the coordinator itself."""
    from delivery import background

    if args.ticket:
        return int(asyncio.run(_ticket_command(args, "stop")))
    return EXIT_OK if background.stop(_load(args)) else EXIT_FAIL


def cmd_restart(args: argparse.Namespace) -> int:
    from delivery import background

    cfg = _load(args)
    if not background.stop(cfg):
        return EXIT_FAIL
    return _start(args, attach=True)


def cmd_open(args: argparse.Namespace) -> int:
    """Open a ticket's latest readable Claude log (or its run folder, or the Jira ticket)."""
    from delivery import console, logfile
    from delivery.transcript import write_transcript

    cfg = _load(args)
    target: str
    if args.jira:
        if not args.ticket:
            print("--jira needs a ticket, for example `coordinator open SDLC-12 --jira`.", file=sys.stderr)
            return EXIT_FAIL
        target = console.ticket_url(cfg, args.ticket)
    elif not args.ticket:
        path = logfile.log_path(cfg.runtime.state_dir, cfg.identity_key)
        target = str(path if path.exists() and not args.folder else path.parent)
    else:
        store = JournalStore(cfg.runtime.state_dir, cfg.identity_key)
        runs = store.runs_for_ticket(args.ticket)
        if args.stage:
            runs = [e for e in runs if e.record and e.record.stage.value == args.stage]
        if not runs:
            print(f"No runs of {args.ticket} are recorded on this machine.", file=sys.stderr)
            return EXIT_FAIL
        entry = runs[-1]
        target = str(entry.journal.dir)
        logs = sorted((entry.journal.dir / "logs").glob("claude-*.jsonl"), key=lambda p: p.stat().st_mtime)
        if logs and not args.folder:
            # Refresh the readable copy: a session that is still working writes it only at the end.
            readable = write_transcript(logs[-1])
            target = str(readable or logs[-1])
    opener = "open" if sys.platform == "darwin" else shutil.which("xdg-open")
    if opener:
        subprocess.run([opener, target], check=False)
        print(f"Opened {target}")
    else:
        print(target)
    return EXIT_OK


def cmd_clean(args: argparse.Namespace) -> int:
    """Remove what finished runs left behind (worktrees, empty folders, optionally old logs)."""
    from delivery import background, cleanup
    from delivery.tmux import for_config

    cfg = _load(args)
    tmux = for_config(cfg.claude.interactive, cfg.runtime.state_dir)
    live = set(asyncio.run(tmux.sessions()))
    st = background.state(cfg)
    p = cleanup.plan(cfg, live, args.older_than)
    say = print
    for item in p.kept:
        say(f"  keep    {item.path}  ({cleanup.human_size(item.size)}): {item.why}")
    for item in p.worktrees:
        say(f"  remove  {item.path}  ({cleanup.human_size(item.size)}): {item.why}")
    for path in p.empty:
        say(f"  remove  {path}: empty folder")
    for item in p.logs:
        say(f"  remove  {item.path}  ({cleanup.human_size(item.size)}): local logs; {item.why}")
    for rec in p.dead_sessions:
        how = "the running coordinator tidies it" if st.running else "tidied now"
        say(f"  close   {rec.ticket_key} {rec.procedure} session: its tmux session has ended; {how}")
    if p.empty_plan:
        say("Nothing to clean.")
        return EXIT_OK
    say(f"Frees about {cleanup.human_size(p.size)}.")
    if not args.yes:
        if not _interactive_terminal():
            say("Nothing removed; run `coordinator clean --yes` to remove these.")
            return EXIT_OK
        if input("Remove these? [y/N] ").strip().lower() not in ("y", "yes"):
            say("Nothing removed.")
            return EXIT_OK
    repo = build_repo(cfg)
    if p.dead_sessions and not st.running:
        asyncio.run(_close_dead_sessions(cfg, repo, p.dead_sessions))
    asyncio.run(cleanup.apply(cfg, repo, p, say))
    say("Done.")
    return EXIT_OK


async def _close_dead_sessions(cfg: Config, repo: Any, records: list[Any]) -> None:
    """What the coordinator does when an open session ends: keep the conversation and any
    changes, then remove its worktrees. Needs no Jira or GitHub access."""
    from types import SimpleNamespace

    from delivery.open_sessions import OpenSessions

    store = JournalStore(cfg.runtime.state_dir, cfg.identity_key)
    deps: Any = SimpleNamespace(cfg=cfg, repo=repo, store=store)
    sessions = OpenSessions(deps, print, is_running=lambda _: False, busy=set())
    for rec in records:
        await sessions.close(rec, "its tmux session had ended (coordinator clean)")


def cmd_project(args: argparse.Namespace) -> int:
    """Write the team settings of your working config as a shared project file."""
    from delivery.setup import export_project

    _load(args)  # only a valid config is worth sharing
    out = Path(args.out).expanduser()
    try:
        export_project(Path(args.config).expanduser(), out, force=args.force)
    except FileExistsError:
        print(f"{out} already exists; use --force to replace it.", file=sys.stderr)
        return EXIT_CONFIG
    print(f"Wrote {out}: the team settings, without anything personal or secret.")
    print(
        f"A colleague runs `delivery setup --project {out}`, or keep it in this repository under "
        "projects/ (setup offers what it finds there)."
    )
    print(f'Your own config can use it too: add `project = "{out}"` and remove the settings it now holds.')
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


def parser(prog: str = "delivery") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="Local Jira-driven AI SDLC supervisor.")
    p.add_argument("--version", action="version", version=f"delivery {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    default_config = str(default_config_path())

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
    sp.add_argument(
        "--project",
        help="your team's shared project file (setup then asks only for your personal settings)",
    )
    sp.set_defaults(func=cmd_setup)
    sp = sub.add_parser("init", help="write a commented config template (never overwrites)")
    sp.add_argument("--config", default=default_config)
    sp.add_argument("--project", help="write a short personal config that uses this shared project file")
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
    sp = with_config(
        sub.add_parser("up", help="start the coordinator in the background if needed, then show it here")
    )
    sp.add_argument("--foreground", action="store_true", help="run it in this terminal instead")
    sp.set_defaults(func=cmd_up)
    sp = with_config(sub.add_parser("start", help="start the coordinator in the background"))
    sp.set_defaults(func=cmd_start)
    sp = with_config(sub.add_parser("restart", help="stop the coordinator and start it again (new code)"))
    sp.set_defaults(func=cmd_restart)
    sp = with_config(sub.add_parser("run", help="run the supervisor in the foreground"))
    sp.add_argument("--once", action="store_true", help="one cycle: dispatch all eligible tickets and wait")
    sp.add_argument("--dry-run", action="store_true", help="discovery only: no Claude, writes or transitions")
    sp.set_defaults(afunc=_run)
    sp = with_config(sub.add_parser("status", help="sessions, states and next human actions"))
    sp.set_defaults(afunc=_status)
    sp = with_config(
        sub.add_parser("stop", help="stop the coordinator, or with a ticket only that ticket's session")
    )
    sp.add_argument("ticket", nargs="?")
    sp.set_defaults(func=cmd_stop)
    for name, helptext in (
        ("inspect", "explain one ticket without changing anything"),
        ("recover", "reconcile one ticket; --resume continues held work"),
        ("handover", "stop and checkpoint one ticket for reassignment"),
    ):
        sp = with_config(sub.add_parser(name, help=helptext))
        sp.add_argument("ticket")
        if name == "recover":
            sp.add_argument("--resume", action="store_true")
        if name == "inspect":
            sp.set_defaults(afunc=_inspect)
        else:
            sp.set_defaults(afunc=lambda a, n=name: _ticket_command(a, n))
    sp = with_config(
        sub.add_parser("logs", help="a ticket's readable Claude session log, or the coordinator's own log")
    )
    sp.add_argument("ticket", nargs="?", help="without one: the coordinator's log")
    sp.add_argument("--lines", "-n", type=int, default=60, help="coordinator log: lines to show")
    sp.add_argument("--list", action="store_true", help="list the ticket's runs")
    sp.add_argument("--stage", choices=[st.value for st in Stage], help="latest run of this stage")
    sp.add_argument("--run", help="a specific run ID (see --list)")
    sp.add_argument("--follow", "-f", action="store_true", help="keep printing as a running session works")
    sp.add_argument("--results", dest="verbose_results", action="store_true", help="also show tool output")
    sp.add_argument("--raw", action="store_true", help="print the raw log file paths (for jq)")
    sp.set_defaults(func=cmd_logs)
    sp = with_config(
        sub.add_parser("attach", help="show a ticket's Claude session, or the coordinator, in this terminal")
    )
    sp.add_argument("ticket", nargs="?", help="without one: the coordinator itself")
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
    sp = with_config(
        sub.add_parser("open", help="open a ticket's latest Claude log (or the coordinator's log)")
    )
    sp.add_argument("ticket", nargs="?")
    sp.add_argument("--folder", action="store_true", help="open the run folder instead")
    sp.add_argument("--jira", action="store_true", help="open the ticket in Jira")
    sp.add_argument("--stage", choices=[st.value for st in Stage], help="that stage's latest run")
    sp.set_defaults(func=cmd_open)
    sp = with_config(sub.add_parser("clean", help="remove worktrees and folders finished runs left behind"))
    sp.add_argument("--yes", action="store_true", help="remove without asking")
    sp.add_argument(
        "--older-than",
        type=int,
        metavar="DAYS",
        help="also remove local logs of tickets whose runs all finished more than DAYS ago",
    )
    sp.set_defaults(func=cmd_clean)
    proj = sub.add_parser("project", help="the team's shared project file").add_subparsers(
        dest="proj", required=True
    )
    sp = with_config(proj.add_parser("export", help="write your config's team settings as a project file"))
    sp.add_argument("out")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_project)
    sp = with_config(sub.add_parser("dispatch", help="pause or resume new launches"))
    sp.add_argument("action", choices=["pause", "resume"])
    sp.add_argument("--reason")
    sp.set_defaults(afunc=_dispatch)
    sp = sub.add_parser("schemas", help="export JSON schemas (development)")
    sp.add_argument("--out", default="plugins/delivery/references/schemas")
    sp.set_defaults(func=cmd_schemas)
    return p


def main(argv: list[str] | None = None, prog: str = "delivery") -> int:
    args = parser(prog).parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    for h in logging.getLogger().handlers:
        # The terminal shows warnings (and debug with -v); the coordinator log keeps the rest.
        h.setLevel(logging.DEBUG if args.verbose else logging.WARNING)
        h.addFilter(lambda r: not getattr(r, "file_only", False))
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
    """``coordinator`` alone starts the coordinator in the background (if it is not running) and
    shows it; ``coordinator <command>`` is ``delivery <command>``.

    Options without a command go to ``up`` (``coordinator --foreground``), except ``--dry-run``
    and ``--once``, which belong to ``run``, ``-v``, the global verbose flag, and
    ``--help``/``--version``, which describe everything.
    """
    if any(a in commands for a in argv) or any(a in ("-h", "--help", "--version") for a in argv):
        return argv
    verbose = [a for a in argv if a in ("-v", "--verbose")]
    rest = [a for a in argv if a not in ("-v", "--verbose")]
    command = "run" if any(a in ("--dry-run", "--once") for a in rest) else "up"
    return [*verbose, command, *rest]


def coordinator_main(argv: list[str] | None = None) -> int:
    """Entry point of the ``coordinator`` command (see :func:`coordinator_args`)."""
    p = parser()
    sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    args = coordinator_args(sys.argv[1:] if argv is None else argv, set(sub.choices))
    return main(args, prog="coordinator")


if __name__ == "__main__":
    sys.exit(main())
