"""Stage strategies: prepare isolated inputs, run procedures, decide, publish.

``work`` returns a persisted :class:`Decision`; ``publish`` turns it into side effects
through the idempotent :class:`Publisher`. Publication can therefore be repeated after
a crash without duplicating comments, commits, PRs or transitions.
"""

from __future__ import annotations

import asyncio
import dataclasses
import fnmatch
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from delivery import comments, deviations
from delivery.checks import all_passed, run_checks
from delivery.claude import ChildHandle, ClaudeInvocation, ClaudeOutcome, ClaudeStatus, OpenSession
from delivery.diagnose import install_root
from delivery.feedback import claude_notes
from delivery.gates import (
    approved_gate,
    current_gate,
    evaluate_ci,
    gate_token,
    supersede_for_new_revision,
)
from delivery.git import GitError, blob_url, commit_url
from delivery.guardrails import Watchdog
from delivery.interactive import RESULT_FILE
from delivery.journal import atomic_write_json, ensure_private_dir
from delivery.models import (
    ArtefactPointer,
    ArtifactKind,
    Brief,
    CheckResult,
    DecisionEvidence,
    Deviation,
    Finding,
    Footprint,
    GateKind,
    GateRecord,
    GateState,
    InputEnvelope,
    Outcome,
    OutputContract,
    OverlapContext,
    PauseInfo,
    PriorWork,
    ResolutionInput,
    ResolutionReport,
    RunState,
    SelectedComment,
    Severity,
    SourceRefs,
    StageResult,
    digest,
    result_json_schema,
    utcnow,
)
from delivery.open_sessions import (
    FollowUp,
    FollowUpRefused,
    OpenRecord,
    SessionRegistry,
    document_digest,
    document_for,
)
from delivery.overlap import OverlapFinding
from delivery.overlap import Severity as OverlapSeverity
from delivery.permissions import PROCEDURE_ROLES, PROTECTED_WORKTREE_PATHS, Role, build_profile
from delivery.ports import JiraComment
from delivery.proposals import dump as dump_proposals
from delivery.proposals import proposals_path
from delivery.publication import Posted
from delivery.resolution import (
    asked_in_session,
    attribute,
    check_next_steps,
    current_tokens,
    ticket_state_lines,
    typed_by_developer,
)
from delivery.resolution import write_briefing as write_blocker_briefing
from delivery.resources import port_env
from delivery.results import OutputInvalid, validate_result
from delivery.runtime import Decision, RunContext
from delivery.session_hook import read_events
from delivery.tmux import for_config as tmux_for
from delivery.transcript import render_file, write_transcript
from delivery.workflow import (
    CHANGE_REQUIREMENTS,
    RESOLVED_ACTIONS,
    ROUTES,
    STAGES,
    Action,
    Actor,
    Requirement,
    Stage,
    Status,
)

MAX_OUTPUT_FILE_BYTES = 2_000_000
# Written by verify-ticket next to its report: how a person checks each criterion by hand.
ACCEPTANCE_GUIDE = "acceptance-guide.md"


class WorkerFailure(Exception):
    def __init__(self, procedure: str, outcome: ClaudeOutcome | None, detail: str) -> None:
        self.procedure = procedure
        self.outcome = outcome
        self.detail = detail
        super().__init__(f"{procedure}: {detail}")

    @property
    def blocker_kind(self) -> str:
        if self.outcome and self.outcome.status in (ClaudeStatus.AUTH, ClaudeStatus.USAGE_LIMIT):
            return "provider_" + self.outcome.status.value
        return "worker_failure"


def safe_output_file(root: Path, rel: str) -> Path:
    """Resolve a worker-supplied path inside ``root`` (no absolute paths, traversal or links)."""
    if not rel or rel.startswith(("/", "~")) or "\\" in rel or "\0" in rel:
        raise OutputInvalid(f"artifact path {rel!r} must be relative")
    parts = Path(rel).parts
    if any(p in ("..", "") for p in parts):
        raise OutputInvalid(f"artifact path {rel!r} escapes the output directory")
    candidate = root / rel
    probe = root
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            raise OutputInvalid(f"artifact path {rel!r} contains a symlink")
    resolved = candidate.resolve()
    if root.resolve() not in resolved.parents:
        raise OutputInvalid(f"artifact path {rel!r} escapes the output directory")
    if not resolved.is_file():
        raise OutputInvalid(f"artifact {rel!r} was not written")
    if resolved.stat().st_size > MAX_OUTPUT_FILE_BYTES:
        raise OutputInvalid(f"artifact {rel!r} exceeds {MAX_OUTPUT_FILE_BYTES} bytes")
    return resolved


def require_artifact(result: StageResult, out_dir: Path, kind: ArtifactKind, name: str) -> Path:
    refs = [a for a in result.artifacts if a.kind is kind]
    if not refs:
        raise OutputInvalid(f"result lists no {kind.value} artifact")
    path = safe_output_file(out_dir, refs[0].path)
    if path.name != name:
        raise OutputInvalid(f"{kind.value} artifact must be named {name}")
    return path


def is_protected(path: str) -> bool:
    return any(
        fnmatch.fnmatch(path, pat) or path == pat.rstrip("/*") or path.startswith(pat.rstrip("*"))
        for pat in PROTECTED_WORKTREE_PATHS
    )


def provenance_header(ctx: RunContext, kind: str, revision: str, extra: dict[str, Any]) -> str:
    lines = [
        "<!-- delivery provenance (written by the coordinator) -->",
        f"<!-- ticket: {ctx.key} | kind: {kind} | revision: {revision} | run: {ctx.run_id} -->",
        f"<!-- input_revision: {ctx.record.input_revision} | worker: {ctx.cfg.identity.worker_id} -->",
    ]
    for k, v in extra.items():
        lines.append(f"<!-- {k}: {v} -->")
    return "\n".join(lines) + "\n\n"


def share_check_logs(ctx: RunContext, checks: list[CheckResult]) -> None:
    """Copy coordinator check logs into the worker's read-only inputs (e.g. browser e2e,
    which cannot run inside the worker sandbox on macOS)."""
    dest = ensure_private_dir(ctx.inputs_dir / "check-logs")
    for c in checks:
        if c.log_path and Path(c.log_path).is_file():
            shutil.copy(c.log_path, dest / Path(c.log_path).name)


# Procedures that action change requests, and what they change.
CHANGES_BY_PROCEDURE = {
    "refine-ticket": "specification",
    "plan-ticket": "plan",
    "investigate-ticket": "findings",
    "implement-ticket": "code",
    "prepare-release": "release proposal",
}


# Procedures whose interactive session closes once it has handed over its result: the stage
# carries on with another procedure (a development session must not be mistaken for it).
CLOSED_AT_HAND_OFF = frozenset({"amend-spec", "resolve-conflicts", "resolve-blocker"})


def change_ids(items: dict[str, str]) -> list[str]:
    """Requested changes (F), problems the coordinator found (R), deviations to change back (D)
    and the PR's review comments (G); answers (Q) are not changes."""
    ids = {k.split("@")[0] for k in items if k[:1] in ("F", "R", "D", "G") and k.split("@")[0][1:].isdigit()}
    return sorted(ids, key=lambda i: (i[0], int(i[1:])))


def closing_note(
    procedure: str, ids: list[str], *, document: Path | None = None, preview: bool = False
) -> str:
    """Finish an interactive session that stays open: after actioning Jira change requests, say
    so item by item; development always ends this way too (and mentions the app, which is about
    to run from its worktree). Then ask for anything further: the session stays open for the
    reply, and what is asked there is published (delivery.open_sessions). A development session
    also says to type /exit when finished, because verification waits until it has closed."""
    develop = procedure == "implement-ticket"
    if procedure not in CHANGES_BY_PROCEDURE or not (ids or develop):
        return ""
    what = CHANGES_BY_PROCEDURE[procedure]
    if ids:
        opening = (
            f"This run actions change requests from Jira: {', '.join(ids)} (`feedback_items` in the "
            "envelope). After writing the result file, finish with a short message to the developer "
            'that starts "The changes requested in Jira have been actioned:" and gives one line per '
            "item saying what you did (or why you did not)."
        )
    else:
        opening = (
            "After writing the result file, finish with a short message to the developer saying what "
            "you changed (or the questions or blocker you reported)."
        )
    if develop and preview:
        opening += (
            " Say that the coordinator is now starting the app from this working copy and will open "
            "it in their browser so they can try it."
        )
    if develop:
        close = (
            "that if not, they should type /exit to end this session: review and verification of "
            "the candidate start then, not before (closing the window only hides the session)."
        )
        further = (
            "If they ask for more, make those changes here too: the coordinator pushes them as the "
            "next candidate, and the latest candidate is reviewed and verified once they type /exit."
        )
    else:
        close = "that if not, they can close this window (or type /exit)."
        further = (
            f"If they ask for more, edit {document or f'the {what}'} in place: the coordinator "
            f"publishes it as the next revision of the {what} for review."
        )
    ask = '"Are there any further changes you\'d like to make?"'
    return f"{opening} End by asking {ask} and saying {close} {further}"


def work_kind(ctx: RunContext) -> Literal["feature", "bug", "spike"]:
    kind = ctx.cfg.flow.kind_of(ctx.ticket.issue.view.issue_type)
    return "spike" if kind == "spike" else "bug" if kind == "bug" else "feature"


def _has_markers(path: Path) -> bool:
    """A file still holding Git conflict markers."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    return any(ln.startswith(("<<<<<<< ", ">>>>>>> ")) for ln in text.splitlines())


def notes_for(ctx: RunContext) -> list[tuple[JiraComment, str]]:
    """`FOR CLAUDE` notes for this run's stage from the developer, assignee or approvers."""
    view = ctx.ticket.issue.view
    humans = {ctx.cfg.identity.developer_jira_account_id, *ctx.cfg.approvals.jira_account_ids}
    if view.assignee_account_id:
        humans.add(view.assignee_account_id)
    return claude_notes(ctx.ticket.comments, stage=ctx.record.stage.value, allowed_authors=humans)


def _rev_numbers(names: list[str], pattern: str) -> list[int]:
    out = []
    for n in names:
        m = re.search(pattern, n)
        if m:
            out.append(int(m.group(1)))
    return out


# --------------------------------------------------------------------------- base strategy


class StageStrategy:
    stage: ClassVar[Stage]

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.deps = ctx.deps
        # Set when publishing a document edited in this stage's open session: the ticket stays
        # in its review status and the new revision supersedes the one under review.
        self.follow_up: FollowUp | None = None
        # The specification rewritten in this run to include accepted deviations, until
        # publication makes it the approved revision (see amend_specification).
        self.amended_spec: ArtefactPointer | None = None

    # ------------------------------------------------------------------ workspace
    async def delivery_worktree(self) -> Path:
        """Worktree on the ticket's delivery branch (append-only artefacts)."""
        path = self.ctx.worktree_path("delivery")
        if path.exists():
            return path
        repo = self.deps.repo
        start = (
            f"origin/{self.ctx.delivery_branch}"
            if await repo.remote_sha(self.ctx.delivery_branch)
            else f"origin/{self.ctx.cfg.repository.base_branch}"
        )
        await repo.add_worktree(path, start=start, branch=self.ctx.delivery_branch)
        self.ctx.record.worktrees["delivery"] = str(path)
        return path

    async def detached_worktree(self, name: str, ref: str) -> Path:
        path = self.ctx.worktree_path(name)
        if path.exists():
            return path
        await self.deps.repo.add_worktree(path, start=ref)
        self.ctx.record.worktrees[name] = str(path)
        return path

    async def revisions(self, sub: str, pattern: str = r"v(\d{3,4})\.md$") -> list[int]:
        branch = self.ctx.delivery_branch
        if not await self.deps.repo.remote_sha(branch):
            return []
        names = await self.deps.repo.ls_tree(f"origin/{branch}", f"{self.ctx.doc_root}/{sub}/")
        return _rev_numbers(names, pattern)

    async def copy_input(self, ref: str, path: str, dest_name: str) -> Path | None:
        data = await self.deps.repo.show_file(ref, path)
        if data is None:
            return None
        dest = self.ctx.inputs_dir / dest_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    async def approved_input(self, kind: GateKind, art_kind: ArtifactKind) -> ArtefactPointer | None:
        gate = current_gate(self.ctx.shared.gates, kind)
        if gate is None or not gate.artefact_path or not gate.artefact_commit:
            return None
        # Specification and plan revisions share file names (v001.md): prefix the kind.
        dest = await self.copy_input(
            gate.artefact_commit,
            gate.artefact_path,
            f"approved/{art_kind.value}-{Path(gate.artefact_path).name}",
        )
        if dest is None:
            return None
        return ArtefactPointer(
            kind=art_kind,
            path=str(dest),
            revision=gate.revision,
            commit=gate.artefact_commit,
            url=blob_url(self.ctx.cfg.repository.url, gate.artefact_commit, gate.artefact_path),
        )

    # ------------------------------------------------------------------ envelope
    def envelope(
        self,
        procedure: str,
        out_dir: Path,
        *,
        required: list[ArtifactKind],
        next_revision: int | None = None,
        approved: list[ArtefactPointer] | None = None,
        prior: list[ArtefactPointer] | None = None,
        source: SourceRefs | None = None,
        related: list[OverlapContext] | None = None,
        ports: dict[str, int] | None = None,
        review_report: str | None = None,
        write_globs: list[str] | None = None,
        prior_work: PriorWork | None = None,
        feedback: dict[str, str] | None = None,
    ) -> InputEnvelope:
        ctx = self.ctx
        issue = ctx.ticket.issue
        brief = Brief(
            summary=issue.view.summary,
            description=issue.description_text,
            issue_type=issue.view.issue_type,
            labels=list(issue.view.labels),
            digest=ctx.record.brief_digest or "",
        )
        selected = [
            SelectedComment(
                id=c.id,
                author_account_id=c.author_account_id,
                created=c.created,
                updated=c.updated,
                body=c.body_text,
                digest=digest(c.body_text),
            )
            for c in ctx.intake.selected
        ]
        return InputEnvelope(
            run_id=ctx.run_id,
            attempt=ctx.record.attempt,
            ticket_key=ctx.key,
            stage=self.stage,
            procedure=procedure,
            input_revision=ctx.record.input_revision or "",
            work_kind=work_kind(ctx),
            fast_track=ctx.cfg.flow.fast_track(issue.view.labels),
            brief=brief,
            selected_comments=selected,
            attachments=ctx.attachments,
            attachments_skipped=ctx.attachments_skipped,
            designs=ctx.designs,
            designs_skipped=ctx.designs_skipped,
            linked_tickets=ctx.linked,
            prior_work=prior_work,
            clarification_round=ctx.intake.round_token,
            feedback_token=ctx.intake.feedback_token,
            feedback_items=dict(ctx.intake.feedback_items if feedback is None else feedback),
            changes_requested=ctx.intake.requirement in CHANGE_REQUIREMENTS,
            notes=self.notes(),
            project_guidance=str(ctx.guidance) if ctx.guidance else None,
            approved_artefacts=approved or [],
            prior_drafts=prior or [],
            source=source or self.source_refs(),
            output=OutputContract(
                artifact_dir=str(out_dir),
                allowed_write_globs=write_globs or [f"{out_dir}/**"],
                required_kinds=required,
                next_revision=next_revision,
            ),
            configured_checks=list(ctx.cfg.checks.commands),
            review_report_path=review_report,
            related_work=related or [],
            ports=ports or {},
            policy={"plugin_digest": ctx.deps.plugin.digest, "config_digest": ctx.cfg.digest()},
        )

    def notes(self) -> list[SelectedComment]:
        """`FOR CLAUDE` comments for this stage. Not part of the run's material inputs: a note
        never starts or restarts work, it guides the next session that runs."""
        notes = [
            SelectedComment(
                id=c.id,
                author_account_id=c.author_account_id,
                created=c.created,
                updated=c.updated,
                body=text,
                digest=digest(text),
            )
            for c, text in notes_for(self.ctx)
        ]
        resolved = self.resolution_note()
        return [resolved, *notes] if resolved else notes

    def resolution_note(self) -> SelectedComment | None:
        """What the developer and Claude settled when they cleared the blocker this run resumes
        from: only the first run after a resolution gets it (the ticket came from Resolving)."""
        ctx = self.ctx
        if ctx.intake.requirement is not Requirement.RESOLVED:
            return None
        for e in reversed(self.deps.store.runs_for_ticket(ctx.key)):
            rec = e.record
            info = rec.outputs.get("resolution") if rec is not None else None
            if rec is None or rec.stage is not Stage.RESOLUTION or not info:
                continue
            lines = [
                f"The previous attempt at this stage was blocked, and the developer cleared the blocker "
                f"with Claude in a resolution session ({e.run_id}).",
                f"What was found and done: {info.get('summary', '')}",
                *[f"- Did: {a}" for a in info.get("actions", [])],
                *[
                    f"- Decision {d['id']} by "
                    f"{'the developer' if d['decided_by'] == 'developer' else 'Claude'}: "
                    f"{d['question']} {d['decision']} ({d.get('basis', '')})"
                    for d in info.get("decisions", [])
                ],
                *[f"- Left for people: {f}" for f in info.get("follow_ups", [])],
                "Follow these where they fit the approved specification and plan; they never widen scope.",
            ]
            text = "\n".join(lines)
            now = utcnow()
            return SelectedComment(
                id=f"resolution:{e.run_id}",
                author_account_id=ctx.cfg.identity.developer_jira_account_id,
                created=now,
                updated=now,
                body=text,
                digest=digest(text),
            )
        return None

    def source_refs(self, **kw: str | None) -> SourceRefs:
        ctx = self.ctx
        return SourceRefs(
            repository=ctx.cfg.repository.url,
            base_branch=ctx.cfg.repository.base_branch,
            delivery_branch=ctx.delivery_branch,
            feature_branch=ctx.feature_branch,
            base_commit=kw.get("base_commit"),
            delivery_commit=kw.get("delivery_commit"),
            feature_commit=kw.get("feature_commit"),
            candidate_sha=kw.get("candidate_sha") or ctx.shared.candidate_sha,
        )

    async def _save_unfinished(
        self, wt: Path, start_sha: str, spec: ArtefactPointer, plan: ArtefactPointer
    ) -> None:
        """Keep a session's unfinished changes so the next development run continues from them."""
        ctx, repo = self.ctx, self.deps.repo
        try:
            await repo.git("add", "-A", cwd=wt)
            target = ctx.journal.dir / "wip"
            target.mkdir(mode=0o700, exist_ok=True)
            patch = target / "changes.patch"
            await repo.git("diff", "--cached", "--binary", f"--output={patch}", start_sha, cwd=wt)
            files = await repo.changed_paths(wt, start_sha)
        except (GitError, OSError) as exc:
            ctx.journal.events.append("wip_not_saved", {"error": str(exc)[:300]})
            return
        if not files or not patch.exists() or patch.stat().st_size == 0:
            return
        patch.chmod(0o600)
        meta = {"start_sha": start_sha, "spec": _ref(spec), "plan": _ref(plan), "files": files}
        ctx.record.outputs["wip"] = meta
        ctx.save("wip_saved", files=len(files))

    # ------------------------------------------------------------------ procedures
    async def run_procedure(
        self,
        procedure: str,
        worktree: Path,
        envelope: InputEnvelope,
        *,
        ports: dict[str, int] | None = None,
        expect_extra: dict[str, Any] | None = None,
    ) -> StageResult:
        ctx = self.ctx
        out_dir = Path(envelope.output.artifact_dir)
        role = PROCEDURE_ROLES[procedure]
        profile = build_profile(
            role,
            worktree=worktree,
            output_dir=out_dir,
            inputs_dir=ctx.inputs_dir,
            tmp_dir=ctx.tmp_dir,
            readonly_dirs=(ctx.cfg.claude.plugin_path,),
        )
        settings_path = ctx.journal.dir / f"settings-{procedure}.json"
        atomic_write_json(settings_path, profile.settings)
        # The envelope must live in a directory the restricted session may read.
        env_path = ctx.inputs_dir / f"envelope-{procedure}.json"
        atomic_write_json(env_path, envelope)
        # Interactive sessions write their result to a file and read the schema from here.
        schema_path = ctx.inputs_dir / "result.schema.json"
        atomic_write_json(schema_path, result_json_schema())
        inv = ClaudeInvocation(
            run_id=ctx.run_id,
            procedure=procedure,
            envelope_path=env_path,
            cwd=worktree,
            plugin_dir=ctx.cfg.claude.plugin_path,
            schema=result_json_schema(),
            settings_path=settings_path,
            tools=profile.tools,
            permission_mode=profile.permission_mode,
            human_present=role is Role.RESOLVER,
            add_dirs=profile.add_dirs,
            timeout=ctx.cfg.claude.timeout_for(procedure, ctx.cfg.runtime.timeout_seconds),
            stdout_path=ctx.logs_dir / f"claude-{procedure}.jsonl",
            stderr_path=ctx.logs_dir / f"claude-{procedure}.stderr.log",
            max_turns=ctx.cfg.claude.turns_for(procedure),
            model=ctx.cfg.claude.model_for(procedure),
            extra_env={"TMPDIR": str(ctx.tmp_dir), **port_env(ports or {})},
            ticket=ctx.key,
            session_dir=ctx.journal.dir / "sessions" / procedure,
            result_path=out_dir / RESULT_FILE,
            schema_path=schema_path,
            closing=closing_note(
                procedure,
                change_ids(envelope.feedback_items),
                document=out_dir / doc.filename if (doc := document_for(self.stage, procedure)) else None,
                preview=ctx.cfg.preview.enabled,
            )
            if ctx.cfg.claude.interactive.follow_ups
            else "",
            expect={
                "contract_id": ctx.deps.plugin.contracts[procedure],
                "procedure": procedure,
                "run_id": ctx.run_id,
                "ticket": ctx.key,
                "stage": self.stage.value,
                "input_revision": ctx.record.input_revision or "",
                **(expect_extra or {}),
            },
        )
        ctx.journal.events.append(
            "claude_start",
            {
                "procedure": procedure,
                "session": inv.session_id,
                "role": role.value,
                "model": inv.model or "claude-code-default",
            },
        )

        def on_start(child: ChildHandle) -> None:
            if ctx.on_child:
                ctx.on_child(ctx.key, child)
            from delivery.models import ChildProcess

            ctx.record = ctx.record.model_copy(
                update={
                    "child": ChildProcess(
                        pid=child.pid,
                        pgid=child.pid,
                        started_at=utcnow(),
                        argv0=ctx.cfg.claude.executable,
                        session_label=inv.session_id,
                    ),
                    "state": RunState.RUNNING,
                }
            )
            ctx.save("child_started", procedure=procedure, pid=child.pid)
            guards = ctx.cfg.claude.guardrails
            watchdog.append(
                Watchdog(
                    inv.stdout_path,
                    loop_repeats=guards.loop_repeats,
                    stall_seconds=guards.stall_minutes * 60,
                    stop=child.stop,
                    quiet_ok=child.human_active,
                )
            )
            watch_tasks.append(asyncio.create_task(watchdog[0].run()))

        watchdog: list[Watchdog] = []
        watch_tasks: list[asyncio.Task[None]] = []
        try:
            outcome = await self.deps.claude.run(inv, on_start=on_start)
        finally:
            for t in watch_tasks:
                t.cancel()
            if ctx.on_child:
                ctx.on_child(ctx.key, None)
            ctx.record = ctx.record.model_copy(update={"child": None})
            write_transcript(inv.stdout_path)
        if watchdog and watchdog[0].reason:
            outcome = dataclasses.replace(
                outcome, status=ClaudeStatus.GUARDRAIL, detail=f"stopped by a guardrail: {watchdog[0].reason}"
            )
        ctx.journal.events.append(
            "claude_end",
            {
                "procedure": procedure,
                "status": outcome.status.value,
                "detail": outcome.detail,
                "turns": outcome.num_turns,
                "denials": len(outcome.permission_denials),
                "duration": round(outcome.duration, 1),
            },
        )
        atomic_write_json(
            ctx.journal.dir / f"claude-{procedure}.json",
            {
                "status": outcome.status.value,
                "detail": outcome.detail,
                "subtype": outcome.subtype,
                "plugins": outcome.plugins,
                "plugin_errors": outcome.plugin_errors,
                "permission_denials": outcome.permission_denials,
                "session_id": outcome.session_id,
                "structured": outcome.structured,
            },
        )
        if outcome.status is not ClaudeStatus.OK:
            raise WorkerFailure(procedure, outcome, outcome.detail or outcome.status.value)
        try:
            result = validate_result(
                outcome.structured,
                contract_id=ctx.deps.plugin.contracts[procedure],
                procedure=procedure,
                run_id=ctx.run_id,
                ticket=ctx.key,
                stage=self.stage,
                input_revision=ctx.record.input_revision or "",
            )
        except OutputInvalid as exc:
            if outcome.open_session is not None:
                await tmux_for(ctx.cfg.claude.interactive).kill(outcome.open_session.name)
            so = outcome.structured or {}
            hint = (
                f" (worker reported: {str(so.get('blocker_reason') or so.get('summary'))[:300]})"
                if (so.get("outcome") in ("blocked", "failed"))
                else ""
            )
            raise WorkerFailure(procedure, outcome, f"output rejected: {exc}{hint}") from None
        if outcome.open_session is not None:
            if procedure in CLOSED_AT_HAND_OFF:
                await tmux_for(ctx.cfg.claude.interactive).kill(outcome.open_session.name)
            else:
                self.keep_open(procedure, worktree, outcome.open_session, out_dir)
        return result

    def keep_open(self, procedure: str, worktree: Path, session: OpenSession, out_dir: Path) -> None:
        """Track a session left open after hand-off (delivery.open_sessions); keep its worktree."""
        ctx = self.ctx
        events = len(read_events(session.session_dir))
        SessionRegistry(ctx.cfg.runtime.state_dir).save(
            OpenRecord(
                name=session.name,
                ticket_key=ctx.key,
                run_id=ctx.run_id,
                stage=self.stage,
                procedure=procedure,
                worktree=str(worktree),
                worktrees=[str(worktree)],
                journal_dir=str(ctx.journal.dir),
                session_dir=str(session.session_dir),
                transcript=session.transcript or "",
                mirrored_lines=session.mirrored_lines,
                events_seen=events,
                out_dir=str(out_dir),
                document=document_digest(out_dir, self.stage, procedure),
            )
        )
        ctx.journal.events.append(
            "session_kept_open",
            {"procedure": procedure, "tmux_session": session.name, "human_prompts": session.human_prompts},
        )

    async def follow_up_decision(self, d: Decision, rev: int) -> Decision:
        """The decision that publishes this stage's document, edited in its open session after
        ``d`` was published, as revision ``rev`` (see delivery.open_sessions)."""
        return d.model_copy(update={"outcome": "success", "extra": {**d.extra, "revision": rev}})

    def gate_note(self) -> str:
        """The note in a gate comment: for a follow-up, what was asked for."""
        if self.follow_up is None:
            return ""
        return comments.follow_up_revision(self.stage.value, self.follow_up.replaces, self.follow_up.prompts)

    def worker_decision(self, result: StageResult) -> Decision | None:
        """Map a non-completed worker outcome to a decision."""
        if result.outcome is Outcome.BLOCKED:
            return Decision(
                outcome="blocked",
                reason=result.blocker_reason,
                action="Resolve the blocker described by the worker, then resume.",
                blocker_kind="worker_blocked",
                result=result.model_dump(mode="json"),
            )
        if result.outcome is Outcome.FAILED:
            return Decision(
                outcome="blocked",
                reason=f"worker could not complete: {result.summary}",
                action="Inspect the run logs (`delivery inspect`), then resume.",
                blocker_kind="worker_failed",
                result=result.model_dump(mode="json"),
            )
        return None

    # ------------------------------------------------------------------ accepted deviations
    async def spec_input(self) -> ArtefactPointer | None:
        """The specification this run works to: rewritten in this run, or the approved one."""
        return self.amended_spec or await self.approved_input(GateKind.SPEC, ArtifactKind.SPECIFICATION)

    async def deviation_details(self) -> dict[str, Deviation]:
        """Full text of the latest verification's deviations (deviations.json next to its review)."""
        ref = self.ctx.shared.artefacts.get("deviations")
        if not ref or "@" not in ref:
            return {}
        path, commit = ref.rsplit("@", 1)
        data = await self.deps.repo.show_file(commit, path)
        if data is None:
            return {}
        try:
            return {d.id: d for d in (Deviation.model_validate(x) for x in json.loads(data))}
        except (ValueError, TypeError):
            return {}

    async def amend_specification(self) -> Decision | None:
        """Rewrite the approved specification to include the deviations accepted with the code
        (``intake.accepted_deviations``), without a new refinement round. The rewrite becomes
        ``self.amended_spec`` for the rest of this run and is published, already approved, by
        ``publish_amendment``. Returns a decision only when the rewrite could not be made."""
        ctx = self.ctx
        ids = list(ctx.intake.accepted_deviations)
        if not ids:
            return None
        out = ctx.output_dir("amend-spec")
        done = ctx.record.outputs.get("amendment")
        if not done:
            spec = await self.approved_input(GateKind.SPEC, ArtifactKind.SPECIFICATION)
            if spec is None:
                return Decision(
                    outcome="blocked",
                    reason="the approved specification could not be read to include the accepted deviations",
                    action="Check the delivery branch and resume.",
                    blocker_kind="missing_input",
                )
            details = await self.deviation_details()
            records = {d.id: d for d in ctx.shared.deviations}
            items = {d: deviations.describe(details.get(d), records[d]) for d in ids if d in records}
            revs = await self.revisions("specification")
            rev = max([*revs, ctx.shared.spec_revision, 0]) + 1
            candidate = ctx.shared.candidate_sha or f"origin/{ctx.cfg.repository.base_branch}"
            wt = await self.detached_worktree("amend", candidate)
            review = None
            ref = ctx.shared.artefacts.get("review")
            if ref and "@" in ref:
                path, commit = ref.rsplit("@", 1)
                got = await self.copy_input(commit, path, "deviations-review.md")
                review = str(got) if got else None
            env = self.envelope(
                "amend-spec",
                out,
                required=[ArtifactKind.SPECIFICATION],
                next_revision=rev,
                approved=[spec],
                review_report=review,
                source=self.source_refs(candidate_sha=ctx.shared.candidate_sha),
                feedback=items,
            )
            result = await self.run_procedure("amend-spec", wt, env)
            if (d := self.worker_decision(result)) is not None:
                return d.model_copy(
                    update={
                        "reason": f"the specification could not be rewritten to include {', '.join(ids)}: "
                        + d.reason,
                        "action": "Resolve what Claude reported (or change the deviations back with a "
                        "change request), then resume.",
                    }
                )
            require_artifact(result, out, ArtifactKind.SPECIFICATION, "specification.md")
            done = {
                "revision": rev,
                "accepted": ids,
                "summary": result.summary,
                "amends": spec.revision,
                "at": utcnow().isoformat(),
            }
            ctx.record.outputs["amendment"] = done
            ctx.save("specification_amended", revision=rev, deviations=ids)
        rev = int(done["revision"])
        dest = ctx.inputs_dir / "approved" / f"specification-v{rev:03d}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(out / "specification.md", dest)
        self.amended_spec = ArtefactPointer(kind=ArtifactKind.SPECIFICATION, path=str(dest), revision=rev)
        return None

    async def publish_amendment(self) -> None:
        """Publish this run's rewritten specification as the approved revision. Approval comes
        from the human move that started this run (Accept delivery, after Approve code); the plan
        and the candidate's gates still stand (only the specification changed, to match the
        code). Repeating it after a crash changes nothing."""
        ctx = self.ctx
        done = ctx.record.outputs.get("amendment")
        if not done:
            return
        rev = int(done["revision"])
        ids = list(done["accepted"])
        token = gate_token(ctx.key, GateKind.SPEC, rev)
        spec_rel = f"{ctx.doc_root}/specification/v{rev:03d}.md"
        entry = ctx.intake.entry
        header = provenance_header(
            ctx,
            "specification",
            f"v{rev:03d}",
            {
                "status": "approved (deviations accepted with the code)",
                "deviations": ",".join(ids),
                "accepted_by_transition": entry.history_id if entry else "-",
                "amends": f"v{int(done.get('amends', rev - 1)):03d}",
            },
        )
        text = (ctx.output_dir("amend-spec") / "specification.md").read_text()
        sha = await self.publish_files(
            {spec_rel: header + text},
            "spec-amended",
            f"v{rev}",
            f"{ctx.key}: specification v{rev:03d} (accepted deviations {', '.join(ids)})",
        )
        at = datetime.fromisoformat(done["at"]) if done.get("at") else utcnow()
        gate = GateRecord(
            token=token,
            kind=GateKind.SPEC,
            ticket_key=ctx.key,
            revision=rev,
            artefact_path=spec_rel,
            artefact_commit=sha,
            candidate_sha=ctx.shared.candidate_sha,
            published_at=at,
            approvers=ctx.cfg.approvals.jira_account_ids,
            state=GateState.APPROVED,
            decided_at=at,
            evidence=DecisionEvidence(
                history_id=entry.history_id,
                transition_author=entry.author_account_id,
                transition_at=entry.created,
            )
            if entry
            else None,
        )
        # Published again after a crash: the gate is replaced where it is, so nothing changes.
        gates = [
            gate
            if g.token == token
            else g.model_copy(update={"state": GateState.SUPERSEDED, "superseded_by": token})
            if g.kind is GateKind.SPEC and g.state is not GateState.SUPERSEDED
            else g
            for g in ctx.shared.gates
        ]
        if not any(g.token == token for g in gates):
            gates.append(gate)
        n = ctx.shared.candidate_number
        devs = [
            d.model_copy(update={"state": "accepted", "spec_revision": rev})
            if d.id in ids and d.candidate == n and d.state == "open"
            else d
            for d in ctx.shared.deviations
        ]
        url = blob_url(ctx.cfg.repository.url, sha, spec_rel)
        ctx.shared = ctx.shared.model_copy(
            update={
                "gates": gates,
                "spec_revision": rev,
                "artefacts": {**ctx.shared.artefacts, "specification": f"{spec_rel}@{sha}"},
                "deviations": devs,
                "updated_at": utcnow(),
            }
        )
        await self.announce(
            "spec-amended",
            comments.spec_amended(
                rev,
                url,
                [d for d in devs if d.id in ids and d.candidate == n],
            ),
            f"v{rev}",
        )
        await ctx.publisher().save_record(ctx.key, ctx.shared, "spec-amended")

    # ------------------------------------------------------------------ publication helpers
    async def publish_files(self, files: dict[str, str], op_type: str, revision: str, message: str) -> str:
        """Write ``{repo_path: text}`` to the delivery branch and push. Returns the commit."""
        wt = await self.delivery_worktree()
        for rel, text in files.items():
            dest = wt / rel
            if dest.exists() and dest.read_text() != text and re.search(r"/v\d{3,4}\.", rel):
                raise OutputInvalid(f"revision file {rel} already exists with different content")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
        sha = await self.ctx.publisher().commit_and_push(
            wt,
            self.ctx.delivery_branch,
            op_type,
            message,
            revision=revision,
            paths=sorted(files),
        )
        if sha is None:
            sha = await self.deps.repo.worktree_head(wt)
        return sha

    def execution_summary(self, decision: Decision, extra: dict[str, Any]) -> str:
        ctx = self.ctx
        rec = ctx.record
        data = {
            "schema_version": 1,
            "ticket_key": ctx.key,
            "run_id": ctx.run_id,
            "attempt": rec.attempt,
            "stage": self.stage.value,
            "worker_id": rec.worker_id,
            "session_label": rec.session_label,
            "input_revision": rec.input_revision,
            "brief_digest": rec.brief_digest,
            "selected_comment_ids": rec.selected_comment_ids,
            "plugin_digest": rec.plugin_digest,
            "config_digest": rec.config_digest,
            "started_at": rec.started_at.isoformat() if rec.started_at else None,
            "decision": decision.outcome,
            "reason": decision.reason,
            "checks": [c.model_dump(mode="json") for c in rec.checks],
            **extra,
        }
        return json.dumps(data, indent=2, sort_keys=True, default=str) + "\n"

    async def publish_block(self, d: Decision, source: Status, markdown: str | None = None) -> None:
        """Move an active stage to Blocked with the resume stage recorded first."""
        ctx = self.ctx
        pub = ctx.publisher()
        resume = Stage(d.resume_stage) if d.resume_stage else self.stage
        ctx.shared = ctx.shared.model_copy(
            update={
                "pause": PauseInfo(
                    kind="blocker",
                    resume_stage=resume,
                    reason=d.reason[:1900],
                    blocker_kind=d.blocker_kind,
                    published_at=utcnow(),
                    gate_token=d.gate_token,
                ),
                "current_run_id": ctx.run_id,
                "current_stage": self.stage,
                "current_state": RunState.BLOCKED,
                "updated_at": utcnow(),
            }
        )
        await self.announce(
            "blocked",
            markdown or comments.blocked(self.stage.value, d.reason, d.action, resume.value),
            pause=True,
        )
        await pub.set_resume_field(ctx.key, resume.value, "blocked")
        await pub.save_record(ctx.key, ctx.shared, "blocked")
        await pub.transition(ctx.key, source, Action.BLOCK_STAGE)

    async def announce(
        self,
        op_type: str,
        markdown: str,
        revision: str = "",
        *,
        gate_tokens: tuple[str, ...] = (),
        pause: bool = False,
    ) -> Posted:
        """Post a comment and stamp the gates/pause it announces with Jira's creation time.

        Human decisions are compared with Jira timestamps, so the gate start must come
        from the same clock, never from this machine's clock.
        """
        posted = await self.ctx.publisher().comment(self.ctx.key, op_type, markdown, revision)
        sh = self.ctx.shared
        if gate_tokens:
            sh = sh.model_copy(
                update={
                    "gates": [
                        g.model_copy(
                            update={"published_at": posted.created, "published_comment_id": posted.id}
                        )
                        if g.token in gate_tokens
                        else g
                        for g in sh.gates
                    ]
                }
            )
        if pause and sh.pause is not None:
            sh = sh.model_copy(
                update={
                    "pause": sh.pause.model_copy(
                        update={"published_at": posted.created, "comment_id": posted.id}
                    )
                }
            )
        self.ctx.shared = sh
        return posted

    # ------------------------------------------------------------------ interface
    async def preflight(self) -> Decision | None:
        return None

    async def work(self) -> Decision:
        raise NotImplementedError

    async def publish(self, d: Decision) -> None:
        raise NotImplementedError

    async def cleanup(self) -> None:
        held = SessionRegistry(self.ctx.cfg.runtime.state_dir).held_worktrees(self.ctx.run_id)
        for name, path in list(self.ctx.record.worktrees.items()):
            try:
                if path in held:
                    # An open session still works here; it is removed when the session closes.
                    # Free any other branch for later runs (the feature branch stays, so that
                    # follow-up changes commit to it; a new development run closes the session).
                    if name != "feature":
                        await self.deps.repo.git("checkout", "-q", "--detach", cwd=Path(path), check=False)
                    continue
                await self.deps.repo.remove_worktree(Path(path))
            except Exception as exc:
                self.ctx.journal.events.append("cleanup_failed", {"worktree": name, "error": str(exc)})


# --------------------------------------------------------------------------- refinement


class RefinementStage(StageStrategy):
    stage = Stage.REFINEMENT

    async def work(self) -> Decision:
        ctx = self.ctx
        wt = await self.delivery_worktree()
        revs = await self.revisions("specification")
        nxt = max([*revs, ctx.shared.spec_revision, 0]) + 1
        prior: list[ArtefactPointer] = []
        if revs:
            latest = max(revs)
            path = f"{ctx.doc_root}/specification/v{latest:03d}.md"
            dest = await self.copy_input(f"origin/{ctx.delivery_branch}", path, f"prior/{Path(path).name}")
            if dest:
                prior.append(
                    ArtefactPointer(kind=ArtifactKind.SPECIFICATION, path=str(dest), revision=latest)
                )
        out = ctx.output_dir("refine-ticket")
        env = self.envelope(
            "refine-ticket",
            out,
            required=[ArtifactKind.SPECIFICATION],
            next_revision=nxt,
            prior=prior,
            source=self.source_refs(delivery_commit=await self.deps.repo.worktree_head(wt)),
        )
        result = await self.run_procedure("refine-ticket", wt, env)
        if (d := self.worker_decision(result)) is not None:
            return d
        require_artifact(result, out, ArtifactKind.SPECIFICATION, "specification.md")
        outcome = "clarification" if result.outcome is Outcome.NEEDS_CLARIFICATION else "success"
        extra: dict[str, Any] = {"revision": nxt}
        if outcome == "success" and ctx.cfg.flow.fast_track(ctx.ticket.issue.view.labels):
            extra.update(await self._fast_track_plan(result, out))
        return Decision(
            outcome=outcome,
            reason=result.summary,
            result=result.model_dump(mode="json"),
            extra=extra,
        )

    async def _fast_track_plan(self, result: StageResult, out: Path) -> dict[str, Any]:
        """Fast track: the plan Claude wrote with the specification, published with it as the next
        plan revision and approved by the specification's approval (see PlanningStage)."""
        if not (out / "plan.md").is_file() or result.footprint is None:
            return {
                "fast_track_skipped": "no plan was written with the specification, so planning runs as usual"
            }
        revs = await self.revisions("plan")
        return {
            "fast_track_plan": max([*revs, self.ctx.shared.plan_revision, 0]) + 1,
            "base": await self.deps.repo.remote_sha(self.ctx.cfg.repository.base_branch),
        }

    async def follow_up_decision(self, d: Decision, rev: int) -> Decision:
        """A specification edited in its open session: a fast-track plan goes out as the next plan
        revision with it (the earlier one keeps its number)."""
        d = await super().follow_up_decision(d, rev)
        if "fast_track_plan" not in d.extra:
            return d
        revs = await self.revisions("plan")
        nxt = max([*revs, self.ctx.shared.plan_revision, int(d.extra["fast_track_plan"]) - 1, 0]) + 1
        return d.model_copy(update={"extra": {**d.extra, "fast_track_plan": nxt}})

    async def publish(self, d: Decision) -> None:
        ctx = self.ctx
        if d.outcome == "blocked":
            await self.publish_block(d, Status.REFINING)
            return
        result = StageResult.model_validate(d.result)
        rev = int(d.extra["revision"])
        out = ctx.output_dir("refine-ticket")
        spec_rel = f"{ctx.doc_root}/specification/v{rev:03d}.md"
        extra = {
            "status": "draft with open questions" if d.outcome == "clarification" else "for review",
            "selected_comments": ",".join(ctx.record.selected_comment_ids) or "none",
        }
        if self.follow_up:
            extra["follow_up_of"] = self.follow_up.replaces
        header = provenance_header(ctx, "specification", f"v{rev:03d}", extra)
        files = {spec_rel: header + (out / "specification.md").read_text()}
        if self.follow_up is None:
            files[f"{ctx.doc_root}/executions/{ctx.run_id}.json"] = self.execution_summary(
                d, {"artefact": spec_rel, "questions": [q.id for q in result.questions]}
            )
        if result.proposed_tickets:
            files[proposals_path(ctx.doc_root, "specification", rev)] = dump_proposals(
                result.proposed_tickets
            )
        plan_rel = None
        if d.outcome == "success" and d.extra.get("fast_track_plan"):
            plan_rel = self._fast_track_files(
                files, result, rev, int(d.extra["fast_track_plan"]), d.extra.get("base")
            )
        sha = await self.publish_files(files, "spec", f"v{rev}", f"{ctx.key}: specification v{rev:03d}")
        url = blob_url(ctx.cfg.repository.url, sha, spec_rel)
        pub = ctx.publisher()
        artefacts = {
            k: v for k, v in ctx.shared.artefacts.items() if k not in ("fast_track_plan", "fast_track_spec")
        }
        artefacts["specification"] = f"{spec_rel}@{sha}"
        if plan_rel:
            artefacts.update({"fast_track_plan": f"{plan_rel}@{sha}", "fast_track_spec": f"v{rev}"})
        shared = ctx.shared.model_copy(
            update={
                "spec_revision": rev,
                "artefacts": artefacts,
                "current_run_id": ctx.run_id,
                "current_stage": self.stage,
                "updated_at": utcnow(),
            }
        )
        if d.outcome == "clarification":
            n = shared.clarification_rounds.get(self.stage.value, 0) + 1
            from delivery.feedback import round_token

            token = round_token(ctx.key, self.stage, n)
            pause = PauseInfo(
                kind="clarification",
                resume_stage=self.stage,
                round_token=token,
                question_ids=[q.id for q in result.questions],
                draft_path=spec_rel,
                draft_commit=sha,
                published_at=utcnow(),
            )
            ctx.shared = shared.model_copy(
                update={
                    "clarification_rounds": {**shared.clarification_rounds, self.stage.value: n},
                    "pause": pause,
                    "current_state": RunState.AWAITING_HUMAN,
                }
            )
            await self.announce(
                "questions",
                comments.questions(url, result.questions, self.stage.value),
                f"r{n}",
                pause=True,
            )
            await pub.save_record(ctx.key, ctx.shared, "questions")
            await pub.set_resume_field(ctx.key, self.stage.value, "questions")
            await pub.transition(ctx.key, Status.REFINING, Action.ASK_QUESTIONS)
            return
        token = gate_token(ctx.key, GateKind.SPEC, rev)
        gate = GateRecord(
            token=token,
            kind=GateKind.SPEC,
            ticket_key=ctx.key,
            revision=rev,
            artefact_path=spec_rel,
            artefact_commit=sha,
            published_at=utcnow(),
            approvers=ctx.cfg.approvals.jira_account_ids,
        )
        gates = supersede_for_new_revision(shared.gates, GateKind.SPEC, token) + [gate]
        ctx.shared = shared.model_copy(
            update={"gates": gates, "pause": None, "current_state": RunState.AWAITING_HUMAN}
        )
        await self.announce(
            "spec-gate",
            comments.spec_gate(
                token,
                url,
                rev,
                note=self.gate_note(),
                plan=(
                    int(d.extra["fast_track_plan"]),
                    blob_url(ctx.cfg.repository.url, sha, plan_rel),
                )
                if plan_rel
                else None,
                fast_track_note=str(d.extra.get("fast_track_skipped", "")),
                proposals=result.proposed_tickets,
            ),
            f"v{rev}",
            gate_tokens=(token,),
        )
        await pub.save_record(ctx.key, ctx.shared, "spec-gate")
        if self.follow_up:
            return
        SessionRegistry(ctx.cfg.runtime.state_dir).published(ctx.run_id, revision=rev)
        await pub.set_resume_field(ctx.key, None, "clear")
        await pub.transition(ctx.key, Status.REFINING, Action.COMPLETE_REFINEMENT)

    def _fast_track_files(
        self, files: dict[str, str], result: StageResult, rev: int, plan_rev: int, base: str | None
    ) -> str:
        """Add the fast-track plan and its footprint to ``files``; returns the plan's path."""
        ctx = self.ctx
        assert result.footprint is not None
        plan_rel = f"{ctx.doc_root}/plan/v{plan_rev:03d}.md"
        header = provenance_header(
            ctx, "plan", f"v{plan_rev:03d}", {"fast_track": f"written with specification v{rev:03d}"}
        )
        files[plan_rel] = header + (ctx.output_dir("refine-ticket") / "plan.md").read_text()
        fp = Footprint(
            ticket_key=ctx.key,
            owner_account_id=ctx.cfg.identity.developer_jira_account_id,
            stage=Stage.PLANNING,
            plan_revision=plan_rev,
            source_commit=base or "",
            published_at=utcnow(),
            **result.footprint.model_dump(),
        )
        files[plan_rel.removesuffix(".md") + ".footprint.json"] = (
            json.dumps(fp.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
        return plan_rel


# --------------------------------------------------------------------------- planning


class PlanningStage(StageStrategy):
    """The plan, or for a spike its findings (``investigate-ticket``, reviewed the same way). A
    fast-track ticket's plan was written and approved with its specification: it is published as
    approved, without a Claude session or a second review."""

    stage = Stage.PLANNING

    @property
    def spike(self) -> bool:
        return work_kind(self.ctx) == "spike"

    @property
    def procedure(self) -> str:
        return "investigate-ticket" if self.spike else "plan-ticket"

    @property
    def folder(self) -> str:
        return "findings" if self.spike else "plan"

    async def work(self) -> Decision:
        ctx = self.ctx
        if not self.spike and (fast := await self._fast_track()) is not None:
            return fast
        wt = (
            await self.detached_worktree("investigate", f"origin/{ctx.cfg.repository.base_branch}")
            if self.spike
            else await self.delivery_worktree()
        )
        spec = await self.approved_input(GateKind.SPEC, ArtifactKind.SPECIFICATION)
        if spec is None:
            return Decision(
                outcome="blocked",
                reason="approved specification could not be read",
                action="Check the delivery branch and resume.",
                blocker_kind="missing_input",
            )
        revs = await self.revisions(self.folder)
        nxt = max([*revs, ctx.shared.plan_revision, 0]) + 1
        prior = []
        if revs:
            path = f"{ctx.doc_root}/{self.folder}/v{max(revs):03d}.md"
            dest = await self.copy_input(f"origin/{ctx.delivery_branch}", path, f"prior/{Path(path).name}")
            if dest:
                prior.append(ArtefactPointer(kind=ArtifactKind.PLAN, path=str(dest), revision=max(revs)))
        base = await self.deps.repo.remote_sha(ctx.cfg.repository.base_branch)
        related = await self.related_work()
        out = ctx.output_dir(self.procedure)
        ports = None
        if self.spike:
            ports = ctx.record.ports or self.deps.ports.allocate(ctx.run_id)
            ctx.record = ctx.record.model_copy(update={"ports": ports})
        env = self.envelope(
            self.procedure,
            out,
            required=[ArtifactKind.PLAN],
            next_revision=nxt,
            approved=[spec],
            prior=prior,
            related=related,
            ports=ports,
            source=self.source_refs(base_commit=base),
        )
        result = await self.run_procedure(self.procedure, wt, env, ports=ports)
        if (d := self.worker_decision(result)) is not None:
            return d
        require_artifact(result, out, ArtifactKind.PLAN, f"{self.folder}.md")
        if not self.spike and result.outcome is Outcome.COMPLETED and result.footprint is None:
            raise WorkerFailure("plan-ticket", None, "completed plan has no change footprint")
        for adr in [a for a in result.artifacts if a.kind is ArtifactKind.ARCHITECTURE]:
            safe_output_file(out, adr.path)
        outcome = "clarification" if result.outcome is Outcome.NEEDS_CLARIFICATION else "success"
        extra: dict[str, Any] = {"revision": nxt, "base": base}
        if self.spike:
            extra["findings"] = True
        elif result.footprint:
            extra.update(await self.footprint(result, nxt, base))
        return Decision(
            outcome=outcome,
            reason=result.summary,
            result=result.model_dump(mode="json"),
            extra=extra,
        )

    async def footprint(self, result: StageResult, rev: int, base: str | None) -> dict[str, Any]:
        """The plan's change footprint for revision ``rev`` and its overlap with other work."""
        assert result.footprint is not None
        fp = Footprint(
            ticket_key=self.ctx.key,
            owner_account_id=self.ctx.cfg.identity.developer_jira_account_id,
            stage=self.stage,
            plan_revision=rev,
            source_commit=base or "",
            published_at=utcnow(),
            **result.footprint.model_dump(),
        )
        from delivery.coordination import Coordinator

        findings = await Coordinator(self.deps).check(fp, self.ctx.shared, checkpoint="plan")
        return {"footprint": fp.model_dump(mode="json"), "overlap": findings_json(findings)}

    async def follow_up_decision(self, d: Decision, rev: int) -> Decision:
        """The edited plan keeps its footprint unless the session rewrote the result file with a
        new one; either way it is checked for overlap again as revision ``rev``."""
        d = await super().follow_up_decision(d, rev)
        result = StageResult.model_validate(d.result)
        try:
            rewritten = StageResult.model_validate_json(
                (self.ctx.output_dir(self.procedure) / RESULT_FILE).read_text()
            )
        except (OSError, ValueError):
            rewritten = None
        if rewritten is not None and rewritten.footprint is not None:
            result = result.model_copy(update={"footprint": rewritten.footprint})
        if result.footprint is None:
            return d
        extra = {**d.extra, **await self.footprint(result, rev, d.extra.get("base"))}
        return d.model_copy(update={"result": result.model_dump(mode="json"), "extra": extra})

    async def related_work(self) -> list[OverlapContext]:
        from delivery.coordination import Coordinator

        return await Coordinator(self.deps).related_contexts(self.ctx.key)

    async def publish(self, d: Decision) -> None:
        ctx = self.ctx
        if d.outcome == "blocked":
            await self.publish_block(d, Status.PLANNING)
            return
        if d.outcome == "fast_track":
            await self._publish_fast_track(d)
            return
        result = StageResult.model_validate(d.result)
        rev = int(d.extra["revision"])
        out = ctx.output_dir(self.procedure)
        plan_rel = f"{ctx.doc_root}/{self.folder}/v{rev:03d}.md"
        fp_rel = f"{ctx.doc_root}/plan/v{rev:03d}.footprint.json"
        spec_gate = current_gate(ctx.shared.gates, GateKind.SPEC)
        extra = {
            "approved_specification": spec_gate.token if spec_gate else "none",
            "source_commit": d.extra.get("base"),
        }
        if self.follow_up:
            extra["follow_up_of"] = self.follow_up.replaces
        header = provenance_header(ctx, self.folder, f"v{rev:03d}", extra)
        files = {plan_rel: header + (out / f"{self.folder}.md").read_text()}
        if self.spike and result.proposed_tickets:
            files[proposals_path(ctx.doc_root, "findings", rev)] = dump_proposals(result.proposed_tickets)
        for i, adr in enumerate([a for a in result.artifacts if a.kind is ArtifactKind.ARCHITECTURE], 1):
            files[f"{ctx.doc_root}/architecture/adr-{rev:03d}-{i}.md"] = (
                provenance_header(ctx, "architecture", f"v{rev:03d}", {}) + (out / adr.path).read_text()
            )
        if d.extra.get("footprint"):
            files[fp_rel] = json.dumps(d.extra["footprint"], indent=2, sort_keys=True) + "\n"
        if self.follow_up is None:
            files[f"{ctx.doc_root}/executions/{ctx.run_id}.json"] = self.execution_summary(
                d, {"artefact": plan_rel, "overlap": d.extra.get("overlap", [])}
            )
        sha = await self.publish_files(files, "plan", f"v{rev}", f"{ctx.key}: {self.folder} v{rev:03d}")
        url = blob_url(ctx.cfg.repository.url, sha, plan_rel)
        pub = ctx.publisher()
        shared = ctx.shared.model_copy(
            update={
                "plan_revision": rev,
                "artefacts": {**ctx.shared.artefacts, "plan": f"{plan_rel}@{sha}"},
                "current_run_id": ctx.run_id,
                "current_stage": self.stage,
                "updated_at": utcnow(),
            }
        )
        if d.extra.get("footprint"):
            shared = shared.model_copy(
                update={
                    "footprint_ref": {
                        "path": fp_rel,
                        "commit": sha,
                        "revision": rev,
                        "actual_paths": [],
                        "candidate_sha": None,
                    }
                }
            )
        if d.outcome == "clarification":
            n = shared.clarification_rounds.get(self.stage.value, 0) + 1
            from delivery.feedback import round_token

            token = round_token(ctx.key, self.stage, n)
            ctx.shared = shared.model_copy(
                update={
                    "clarification_rounds": {**shared.clarification_rounds, self.stage.value: n},
                    "pause": PauseInfo(
                        kind="clarification",
                        resume_stage=self.stage,
                        round_token=token,
                        question_ids=[q.id for q in result.questions],
                        draft_path=plan_rel,
                        draft_commit=sha,
                        published_at=utcnow(),
                    ),
                    "current_state": RunState.AWAITING_HUMAN,
                }
            )
            await self.announce(
                "questions",
                comments.questions(url, result.questions, self.stage.value),
                f"r{n}",
                pause=True,
            )
            await pub.save_record(ctx.key, ctx.shared, "questions")
            await pub.set_resume_field(ctx.key, self.stage.value, "questions")
            await pub.transition(ctx.key, Status.PLANNING, Action.ASK_QUESTIONS)
            return
        token = gate_token(ctx.key, GateKind.PLAN, rev)
        gate = GateRecord(
            token=token,
            kind=GateKind.PLAN,
            ticket_key=ctx.key,
            revision=rev,
            artefact_path=plan_rel,
            artefact_commit=sha,
            published_at=utcnow(),
            approvers=ctx.cfg.approvals.jira_account_ids,
        )
        gates = supersede_for_new_revision(shared.gates, GateKind.PLAN, token) + [gate]
        ctx.shared = shared.model_copy(
            update={"gates": gates, "pause": None, "current_state": RunState.AWAITING_HUMAN}
        )
        overlap = [_finding(o) for o in d.extra.get("overlap", [])]
        from delivery.coordination import Coordinator

        await Coordinator(self.deps).publish_warnings(ctx, overlap)
        await self.announce(
            "plan-gate",
            comments.findings_gate(
                token,
                url,
                rev,
                result.proposed_tickets,
                note=self.gate_note(),
            )
            if self.spike
            else comments.plan_gate(
                url,
                blob_url(ctx.cfg.repository.url, sha, fp_rel),
                rev,
                overlap,
                self.gate_note(),
            ),
            f"v{rev}",
            gate_tokens=(token,),
        )
        await pub.save_record(ctx.key, ctx.shared, "plan-gate")
        if self.follow_up:
            return
        SessionRegistry(ctx.cfg.runtime.state_dir).published(ctx.run_id, revision=rev)
        await pub.set_resume_field(ctx.key, None, "clear")
        await pub.transition(ctx.key, Status.PLANNING, Action.COMPLETE_PLANNING)

    # ------------------------------------------------------------------ fast track
    async def _fast_track(self) -> Decision | None:
        """The plan written with the approved specification, when the ticket takes the fast track."""
        ctx = self.ctx
        spec = approved_gate(ctx.shared.gates, GateKind.SPEC)
        ref = ctx.shared.artefacts.get("fast_track_plan")
        if spec is None or not ref or ctx.shared.artefacts.get("fast_track_spec") != f"v{spec.revision}":
            return None
        if not ctx.cfg.flow.fast_track(ctx.ticket.issue.view.labels):
            return None  # the label was removed: plan as usual
        if ctx.intake.requirement is not Requirement.SPEC_APPROVAL:
            return None  # plan changes were asked for, or planning resumes: plan as usual
        plan_rel, commit = ref.rsplit("@", 1)
        m = re.search(r"/v(\d{3,4})\.md$", plan_rel)
        raw = await self.deps.repo.show_file(commit, plan_rel.removesuffix(".md") + ".footprint.json")
        if m is None or raw is None:
            return None
        from delivery.coordination import Coordinator

        fp = Footprint.model_validate_json(raw)
        overlap = await Coordinator(self.deps).check(fp, ctx.shared, checkpoint="plan")
        name = ctx.cfg.workflow.action_name(Action.USE_APPROVED_PLAN)
        target = ctx.cfg.status_id(Status.READY_DEVELOPMENT)
        offered = any(
            t.name == name and t.to_status_id == target for t in await self.deps.jira.transitions(ctx.key)
        )
        return Decision(
            outcome="fast_track",
            reason=f"plan v{int(m.group(1)):03d} was approved with specification v{spec.revision:03d}",
            extra={
                "revision": int(m.group(1)),
                "plan": ref,
                "overlap": findings_json(overlap),
                "route": "fast" if offered else "review",
                "spec_token": spec.token,
            },
        )

    async def _publish_fast_track(self, d: Decision) -> None:
        ctx = self.ctx
        rev = int(d.extra["revision"])
        plan_rel, sha = str(d.extra["plan"]).rsplit("@", 1)
        fp_rel = plan_rel.removesuffix(".md") + ".footprint.json"
        fast = d.extra.get("route") == "fast"
        spec = approved_gate(ctx.shared.gates, GateKind.SPEC)
        token = gate_token(ctx.key, GateKind.PLAN, rev)
        gate = GateRecord(
            token=token,
            kind=GateKind.PLAN,
            ticket_key=ctx.key,
            revision=rev,
            artefact_path=plan_rel,
            artefact_commit=sha,
            published_at=utcnow(),
            approvers=ctx.cfg.approvals.jira_account_ids,
            state=GateState.APPROVED if fast else GateState.PENDING,
            decided_at=spec.decided_at if fast and spec else None,
            evidence=spec.evidence if fast and spec else None,
        )
        ctx.shared = ctx.shared.model_copy(
            update={
                "plan_revision": rev,
                "artefacts": {**ctx.shared.artefacts, "plan": f"{plan_rel}@{sha}"},
                "footprint_ref": {
                    "path": fp_rel,
                    "commit": sha,
                    "revision": rev,
                    "actual_paths": [],
                    "candidate_sha": None,
                },
                "gates": supersede_for_new_revision(ctx.shared.gates, GateKind.PLAN, token) + [gate],
                "pause": None,
                "current_run_id": ctx.run_id,
                "current_stage": self.stage,
                "current_state": RunState.COMPLETED if fast else RunState.AWAITING_HUMAN,
                "updated_at": utcnow(),
            }
        )
        overlap = [_finding(o) for o in d.extra.get("overlap", [])]
        from delivery.coordination import Coordinator

        await Coordinator(self.deps).publish_warnings(ctx, overlap)
        url = blob_url(ctx.cfg.repository.url, sha, plan_rel)
        pub = ctx.publisher()
        if fast:
            await self.announce(
                "fast-track",
                comments.fast_track_plan(url, rev),
                f"v{rev}",
                gate_tokens=(token,),
            )
            await pub.save_record(ctx.key, ctx.shared, "fast-track")
            await pub.set_resume_field(ctx.key, None, "clear")
            await pub.transition(ctx.key, Status.PLANNING, Action.USE_APPROVED_PLAN)
            return
        note = (
            "This plan was written with the specification (fast track), but this Jira workflow has no "
            f"**{ctx.cfg.workflow.action_name(Action.USE_APPROVED_PLAN)}** transition (Planning to Ready "
            "for development), so it needs its own approval."
        )
        await self.announce(
            "plan-gate",
            comments.plan_gate(
                url,
                blob_url(ctx.cfg.repository.url, sha, fp_rel),
                rev,
                overlap,
                note,
            ),
            f"v{rev}",
            gate_tokens=(token,),
        )
        await pub.save_record(ctx.key, ctx.shared, "plan-gate")
        await pub.set_resume_field(ctx.key, None, "clear")
        await pub.transition(ctx.key, Status.PLANNING, Action.COMPLETE_PLANNING)


def _finding(raw: dict[str, Any]) -> OverlapFinding:
    from delivery.overlap import OverlapKind

    return OverlapFinding(
        warning_id=raw["warning_id"],
        kind=OverlapKind(raw["kind"]),
        severity=OverlapSeverity(raw["severity"]),
        ticket=raw["ticket"],
        other=raw["other"],
        other_assignee=raw.get("other_assignee"),
        details=tuple(raw.get("details", ())),
        revisions=raw.get("revisions", {}),
    )


def findings_json(findings: list[OverlapFinding]) -> list[dict[str, Any]]:
    return [
        f.__dict__ | {"kind": f.kind.value, "severity": f.severity.value, "details": list(f.details)}
        for f in findings
    ]


# --------------------------------------------------------------------------- development


def _ref(p: ArtefactPointer) -> str:
    """Identity of an approved document, independent of where a run keeps its copy."""
    return f"{p.kind.value}:v{p.revision}:{p.commit or ''}"


class DevelopmentStage(StageStrategy):
    stage = Stage.DEVELOPMENT

    async def preflight(self) -> Decision | None:
        """Overlap checkpoint 2: immediately before implementation begins. Flags, never pauses."""
        from delivery.coordination import Coordinator

        coord = Coordinator(self.deps)
        fp = await coord.own_footprint(self.ctx.shared)
        if fp is not None:
            await coord.publish_warnings(
                self.ctx, await coord.check(fp, self.ctx.shared, checkpoint="pre-implementation")
            )
        return None

    async def work(self) -> Decision:
        ctx = self.ctx
        repo = self.deps.repo
        base = ctx.cfg.repository.base_branch
        wt = ctx.worktree_path("feature")
        fresh = not wt.exists()
        if fresh:
            exists = await repo.remote_sha(ctx.feature_branch)
            start = f"origin/{ctx.feature_branch}" if exists else f"origin/{base}"
            await repo.add_worktree(wt, start=start, branch=ctx.feature_branch)
            ctx.record.worktrees["feature"] = str(wt)
            base_sha = await repo.remote_sha(base)
            if exists and base_sha and not await repo.is_ancestor(base_sha, await repo.worktree_head(wt)):
                merged = await repo.merge(wt, f"origin/{base}", f"{ctx.key}: merge {base} into candidate")
                if not merged.ok and not await self._resolve_conflicts(wt, base_sha, list(merged.conflicts)):
                    # Resolved when the PR is merged; development carries on without it.
                    ctx.record.outputs["merge_conflicts"] = [
                        {"with": base, "sha": base_sha, "paths": list(merged.conflicts)}
                    ]
                    ctx.save("base_merge_conflicts", paths=list(merged.conflicts))
        start_sha = ctx.record.outputs.get("start_sha") or await repo.worktree_head(wt)
        ctx.record.outputs["start_sha"] = start_sha
        ctx.save("worktree_ready", start_sha=start_sha)
        spec = await self.spec_input()
        plan = await self.approved_input(GateKind.PLAN, ArtifactKind.PLAN)
        if spec is None or plan is None:
            return Decision(
                outcome="blocked",
                reason="approved specification or plan could not be read",
                action="Check the delivery branch and resume.",
                blocker_kind="missing_input",
            )
        feedback = self._carried_feedback()
        if feedback:
            (ctx.inputs_dir / "feedback.json").write_text(json.dumps(feedback, indent=2, sort_keys=True))
            ctx.record.outputs["feedback_items"] = feedback
        ports = ctx.record.ports or self.deps.ports.allocate(ctx.run_id)
        ctx.record = ctx.record.model_copy(update={"ports": ports})
        if fresh:
            prior = await self._continue_unfinished(wt, start_sha, spec, plan)
        else:
            prior = await self._resumed_in_place(wt, start_sha)
        out = ctx.output_dir("implement-ticket")
        env = self.envelope(
            "implement-ticket",
            out,
            required=[],
            approved=[spec, plan],
            ports=ports,
            source=self.source_refs(feature_commit=start_sha),
            write_globs=[f"{wt}/**", f"{out}/**"],
            prior_work=prior,
            feedback=feedback,
        )
        try:
            result = await self.run_procedure("implement-ticket", wt, env, ports=ports)
        except WorkerFailure:
            # Out of turns or time, a provider limit: keep the unfinished changes for Resume.
            await self._save_unfinished(wt, start_sha, spec, plan)
            raise
        if (d := self.worker_decision(result)) is not None:
            await self._save_unfinished(wt, start_sha, spec, plan)
            return d
        head = await repo.worktree_head(wt)
        if head != start_sha:
            # The worker committed locally; the coordinator re-commits everything itself.
            await repo.git("reset", "--soft", start_sha, cwd=wt)
            ctx.journal.events.append("worker_commits_folded", {"head": head})
        changed = await repo.changed_paths(wt, start_sha)
        protected = [p for p in changed if is_protected(p)]
        if protected:
            return Decision(
                outcome="blocked",
                reason=f"implementation changed protected paths: {protected}",
                action="Protected policy files cannot change in a feature ticket; "
                "raise a separate human-owned change, then resume.",
                blocker_kind="protected_paths",
                result=result.model_dump(mode="json"),
            )
        if result.outcome is Outcome.NEEDS_CLARIFICATION:
            return Decision(
                outcome="clarification",
                reason=result.summary,
                result=result.model_dump(mode="json"),
                extra={"changed": changed},
            )
        if not changed and ctx.shared.candidate_sha and start_sha != ctx.shared.candidate_sha:
            # Nothing more to change, but the branch moved on from the recorded candidate (the
            # latest base merged in, or a conflict resolved by hand): that is the next candidate.
            return Decision(
                outcome="success",
                reason=result.summary,
                result=result.model_dump(mode="json"),
                extra={"changed": [], "start_sha": start_sha},
            )
        if not changed:
            return Decision(
                outcome="blocked",
                reason="the implementation produced no changes",
                action="Check the plan and feedback, then resume.",
                blocker_kind="no_changes",
                result=result.model_dump(mode="json"),
            )
        return Decision(
            outcome="success",
            reason=result.summary,
            result=result.model_dump(mode="json"),
            extra={"changed": changed, "start_sha": start_sha},
        )

    async def _resolve_conflicts(self, wt: Path, base_sha: str, paths: list[str]) -> bool:
        """Merge the base again, leaving its conflicts for a short Claude session to resolve.
        True when the merge was resolved and committed; otherwise the merge is abandoned (the
        branch is as it was) and the conflict is flagged to resolve when merging, as before."""
        ctx, repo = self.ctx, self.deps.repo
        base = ctx.cfg.repository.base_branch
        if not ctx.cfg.flow.resolve_conflicts:
            return False
        head = await repo.worktree_head(wt)
        message = f"{ctx.key}: merge {base} into candidate"
        res = await repo.git(
            "merge", "--no-ff", "--no-edit", "-m", message, f"origin/{base}", cwd=wt, check=False
        )
        if res.returncode == 0:
            return True  # merged cleanly this time (the base moved on meanwhile)
        approved = [
            a
            for a in (
                await self.approved_input(GateKind.SPEC, ArtifactKind.SPECIFICATION),
                await self.approved_input(GateKind.PLAN, ArtifactKind.PLAN),
            )
            if a
        ]
        ports = ctx.record.ports or self.deps.ports.allocate(ctx.run_id)
        ctx.record = ctx.record.model_copy(update={"ports": ports})
        out = ctx.output_dir("resolve-conflicts")
        item = (
            f"Merging the latest {base} ({base_sha[:12]}) into this branch conflicts in: {', '.join(paths)}. "
            "Resolve every conflict, keeping the intent of both sides, and change nothing else."
        )
        env = self.envelope(
            "resolve-conflicts",
            out,
            required=[],
            approved=approved,
            ports=ports,
            source=self.source_refs(base_commit=base_sha, feature_commit=head),
            write_globs=[f"{wt}/**", f"{out}/**"],
            feedback={"R1": item},
        )
        ctx.save("resolving_conflicts", paths=paths)
        try:
            result = await self.run_procedure("resolve-conflicts", wt, env, ports=ports)
        except WorkerFailure as exc:
            await repo.git("merge", "--abort", cwd=wt, check=False)
            if exc.blocker_kind.startswith("provider_"):
                raise  # Claude cannot be used: the run waits for it (with the branch as it was)
            ctx.journal.events.append("conflicts_not_resolved", {"reason": exc.detail[:300]})
            return False
        # Still unmerged in the index until staged: a file is resolved when its markers are gone.
        unmerged = await repo.git("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False)
        conflicted = set(paths) | {n for n in unmerged.stdout.splitlines() if n}
        left = sorted(p for p in conflicted if _has_markers(wt / p))
        if result.outcome is not Outcome.COMPLETED or left:
            await repo.git("merge", "--abort", cwd=wt, check=False)
            ctx.journal.events.append(
                "conflicts_not_resolved",
                {"outcome": result.outcome.value, "left": left, "reason": result.blocker_reason},
            )
            return False
        await repo.git("add", "-A", cwd=wt)
        await repo.git("commit", "--no-verify", "-q", "--no-edit", cwd=wt)
        ctx.record.outputs["conflicts_resolved"] = {"with": base, "sha": base_sha, "paths": paths}
        ctx.save("conflicts_resolved", paths=paths)
        return True

    def _carried_feedback(self) -> dict[str, str]:
        """This run's change items or, when it resumes a blocked development run, that run's:
        a blocker (such as a merge conflict resolved by hand) must not drop what was asked."""
        ctx = self.ctx
        if ctx.intake.feedback_items:
            return dict(ctx.intake.feedback_items)
        for e in reversed(self.deps.store.runs_for_ticket(ctx.key)):
            rec = e.record
            if e.run_id == ctx.run_id or rec is None or rec.stage is not Stage.DEVELOPMENT:
                continue
            if rec.state is not RunState.BLOCKED:
                return {}
            items = rec.outputs.get("feedback_items") or (rec.outputs.get("intake") or {}).get(
                "feedback_items"
            )
            if items:
                return dict(items)
        return {}

    async def _continue_unfinished(
        self, wt: Path, start_sha: str, spec: ArtefactPointer, plan: ArtefactPointer
    ) -> PriorWork | None:
        """Apply the latest unfinished changes from an earlier run of this ticket, if they were
        made from the same starting commit, specification and plan."""
        ctx = self.ctx
        for e in reversed(self.deps.store.runs_for_ticket(ctx.key)):
            rec = e.record
            if (
                e.run_id == ctx.run_id
                or rec is None
                or rec.stage not in (Stage.DEVELOPMENT, Stage.RESOLUTION)
            ):
                continue
            meta = rec.outputs.get("wip")
            patch = e.journal.dir / "wip" / "changes.patch"
            if not meta or not patch.exists():
                continue
            if (meta.get("start_sha"), meta.get("spec"), meta.get("plan")) != (
                start_sha,
                _ref(spec),
                _ref(plan),
            ):
                return None  # the inputs moved on; start from the approved plan again
            applied = await self.deps.repo.git(
                "apply", "--index", "--binary", str(patch), cwd=wt, check=False
            )
            if applied.returncode != 0:
                ctx.journal.events.append(
                    "wip_not_applied", {"from": e.run_id, "error": applied.stderr[:300]}
                )
                return None
            tail_path = self._session_tail(e.journal.dir)
            ctx.record.outputs["continued_from"] = e.run_id
            ctx.save("wip_applied", source=e.run_id, files=len(meta.get("files", [])))
            return PriorWork(run_id=e.run_id, files=list(meta.get("files", [])), session_tail_path=tail_path)
        return None

    async def _resumed_in_place(self, wt: Path, start_sha: str) -> PriorWork | None:
        """This run resumes in the worktree it already had (after a restart, or after waiting
        for Claude): tell the new session about the changes the last one left there."""
        ctx = self.ctx
        files = await self.deps.repo.changed_paths(wt, start_sha)
        if not files:
            return None
        tail_path = self._session_tail(ctx.journal.dir)
        ctx.save("resumed_in_place", files=len(files))
        return PriorWork(run_id=ctx.run_id, files=files, session_tail_path=tail_path)

    def _session_tail(self, journal_dir: Path) -> str | None:
        """The last steps of an implementation session, for the session that continues it."""
        log = journal_dir / "logs" / "claude-implement-ticket.jsonl"
        if not log.exists():
            return None
        from delivery.transcript import render_file

        tail = [ln for ln in render_file(log) if ln.strip()][-40:]
        tail_file = self.ctx.inputs_dir / "prior-session-tail.txt"
        tail_file.write_text("\n".join(tail) + "\n")
        return str(tail_file)

    async def publish(self, d: Decision) -> None:
        ctx = self.ctx
        pub = ctx.publisher()
        if d.outcome == "blocked":
            await self.publish_block(d, Status.DEVELOPING)
            return
        result = StageResult.model_validate(d.result)
        if d.outcome == "clarification":
            n = ctx.shared.clarification_rounds.get(self.stage.value, 0) + 1
            from delivery.feedback import round_token

            token = round_token(ctx.key, self.stage, n)
            ctx.shared = ctx.shared.model_copy(
                update={
                    "clarification_rounds": {
                        **ctx.shared.clarification_rounds,
                        self.stage.value: n,
                    },
                    "pause": PauseInfo(
                        kind="clarification",
                        resume_stage=self.stage,
                        round_token=token,
                        question_ids=[q.id for q in result.questions],
                        published_at=utcnow(),
                    ),
                    "current_state": RunState.AWAITING_HUMAN,
                    "current_run_id": ctx.run_id,
                    "current_stage": self.stage,
                }
            )
            pr_url = ctx.record.pr_url or ctx.cfg.repository.url
            await self.announce(
                "questions",
                comments.questions(pr_url, result.questions, self.stage.value),
                f"r{n}",
                pause=True,
            )
            await pub.save_record(ctx.key, ctx.shared, "questions")
            await pub.set_resume_field(ctx.key, self.stage.value, "questions")
            await pub.transition(ctx.key, Status.DEVELOPING, Action.ASK_QUESTIONS)
            return
        wt = Path(ctx.record.worktrees["feature"])
        n = int(d.extra.get("candidate_number") or ctx.shared.candidate_number + 1)
        d.extra["candidate_number"] = n
        ctx.record.outputs["decision"] = d.model_dump(mode="json")
        ctx.save("candidate_number", candidate=n)
        sha = await pub.commit_and_push(
            wt,
            ctx.feature_branch,
            "feature",
            f"{ctx.key}: {ctx.ticket.issue.view.summary}\n\n{result.summary}",
            revision=f"c{n}",
        )
        if sha is None:
            # No new edits: publish the branch as it is (for example with the latest base merged).
            sha = await pub.push_head(wt, ctx.feature_branch, "feature-head", revision=f"c{n}")
        spec_gate = current_gate(ctx.shared.gates, GateKind.SPEC)
        plan_gate = current_gate(ctx.shared.gates, GateKind.PLAN)
        body = "\n".join(
            [
                f"Implements {ctx.key}: {ctx.ticket.issue.view.summary}",
                "",
                f"- Approved specification: {spec_gate.token if spec_gate else '-'}"
                + (
                    " ("
                    + blob_url(ctx.cfg.repository.url, spec_gate.artefact_commit, spec_gate.artefact_path)
                    + ")"
                    if spec_gate and spec_gate.artefact_commit and spec_gate.artefact_path
                    else ""
                ),
                f"- Approved plan: {plan_gate.token if plan_gate else '-'}",
                f"- Candidate: c{n} `{sha}`",
                "",
                result.summary,
                "",
                "Opened by the delivery coordinator. Humans review, approve and merge.",
            ]
        )
        pr = await pub.ensure_pr(
            ctx.feature_branch,
            ctx.cfg.repository.base_branch,
            f"{ctx.key}: {ctx.ticket.issue.view.summary}",
            body,
            revision=f"c{n}",
        )
        ctx.record = ctx.record.model_copy(
            update={"candidate_sha": sha, "pr_number": pr.number, "pr_url": pr.url}
        )
        ctx.save("candidate_published", sha=sha, pr=pr.number)
        SessionRegistry(ctx.cfg.runtime.state_dir).published(ctx.run_id, sha=sha, candidate_number=n)
        changed = list(d.extra.get("changed", []))
        files = {
            f"{ctx.doc_root}/executions/{ctx.run_id}.json": self.execution_summary(
                d,
                {
                    "candidate_number": n,
                    "candidate_sha": sha,
                    "pr": pr.url,
                    "changed_paths": changed,
                    "evidence": [e.model_dump(mode="json") for e in result.evidence],
                    "worker_checks": [w.model_dump(mode="json") for w in result.worker_checks],
                },
            )
        }
        await self.publish_files(files, "execution", f"c{n}", f"{ctx.key}: candidate c{n} record")
        code_token = gate_token(ctx.key, GateKind.CODE, n)
        fp_ref = dict(ctx.shared.footprint_ref or {})
        # A later candidate's edits add to the earlier ones; they never replace them.
        actual = sorted(set(fp_ref.get("actual_paths") or []) | set(changed))
        fp_ref.update({"actual_paths": actual[:200], "candidate_sha": sha})
        ctx.shared = ctx.shared.model_copy(
            update={
                "candidate_number": n,
                "candidate_sha": sha,
                "pr_number": pr.number,
                "gates": supersede_for_new_revision(ctx.shared.gates, GateKind.CODE, code_token),
                "pending_feedback": [],
                "footprint_ref": fp_ref,
                "pause": None,
                "current_run_id": ctx.run_id,
                "current_stage": self.stage,
                "current_state": RunState.COMPLETED,
                "updated_at": utcnow(),
            }
        )
        from delivery.coordination import Coordinator

        coord = Coordinator(self.deps)
        fp = await coord.own_footprint(ctx.shared)
        if fp is not None:
            fp = fp.model_copy(update={"actual_paths": changed, "actual_commit": sha})
            await coord.publish_warnings(
                ctx, await coord.check(fp, ctx.shared, checkpoint="changed-paths", use_actual=True)
            )
        await pub.save_record(ctx.key, ctx.shared, "candidate")
        await pub.comment(
            ctx.key,
            "candidate",
            comments.candidate_ready(
                n,
                sha,
                pr.url,
                merge_conflicts=ctx.record.outputs.get("merge_conflicts", []),
                resolved=ctx.record.outputs.get("conflicts_resolved"),
                base=ctx.cfg.repository.base_branch,
                session_open=SessionRegistry(ctx.cfg.runtime.state_dir).development(ctx.key) is not None,
            ),
            f"c{n}",
        )
        await pub.transition(ctx.key, Status.DEVELOPING, Action.COMPLETE_DEVELOPMENT)


# --------------------------------------------------------------------------- verification


class VerificationStage(StageStrategy):
    stage = Stage.VERIFICATION

    async def work(self) -> Decision:
        ctx = self.ctx
        repo = self.deps.repo
        await repo.fetch()
        candidate = ctx.shared.candidate_sha
        assert candidate
        remote = await repo.remote_sha(ctx.feature_branch)
        if remote != candidate:
            return Decision(
                outcome="blocked",
                reason=f"feature branch head {str(remote)[:12]} is not the recorded candidate "
                f"{candidate[:12]}; a changed candidate needs development to record it",
                action="Use Submit implementation changes via Changes requested, or resume once fixed.",
                blocker_kind="candidate_changed",
                resume_stage=Stage.DEVELOPMENT.value,
            )
        base = ctx.cfg.repository.base_branch
        base_sha = await repo.remote_sha(base) or ""
        spec = await self.spec_input()
        plan = await self.approved_input(GateKind.PLAN, ArtifactKind.PLAN)
        approved = [a for a in (spec, plan) if a]
        diff = await repo.git("diff", f"{base_sha}...{candidate}")
        (ctx.inputs_dir / "candidate.diff").write_text(diff.stdout)
        # What the developer asked for in the open session is in the follow-up commit messages:
        # the reviewer uses it to tell intended deviations from the specification apart.
        log = await repo.git(
            "log",
            "--format=%h %s%n%n%b%n----",
            f"{base_sha}..{candidate}" if base_sha else candidate,
            check=False,
        )
        (ctx.inputs_dir / "candidate-commits.txt").write_text(log.stdout)
        from delivery.coordination import Coordinator

        coord = Coordinator(self.deps)
        related = await coord.related_contexts(ctx.key)
        interacting = await coord.interacting_candidates(ctx.key, ctx.shared)

        # 1. Fresh independent review (read-only, no author session).
        review_wt = await self.detached_worktree("review", candidate)
        out_r = ctx.output_dir("review-ticket")
        env = self.envelope(
            "review-ticket",
            out_r,
            required=[ArtifactKind.REVIEW],
            approved=approved,
            related=related,
            source=self.source_refs(base_commit=base_sha, candidate_sha=candidate),
        )
        review = await self.run_procedure("review-ticket", review_wt, env)
        if (d := self.worker_decision(review)) is not None:
            return d
        require_artifact(review, out_r, ArtifactKind.REVIEW, "review.md")
        if await repo.tracked_changes(review_wt):
            return Decision(
                outcome="blocked",
                reason="reviewer modified the frozen candidate worktree",
                action="Inspect the run; resume to re-run verification.",
                blocker_kind="reviewer_modified_candidate",
            )

        # 2. Coordinator-run checks: candidate tree and integration tree, separately.
        ports = ctx.record.ports or self.deps.ports.allocate(ctx.run_id)
        ctx.record = ctx.record.model_copy(update={"ports": ports})
        verify_wt = await self.detached_worktree("verify", candidate)
        names = list(ctx.cfg.checks.commands)
        checks = await self.setup_and_check(
            verify_wt, "candidate", candidate, ports, names, base_sha=base_sha
        )
        integration_wt = await self.detached_worktree("integration", candidate)
        merged_with = []
        # Textual conflicts are flagged, never a failure: they are resolved when the PR is
        # merged. Failing on them would also deadlock two tickets that conflict with each other.
        # The integration tree is tested without whatever conflicts.
        conflicts: list[dict[str, Any]] = []
        res = await repo.merge(integration_wt, base_sha, f"integration: {base} into {candidate[:12]}")
        if not res.ok:
            conflicts.append({"with": base, "sha": base_sha, "paths": list(res.conflicts)})
        for other_key, other_sha in interacting:
            r2 = await repo.merge(integration_wt, other_sha, f"integration: {other_key}")
            if r2.ok:
                merged_with.append(f"{other_key}@{other_sha[:12]}")
            else:
                conflicts.append({"with": other_key, "sha": other_sha, "paths": list(r2.conflicts)})
        tree = (await repo.git("rev-parse", "HEAD^{tree}", cwd=integration_wt)).stdout.strip()
        int_head = await repo.worktree_head(integration_wt)
        integ = await self.setup_and_check(
            integration_wt,
            "integration",
            int_head,
            ports,
            ctx.cfg.checks.integration_names,
            tree_sha=tree,
            base_sha=base_sha,
        )
        all_checks = checks + integ
        ctx.record = ctx.record.model_copy(update={"checks": all_checks})
        ctx.save("coordinator_checks", passed=all_passed(all_checks))
        atomic_write_json(
            ctx.inputs_dir / "coordinator_checks.json",
            [c.model_dump(mode="json") for c in all_checks],
        )
        share_check_logs(ctx, all_checks)
        if await repo.tracked_changes(verify_wt):
            await repo.git("checkout", "--", ".", cwd=verify_wt)
        # A bug's regression tests should fail on the base branch without the fix. Reported for
        # the reviewers, never a reason to fail verification.
        reproduction = None
        if ctx.cfg.flow.kind_of(ctx.ticket.issue.view.issue_type) == "bug":
            reproduction = await self.reproduction(base_sha, candidate, ports)
            atomic_write_json(ctx.inputs_dir / "reproduction.json", reproduction)

        # 3. Fresh executable verification (separate process), reading the review.
        out_v = ctx.output_dir("verify-ticket")
        shutil.copy(out_r / "review.md", ctx.inputs_dir / "review.md")
        env = self.envelope(
            "verify-ticket",
            out_v,
            required=[ArtifactKind.VERIFICATION],
            approved=approved,
            related=related,
            ports=ports,
            review_report=str(ctx.inputs_dir / "review.md"),
            source=self.source_refs(base_commit=base_sha, candidate_sha=candidate),
        )
        verify = await self.run_procedure("verify-ticket", verify_wt, env, ports=ports)
        if (d := self.worker_decision(verify)) is not None:
            return d
        require_artifact(verify, out_v, ArtifactKind.VERIFICATION, "verification.md")
        tampered = await repo.tracked_changes(verify_wt)
        if tampered:
            return Decision(
                outcome="blocked",
                reason=f"verifier modified tracked files of the frozen candidate: {tampered[:10]}",
                action="Inspect the run; resume to re-run verification.",
                blocker_kind="verifier_modified_candidate",
            )

        # 4. CI evidence for the exact candidate (bounded wait, never treated as passed if absent).
        ci = await self.wait_for_ci(candidate)
        ci_results = ci.results if ci else []
        provenance = await self.ci_provenance(candidate, base_sha)

        # 5. Overlap checkpoint 4 (candidate published).
        fp = await coord.own_footprint(ctx.shared)
        overlap: list[OverlapFinding] = []
        if fp is not None:
            fp = fp.model_copy(
                update={
                    "actual_paths": (ctx.shared.footprint_ref or {}).get("actual_paths", []),
                    "actual_commit": candidate,
                }
            )
            overlap = await coord.check(fp, ctx.shared, checkpoint="candidate", use_actual=True)

        # Problems the coordinator found itself; each becomes an R-item for development.
        problems: list[str] = []
        failed = [c for c in all_checks if c.conclusion != "passed"]
        problems += [f"coordinator check {c.name} ({c.target}) {c.conclusion}" for c in failed]
        if ci and not ci.ok and not ci.pending:
            problems.append("CI failed: " + ", ".join(ci.problems[:5]))
        if provenance["state"] == "mismatch":
            problems.append(f"CI integration provenance does not match the candidate: {provenance['detail']}")
        findings = [
            *review.findings,
            *[f.model_copy(update={"id": f"F{100 + i}"}) for i, f in enumerate(verify.findings, 1)],
        ]
        # Differences from the specification that work are questions for a human, never a failure.
        found = [
            *review.deviations,
            *[x.model_copy(update={"id": f"D{100 + i}"}) for i, x in enumerate(verify.deviations, 1)],
        ]
        not_met = sorted(
            {e.criterion_id for e in [*review.evidence, *verify.evidence] if e.status == "not_met"}
        )
        if not_met:
            problems.append(f"acceptance criteria not met: {', '.join(not_met)}")
        serious = [f for f in findings if f.severity in (Severity.BLOCKER, Severity.MAJOR)]
        reasons = list(problems)
        if serious:
            reasons.append(f"{len(serious)} blocker/major findings ({', '.join(f.id for f in serious)})")
        verified = {e.criterion_id for e in verify.evidence if e.status in ("met", "deviates")}
        unverified = sorted({e.criterion_id for e in [*review.evidence, *verify.evidence]} - verified)
        extra = {
            "candidate": candidate,
            "base_sha": base_sha,
            "integration_tree": tree,
            "integration_head": int_head,
            "integration_with": merged_with,
            "merge_conflicts": conflicts,
            "problems": problems,
            "ci": [c.model_dump(mode="json") for c in ci_results],
            "ci_pending": bool(ci and ci.pending),
            "ci_provenance": provenance,
            "findings": [f.model_dump(mode="json") for f in findings],
            "deviations": [x.model_dump(mode="json") for x in found],
            "unverified": unverified,
            "overlap": findings_json(overlap),
            "review": review.model_dump(mode="json"),
            "verify": verify.model_dump(mode="json"),
            "reproduction": reproduction,
        }
        outcome = "verification_failed" if reasons else "success"
        passed = "verification passed" + (
            f" with {len(found)} deviation{'s' if len(found) != 1 else ''} from the specification to decide"
            if found
            else ""
        )
        return Decision(
            outcome=outcome,
            reason="; ".join(reasons) or passed,
            action="In Jira: Submit implementation changes to fix (moves into Ready for development), "
            f"or Revise scope. `delivery inspect {ctx.key}` shows why."
            if reasons
            else "",
            result=verify.model_dump(mode="json"),
            extra=extra,
        )

    async def reproduction(self, base_sha: str, candidate: str, ports: dict[str, int]) -> dict[str, Any]:
        """Run the check on the base branch with only the candidate's test files: a regression test
        for a bug fails there, because the fix is not."""
        ctx, repo, flow = self.ctx, self.deps.repo, self.ctx.cfg.flow
        names = list(ctx.cfg.checks.commands)
        check = flow.reproduce_check or ("unit" if "unit" in names else (names[0] if names else ""))
        tests = [p for p in await repo.diff_names(base_sha, candidate) if flow.is_test(p)]
        out: dict[str, Any] = {"state": "no_tests", "tests": tests, "check": check, "base": base_sha}
        if not tests:
            return out
        if not check:
            return {**out, "state": "error", "detail": "no check is configured to run the tests"}
        wt = await self.detached_worktree("reproduce", base_sha)
        present = await repo.git("ls-tree", "-r", "--name-only", candidate, "--", *tests, check=False)
        files = [ln for ln in present.stdout.splitlines() if ln]
        if files:
            await repo.git("checkout", candidate, "--", *files, cwd=wt)
        results = await self.setup_and_check(wt, "reproduction", base_sha, ports, [check], base_sha=base_sha)
        res = next((c for c in results if c.name == check), None)
        if res is None:
            return {**out, "state": "error", "detail": "setup failed on the base branch"}
        state = {"failed": "reproduced", "passed": "not_reproduced"}.get(res.conclusion, "error")
        return {**out, "state": state, "result": res.model_dump(mode="json")}

    async def setup_and_check(
        self,
        wt: Path,
        target: str,
        sha: str,
        ports: dict[str, int],
        names: list[str],
        tree_sha: str | None = None,
        base_sha: str | None = None,
    ) -> list[CheckResult]:
        ctx = self.ctx
        if ctx.cfg.checks.setup:
            setup = await run_checks(
                {"setup": ctx.cfg.checks.setup},
                ["setup"],
                worktree=wt,
                log_dir=ctx.logs_dir,
                target=target,
                sha=sha,
                ports=ports,
                tmp_dir=ctx.tmp_dir,
                timeout=ctx.cfg.runtime.check_timeout_seconds,
            )
            if not all_passed(setup):
                return setup
        return await run_checks(
            ctx.cfg.checks.commands,
            names,
            worktree=wt,
            log_dir=ctx.logs_dir,
            target=target,
            sha=sha,
            ports=ports,
            tmp_dir=ctx.tmp_dir,
            timeout=ctx.cfg.runtime.check_timeout_seconds,
            tree_sha=tree_sha,
            base_sha=base_sha,
        )

    async def ci_provenance(self, candidate: str, base_sha: str) -> dict[str, Any]:
        """Check which merge tree GitHub CI actually tested for this candidate.

        For pull_request events CI runs on a temporary merge commit, so check runs are attached
        to the PR head while the tested tree is head merged onto some base. The application's
        CI publishes ``tested=<merge sha> base=<base sha>`` as a commit status; this verifies
        that the tested commit really is the candidate merged onto that base, and whether that
        base is still current. Absent provenance is reported, never assumed.
        """
        import re as _re

        context = self.ctx.cfg.checks.ci.provenance_context
        statuses = [s for s in await self.deps.github.statuses(candidate) if s.context == context]
        if not statuses:
            return {"state": "missing", "detail": f"no {context} status on {candidate[:12]}"}
        st = max(statuses, key=lambda s: s.id)
        m = _re.search(r"tested=([0-9a-f]{40}).*base=([0-9a-f]{40})", st.description)
        if not m:
            return {"state": "mismatch", "detail": f"unparseable provenance {st.description!r}"}
        tested, ci_base = m.group(1), m.group(2)
        try:
            commit = await self.deps.github.commit(tested)
        except Exception as exc:
            return {"state": "missing", "detail": f"tested commit {tested[:12]} unreadable: {exc}"}
        if set(commit.parents) != {ci_base, candidate}:
            return {
                "state": "mismatch",
                "tested": tested,
                "base": ci_base,
                "detail": f"tested commit parents {[p[:12] for p in commit.parents]} are not "
                f"base {ci_base[:12]} + candidate {candidate[:12]}",
            }
        state = "verified" if ci_base == base_sha else "stale_base"
        return {
            "state": state,
            "tested": tested,
            "base": ci_base,
            "current_base": base_sha,
            "detail": "CI tested the candidate merged onto the current base"
            if state == "verified"
            else "CI tested an older base; the coordinator's integration tree covers the current base",
        }

    async def wait_for_ci(self, sha: str) -> Any:
        ctx = self.ctx
        names = ctx.cfg.checks.ci.required_names
        if not names:
            return None
        deadline = asyncio.get_running_loop().time() + min(ctx.cfg.runtime.check_timeout_seconds, 1800)
        delay = 15.0
        while True:
            ci = evaluate_ci(
                sha,
                required_names=names,
                expected_producer=ctx.cfg.checks.ci.expected_producer,
                check_runs=await self.deps.github.check_runs(sha),
                statuses=await self.deps.github.statuses(sha),
                allow_neutral=ctx.cfg.checks.ci.allow_neutral,
                allow_skipped=ctx.cfg.checks.ci.allow_skipped,
            )
            missing = [r for r in ci.results if r.conclusion == "missing"]
            if not (ci.pending or missing) or asyncio.get_running_loop().time() > deadline:
                return ci
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 120)

    async def publish(self, d: Decision) -> None:
        ctx = self.ctx
        pub = ctx.publisher()
        if d.outcome == "blocked":
            await self.publish_block(d, Status.VERIFYING)
            return
        candidate = d.extra["candidate"]
        rdir = f"{ctx.doc_root}/reviews/{ctx.run_id}"
        found = [Deviation.model_validate(x) for x in d.extra.get("deviations", [])]
        meta = {
            "candidate_sha": candidate,
            "base_sha": d.extra.get("base_sha"),
            "integration_tree": d.extra.get("integration_tree"),
            "integration_with": ",".join(d.extra.get("integration_with", [])) or "base only",
            "merge_conflicts": "; ".join(
                f"{c['with']}@{str(c['sha'])[:12]}: {', '.join(c['paths'])}"
                for c in d.extra.get("merge_conflicts", [])
            )
            or "none",
        }
        files = {
            f"{rdir}/review.md": provenance_header(ctx, "review", ctx.run_id, meta)
            + (ctx.output_dir("review-ticket") / "review.md").read_text(),
            f"{rdir}/verification.md": provenance_header(ctx, "verification", ctx.run_id, meta)
            + (ctx.output_dir("verify-ticket") / "verification.md").read_text(),
            f"{rdir}/checks.json": json.dumps(
                {
                    "coordinator": [c.model_dump(mode="json") for c in ctx.record.checks],
                    "ci": d.extra.get("ci", []),
                    "reproduction": d.extra.get("reproduction"),
                    **meta,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            f"{ctx.doc_root}/executions/{ctx.run_id}.json": self.execution_summary(d, meta),
        }
        if found:
            files[f"{rdir}/deviations.json"] = (
                json.dumps([x.model_dump(mode="json") for x in found], indent=2, sort_keys=True) + "\n"
            )
        guide = ctx.output_dir("verify-ticket") / ACCEPTANCE_GUIDE
        if guide.is_file() and guide.stat().st_size <= MAX_OUTPUT_FILE_BYTES:
            files[f"{rdir}/{ACCEPTANCE_GUIDE}"] = provenance_header(
                ctx, "acceptance_guide", ctx.run_id, {"candidate_sha": candidate}
            ) + guide.read_text(errors="replace")
        sha = await self.publish_files(files, "reports", ctx.run_id, f"{ctx.key}: verification {ctx.run_id}")
        repo_url = ctx.cfg.repository.url
        review_url = blob_url(repo_url, sha, f"{rdir}/review.md")
        verify_url = blob_url(repo_url, sha, f"{rdir}/verification.md")
        findings = [Finding.model_validate(f) for f in d.extra.get("findings", [])]
        checks = list(ctx.record.checks) + [CheckResult.model_validate(c) for c in d.extra.get("ci", [])]
        n = ctx.shared.candidate_number
        pr_url = f"{repo_url.removesuffix('.git')}/pull/{ctx.shared.pr_number}"
        code_token = gate_token(ctx.key, GateKind.CODE, n)
        accept_token = gate_token(ctx.key, GateKind.ACCEPT, n)
        artefacts = {
            k: v for k, v in ctx.shared.artefacts.items() if k not in ("deviations", "acceptance_guide")
        }
        if found:
            artefacts["deviations"] = f"{rdir}/deviations.json@{sha}"
        if f"{rdir}/{ACCEPTANCE_GUIDE}" in files:
            artefacts["acceptance_guide"] = f"{rdir}/{ACCEPTANCE_GUIDE}@{sha}"
        records = deviations.to_records(found, n)
        base = {
            "current_run_id": ctx.run_id,
            "current_stage": self.stage,
            "updated_at": utcnow(),
            "artefacts": {
                **artefacts,
                "review": f"{rdir}/review.md@{sha}",
                "verification": f"{rdir}/verification.md@{sha}",
            },
            "deviations": records,
        }
        conflicts = list(d.extra.get("merge_conflicts", []))
        base_branch = ctx.cfg.repository.base_branch
        if d.outcome == "verification_failed":
            problems = list(d.extra.get("problems", []))
            ctx.shared = ctx.shared.model_copy(
                update={
                    **base,
                    "current_state": RunState.AWAITING_HUMAN,
                    "pending_feedback": [
                        {"id": f.id, "description": f.description, "severity": f.severity.value}
                        for f in findings
                    ]
                    + [{"id": f"R{i}", "description": r} for i, r in enumerate(problems, 1)],
                }
            )
            posted = await self.announce(
                "verification-failed",
                comments.verification_failed(
                    pr_url,
                    n,
                    candidate,
                    review_url,
                    verify_url,
                    checks,
                    findings,
                    problems,
                    base=base_branch,
                    merge_conflicts=conflicts,
                    claude_resolves=ctx.cfg.flow.resolve_conflicts,
                    deviations=records,
                ),
                candidate[:12],
            )
            self._stamp_deviations(posted)
            await pub.save_record(ctx.key, ctx.shared, "verification-failed")
            await pub.transition(ctx.key, Status.VERIFYING, Action.VERIFICATION_FAILED)
            return
        now = utcnow()
        gates = supersede_for_new_revision(ctx.shared.gates, GateKind.CODE, code_token)
        gates = [g for g in gates if g.token not in (code_token, accept_token)]
        gates += [
            GateRecord(
                token=code_token,
                kind=GateKind.CODE,
                ticket_key=ctx.key,
                revision=n,
                candidate_sha=candidate,
                pr_number=ctx.shared.pr_number,
                published_at=now,
                approvers=ctx.cfg.approvals.jira_account_ids,
                artefact_path=f"{rdir}/review.md",
                artefact_commit=sha,
            ),
            GateRecord(
                token=accept_token,
                kind=GateKind.ACCEPT,
                ticket_key=ctx.key,
                revision=n,
                candidate_sha=candidate,
                pr_number=ctx.shared.pr_number,
                published_at=now,
                approvers=ctx.cfg.approvals.jira_account_ids,
            ),
        ]
        ctx.shared = ctx.shared.model_copy(
            update={**base, "gates": gates, "current_state": RunState.AWAITING_HUMAN}
        )
        overlap = [_finding(o) for o in d.extra.get("overlap", [])]
        from delivery.coordination import Coordinator

        await Coordinator(self.deps).publish_warnings(ctx, overlap)
        posted = await self.announce(
            "code-gate",
            comments.code_gate(
                n,
                pr_url,
                candidate,
                review_url,
                verify_url,
                checks,
                [f for f in findings if f.severity not in (Severity.BLOCKER, Severity.MAJOR)],
                d.extra.get("unverified", []),
                overlap,
                base=base_branch,
                merge_conflicts=conflicts,
                deviations=records,
                claude_resolves=ctx.cfg.flow.resolve_conflicts,
                reproduction=d.extra.get("reproduction"),
            ),
            f"c{n}",
            gate_tokens=(code_token, accept_token),
        )
        self._stamp_deviations(posted)
        await pub.save_record(ctx.key, ctx.shared, "code-gate")
        await pub.transition(ctx.key, Status.VERIFYING, Action.COMPLETE_VERIFICATION)

    def _stamp_deviations(self, posted: Posted) -> None:
        """Decisions about deviations count from the comment that announced them (Jira's clock)."""
        sh = self.ctx.shared
        devs = [
            d.model_copy(update={"announced_at": posted.created}) if d.announced_at is None else d
            for d in sh.deviations
        ]
        self.ctx.shared = sh.model_copy(update={"deviations": devs})


# --------------------------------------------------------------------------- release preparation


class ReleasePreparationStage(StageStrategy):
    stage = Stage.RELEASE_PREPARATION

    async def work(self) -> Decision:
        ctx = self.ctx
        candidate = ctx.shared.candidate_sha
        assert candidate
        await self.deps.repo.fetch()
        pr = await self.deps.github.get_pr(ctx.shared.pr_number) if ctx.shared.pr_number else None
        if pr is None or pr.head_sha != candidate:
            return Decision(
                outcome="blocked",
                reason="the PR head no longer matches the accepted candidate",
                action="New commits need a new candidate: request code changes.",
                blocker_kind="candidate_changed",
            )
        early_merge: str | None = None
        if pr.merged:
            # The head still equals the accepted candidate, so what was merged is exactly what
            # was verified. Flag the early merge and carry on; release approval still gates Done.
            if not pr.merge_commit_sha:
                return Decision(
                    outcome="blocked",
                    reason="the PR was merged before release approval, and GitHub reports no merge commit",
                    action="Investigate the bypassed gate; the merged code cannot be identified.",
                    blocker_kind="gate_bypassed",
                )
            early_merge = pr.merge_commit_sha
        from delivery.coordination import Coordinator

        coord = Coordinator(self.deps)
        fp = await coord.own_footprint(ctx.shared)
        if fp is not None:
            await coord.publish_warnings(
                ctx,
                await coord.check(
                    fp.model_copy(
                        update={"actual_paths": (ctx.shared.footprint_ref or {}).get("actual_paths", [])}
                    ),
                    ctx.shared,
                    checkpoint="pre-release",
                    use_actual=True,
                ),
            )
        if (d := await self.amend_specification()) is not None:
            return d
        wt = await self.detached_worktree("release", candidate)
        approved = [
            a
            for a in (
                await self.spec_input(),
                await self.approved_input(GateKind.PLAN, ArtifactKind.PLAN),
            )
            if a
        ]
        for label in ("review", "verification"):
            ref = ctx.shared.artefacts.get(label)
            if ref and "@" in ref:
                path, commit = ref.rsplit("@", 1)
                dest = await self.copy_input(commit, path, f"approved/{label}.md")
                if dest:
                    approved.append(
                        ArtefactPointer(kind=ArtifactKind(label), path=str(dest), revision=0, commit=commit)
                    )
        revs = await self.revisions("releases")
        nxt = max([*revs, ctx.shared.release_revision, 0]) + 1
        out = ctx.output_dir("prepare-release")
        env = self.envelope(
            "prepare-release",
            out,
            required=[ArtifactKind.RELEASE],
            next_revision=nxt,
            approved=approved,
            source=self.source_refs(candidate_sha=candidate),
        )
        result = await self.run_procedure("prepare-release", wt, env)
        if (d := self.worker_decision(result)) is not None:
            return d
        require_artifact(result, out, ArtifactKind.RELEASE, "release.md")
        if result.release is None or result.release.candidate_sha != candidate:
            raise WorkerFailure(
                "prepare-release", None, "release proposal does not name the accepted candidate"
            )
        if result.outcome is Outcome.NEEDS_CLARIFICATION:
            return Decision(
                outcome="blocked",
                reason="release preparation needs answers: "
                + "; ".join(q.question for q in result.questions),
                action="Answer in a comment, then resume release preparation.",
                blocker_kind="release_questions",
                result=result.model_dump(mode="json"),
            )
        return Decision(
            outcome="success",
            reason=result.summary,
            result=result.model_dump(mode="json"),
            extra={"revision": nxt, "candidate": candidate, "merged_early": early_merge},
        )

    async def publish(self, d: Decision) -> None:
        ctx = self.ctx
        pub = ctx.publisher()
        await self.publish_amendment()
        if d.outcome == "blocked":
            await self.publish_block(d, Status.PREPARING_RELEASE)
            return
        rev = int(d.extra["revision"])
        rel = f"{ctx.doc_root}/releases/v{rev:03d}.md"
        extra = {"candidate_sha": d.extra["candidate"]}
        if self.follow_up:
            extra["follow_up_of"] = self.follow_up.replaces
        header = provenance_header(ctx, "release", f"v{rev:03d}", extra)
        files = {rel: header + (ctx.output_dir("prepare-release") / "release.md").read_text()}
        if self.follow_up is None:
            files[f"{ctx.doc_root}/executions/{ctx.run_id}.json"] = self.execution_summary(
                d, {"artefact": rel}
            )
        sha = await self.publish_files(files, "release", f"v{rev}", f"{ctx.key}: release proposal v{rev:03d}")
        token = gate_token(ctx.key, GateKind.RELEASE, rev)
        # Published again after a crash: the gate recorded the first time stands.
        gates = ctx.shared.gates
        if not any(g.token == token for g in gates):
            gate = GateRecord(
                token=token,
                kind=GateKind.RELEASE,
                ticket_key=ctx.key,
                revision=rev,
                artefact_path=rel,
                artefact_commit=sha,
                candidate_sha=d.extra["candidate"],
                pr_number=ctx.shared.pr_number,
                published_at=utcnow(),
                approvers=ctx.cfg.approvals.jira_account_ids,
            )
            gates = supersede_for_new_revision(gates, GateKind.RELEASE, token) + [gate]
        ctx.shared = ctx.shared.model_copy(
            update={
                "release_revision": rev,
                "gates": gates,
                "artefacts": {**ctx.shared.artefacts, "release": f"{rel}@{sha}"},
                "current_run_id": ctx.run_id,
                "current_stage": self.stage,
                "current_state": RunState.AWAITING_HUMAN,
                "updated_at": utcnow(),
            }
        )
        await self.announce(
            "release-gate",
            comments.release_gate(
                blob_url(ctx.cfg.repository.url, sha, rel),
                rev,
                d.extra["candidate"],
                note=self.gate_note(),
                merged_early=d.extra.get("merged_early"),
                pr_number=ctx.shared.pr_number,
            ),
            f"v{rev}",
            gate_tokens=(token,),
        )
        await pub.save_record(ctx.key, ctx.shared, "release-gate")
        if self.follow_up:
            return
        SessionRegistry(ctx.cfg.runtime.state_dir).published(ctx.run_id, revision=rev)
        await pub.transition(ctx.key, Status.PREPARING_RELEASE, Action.COMPLETE_RELEASE_PREPARATION)

    async def follow_up_decision(self, d: Decision, rev: int) -> Decision:
        if self.ctx.shared.candidate_sha != d.extra.get("candidate"):
            raise FollowUpRefused(
                "the accepted candidate has changed since this release proposal was written"
            )
        return await super().follow_up_decision(d, rev)


# --------------------------------------------------------------------------- release verification


class ReleaseVerificationStage(StageStrategy):
    stage = Stage.RELEASE_VERIFICATION

    async def provenance(self, record: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        """Establish that the recorded release contains exactly the approved candidate."""
        ctx = self.ctx
        repo = self.deps.repo
        await repo.fetch()
        candidate = ctx.shared.candidate_sha or ""
        released = str(record["commit"])
        if ctx.shared.pr_number is None:
            return False, "no PR recorded", {}
        pr = await self.deps.github.get_pr(ctx.shared.pr_number)
        info: dict[str, Any] = {
            "pr": pr.number,
            "pr_head": pr.head_sha,
            "merged": pr.merged,
            "merge_commit": pr.merge_commit_sha,
            "merged_by": pr.merged_by,
            "released": released,
            "candidate": candidate,
        }
        if not pr.merged or not pr.merge_commit_sha:
            return False, f"PR #{pr.number} is not merged", info
        base = ctx.cfg.repository.base_branch
        base_sha = await repo.remote_sha(base) or ""
        resolved: list[str] = []
        if pr.head_sha != candidate:
            # Conflicts are resolved when merging (e.g. GitHub's Resolve conflicts), which merges
            # the base into the PR branch. Accept only that: no other commits on top.
            extra = await self._merge_only_update(candidate, pr.head_sha, f"{pr.merge_commit_sha}^1")
            if extra is None:
                return (
                    False,
                    (
                        f"merged PR head {pr.head_sha[:12]} is not the approved candidate "
                        f"{candidate[:12]} (unapproved changes merged)"
                    ),
                    info,
                )
            resolved = extra
            info["merged_base_into_candidate"] = pr.head_sha
            info["resolved_paths"] = resolved
            candidate = pr.head_sha
        if not await repo.resolve(released):
            return False, f"recorded commit {released[:12]} does not exist in the repository", info
        if not await repo.is_ancestor(released, base_sha):
            return (
                False,
                f"recorded commit {released[:12]} is not on {ctx.cfg.repository.base_branch}",
                info,
            )
        merge = pr.merge_commit_sha
        if not await repo.is_ancestor(merge, released):
            return (
                False,
                f"recorded commit {released[:12]} does not contain merge {merge[:12]} (unrelated)",
                info,
            )
        details = await repo.commit_details(merge)
        strategy = "unknown"
        exact = False
        if len(details.parents) == 2 and details.parents[1] == candidate:
            strategy = "merge commit"
            exact = True
        else:
            # Squash or rebase: the merged tree must equal candidate merged onto some first-parent
            # ancestor of the merge commit (the base the human merged onto).
            probe = merge
            for _ in range(60):
                parent = (await repo.commit_details(probe)).parents
                if not parent:
                    break
                base_before = parent[0]
                mt = await repo.git("merge-tree", "--write-tree", base_before, candidate, check=False)
                expected_tree = mt.stdout.split("\n", 1)[0].strip() if mt.returncode == 0 else ""
                if expected_tree and expected_tree == details.tree:
                    strategy = "squash" if probe == merge else "rebase"
                    exact = True
                    break
                probe = base_before
        info.update(
            {
                "strategy": strategy,
                "merged_tree_matches_candidate": exact,
                "released_equals_merge": released == merge,
            }
        )
        if resolved:
            return (
                True,
                (
                    f"the approved candidate was merged with {base} when merging (conflict "
                    f"resolution); check the resolved files on the released commit: {', '.join(resolved)}"
                ),
                info,
            )
        if not exact:
            return (
                True,
                (
                    "merge tree differs from the reviewed candidate: additional integration "
                    "checks on the released commit are required"
                ),
                info,
            )
        return True, f"{strategy}; released commit contains the approved candidate", info

    async def _merge_only_update(self, candidate: str, head: str, base_before: str) -> list[str] | None:
        """If ``head`` is ``candidate`` plus merges of the base branch (as it was before the PR
        merged) only, the files those merges changed beyond a plain merge (the conflict
        resolutions); otherwise None."""
        repo = self.deps.repo
        if not await repo.is_ancestor(candidate, head):
            return None
        own = await repo.git("rev-list", "--no-merges", head, f"^{candidate}", f"^{base_before}", check=False)
        if own.returncode != 0 or own.stdout.strip():
            return None  # commits that are neither the candidate's nor the base's
        merges = await repo.git("rev-list", "--merges", head, f"^{candidate}", check=False)
        paths: set[str] = set()
        for sha in merges.stdout.split():
            # `--cc` shows only what a merge changed beyond its parents: the resolutions.
            cc = await repo.git("show", "--cc", "--name-only", "--format=", sha, check=False)
            paths.update(n for n in cc.stdout.splitlines() if n.strip())
        return sorted(paths)

    async def work(self) -> Decision:
        ctx = self.ctx
        record = ctx.intake.release_record or ctx.shared.release.get("record")
        if not record:
            return Decision(
                outcome="blocked",
                reason="no release record",
                action="Record the release.",
                blocker_kind="missing_input",
            )
        ok, why, info = await self.provenance(record)
        if not ok:
            return Decision(
                outcome="blocked",
                reason=f"release provenance failed: {why}",
                action="Investigate the merge/release; record the correct release, then resume.",
                blocker_kind="provenance",
                extra={"provenance": info, "record": record},
            )
        released = str(record["commit"])
        ports = ctx.record.ports or self.deps.ports.allocate(ctx.run_id)
        ctx.record = ctx.record.model_copy(update={"ports": ports})
        wt = await self.detached_worktree("release", released)
        checks: list[CheckResult] = []
        if ctx.cfg.checks.setup:
            checks += await run_checks(
                {"setup": ctx.cfg.checks.setup},
                ["setup"],
                worktree=wt,
                log_dir=ctx.logs_dir,
                target="release",
                sha=released,
                ports=ports,
                tmp_dir=ctx.tmp_dir,
                timeout=ctx.cfg.runtime.check_timeout_seconds,
            )
        if ctx.cfg.release.smoke_commands:
            checks += await run_checks(
                ctx.cfg.release.smoke_commands,
                list(ctx.cfg.release.smoke_commands),
                worktree=wt,
                log_dir=ctx.logs_dir,
                target="release",
                sha=released,
                ports=ports,
                tmp_dir=ctx.tmp_dir,
                timeout=ctx.cfg.runtime.check_timeout_seconds,
            )
        needs_integration = not info.get("merged_tree_matches_candidate") or not info.get(
            "released_equals_merge"
        )
        if needs_integration:
            checks += await run_checks(
                ctx.cfg.checks.commands,
                ctx.cfg.checks.integration_names,
                worktree=wt,
                log_dir=ctx.logs_dir,
                target="release",
                sha=released,
                ports=ports,
                tmp_dir=ctx.tmp_dir,
                timeout=ctx.cfg.runtime.check_timeout_seconds,
            )
        ctx.record = ctx.record.model_copy(update={"checks": checks})
        ctx.save("release_checks", passed=all_passed(checks) if checks else None)
        atomic_write_json(
            ctx.inputs_dir / "coordinator_checks.json", [c.model_dump(mode="json") for c in checks]
        )
        share_check_logs(ctx, checks)
        atomic_write_json(ctx.inputs_dir / "release_record.json", {**record, "provenance": info})
        rel = await self.approved_input(GateKind.RELEASE, ArtifactKind.RELEASE)
        out = ctx.output_dir("verify-release")
        env = self.envelope(
            "verify-release",
            out,
            required=[ArtifactKind.RELEASE_VERIFICATION],
            approved=[a for a in (rel,) if a],
            ports=ports,
            source=self.source_refs(candidate_sha=ctx.shared.candidate_sha),
        )
        result = await self.run_procedure("verify-release", wt, env, ports=ports)
        if (d := self.worker_decision(result)) is not None:
            return d
        require_artifact(result, out, ArtifactKind.RELEASE_VERIFICATION, "release-verification.md")
        reasons = [f"check {c.name} {c.conclusion}" for c in checks if c.conclusion != "passed"]
        blockers = [f for f in result.findings if f.severity is Severity.BLOCKER]
        if blockers:
            reasons.append(f"{len(blockers)} blocker findings in release verification")
        extra = {"provenance": info, "provenance_summary": why, "record": record}
        if reasons:
            return Decision(
                outcome="blocked",
                reason="release verification failed: " + "; ".join(reasons),
                action="A human must investigate the release (the coordinator never rolls back), "
                "then resume release verification.",
                blocker_kind="release_verification_failed",
                result=result.model_dump(mode="json"),
                extra=extra,
            )
        return Decision(
            outcome="success",
            reason=result.summary,
            result=result.model_dump(mode="json"),
            extra=extra,
        )

    async def publish(self, d: Decision) -> None:
        ctx = self.ctx
        pub = ctx.publisher()
        record = d.extra.get("record") or {}
        release_id = f"{str(record.get('commit', 'unknown'))[:12]}-{record.get('environment', '')}"
        rdir = f"{ctx.doc_root}/releases/{release_id}"
        files: dict[str, str] = {
            f"{rdir}/provenance.json": json.dumps(
                {
                    "record": record,
                    "provenance": d.extra.get("provenance"),
                    "checks": [c.model_dump(mode="json") for c in ctx.record.checks],
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n",
            f"{ctx.doc_root}/executions/{ctx.run_id}.json": self.execution_summary(d, {"release": record}),
        }
        out = ctx.output_dir("verify-release") / "release-verification.md"
        if out.exists():
            files[f"{rdir}/verification.md"] = (
                provenance_header(
                    ctx,
                    "release_verification",
                    release_id,
                    {"released_commit": record.get("commit")},
                )
                + out.read_text()
            )
        if d.outcome == "blocked" and not files:
            await self.publish_block(d, Status.VERIFYING_RELEASE)
            return
        sha = await self.publish_files(
            files,
            "release-verification",
            release_id,
            f"{ctx.key}: release verification {release_id}",
        )
        if d.outcome == "blocked":
            ctx.shared = ctx.shared.model_copy(
                update={
                    "release": {
                        **ctx.shared.release,
                        "record": record,
                        "verification": f"{rdir}@{sha}",
                    }
                }
            )
            await self.publish_block(d, Status.VERIFYING_RELEASE)
            return
        ctx.shared = ctx.shared.model_copy(
            update={
                "release": {
                    "record": record,
                    "provenance": d.extra.get("provenance"),
                    "verification": f"{rdir}@{sha}",
                },
                "current_run_id": ctx.run_id,
                "current_stage": self.stage,
                "current_state": RunState.COMPLETED,
                "pause": None,
                "updated_at": utcnow(),
            }
        )
        await pub.save_record(ctx.key, ctx.shared, "done")
        url = blob_url(ctx.cfg.repository.url, sha, f"{rdir}/verification.md")
        await pub.comment(
            ctx.key,
            "done",
            comments.done(
                str(record.get("commit")),
                str(record.get("environment")),
                url,
            ),
            release_id,
        )
        await pub.set_resume_field(ctx.key, None, "clear")
        await pub.transition(ctx.key, Status.VERIFYING_RELEASE, Action.COMPLETE_RELEASE_VERIFICATION)


class ResolutionStage(StageStrategy):
    """Clear a blocker with the developer, then send the ticket back to the stage that blocked.

    Not a lifecycle stage: it runs when a person moves a Blocked ticket to Ready for resolution.
    Claude works in the ticket's feature worktree and asks the developer in the session for the
    decisions that are theirs. The coordinator writes what was done, and who decided what, in the
    ticket. The changes it makes are carried into development like a blocked run's unfinished work.
    """

    stage = Stage.RESOLUTION
    PROCEDURE = "resolve-blocker"

    @property
    def resume(self) -> Stage:
        resume = self.ctx.intake.resume_stage
        if resume is None or resume is Stage.RESOLUTION:
            raise OutputInvalid("no stage to return to is recorded")
        return resume

    def _blocked(self, d: Decision, why: str, action: str, kind: str, **extra: Any) -> Decision:
        return d.model_copy(
            update={
                "outcome": "blocked",
                "reason": why,
                "action": action,
                "blocker_kind": kind,
                "resume_stage": self.resume.value,
                "extra": {**d.extra, **extra},
            }
        )

    async def preflight(self) -> Decision | None:
        ctx = self.ctx
        if not ctx.cfg.claude.interactive.enabled:
            return Decision(
                outcome="blocked",
                reason="resolution needs an interactive Claude session, and [claude.interactive] is off",
                action="Set `enabled = true` under [claude.interactive], restart the coordinator, then "
                "choose Request resolution again.",
                blocker_kind="needs_interactive",
                resume_stage=self.resume.value,
            )
        if SessionRegistry(ctx.cfg.runtime.state_dir).development(ctx.key) is not None:
            return Decision(
                outcome="blocked",
                reason="a development session for this ticket is still open and holds its working copy",
                action=f"Close it (`delivery close {ctx.key}`), then choose Request resolution again.",
                blocker_kind="open_session",
                resume_stage=self.resume.value,
            )
        return None

    def _blocked_run(self) -> Any:
        """The latest run of the stage that blocked (this machine's journal), if any."""
        for e in reversed(self.deps.store.runs_for_ticket(self.ctx.key)):
            if e.record is not None and e.record.stage is self.resume and e.record.state is RunState.BLOCKED:
                return e
        return None

    def _transcript_tail(self, entry: Any) -> list[str]:
        if entry is None:
            return []
        lines: list[str] = []
        for proc in STAGES[self.resume].procedures:
            log = entry.journal.dir / "logs" / f"claude-{proc}.jsonl"
            if log.exists():
                try:
                    lines += [ln for ln in render_file(log) if ln.strip()]
                except (OSError, ValueError):
                    continue
        return lines[-80:]

    def _failed_logs(self, entry: Any) -> list[tuple[str, str]]:
        if entry is None:
            return []
        out = []
        for path in sorted((entry.journal.dir / "logs").glob("*.err.log")):
            try:
                if path.stat().st_size:
                    out.append((path.name, path.read_text(errors="replace")[-6000:]))
            except OSError:
                continue
        return out[:3]

    async def _carry_unfinished(self, wt: Path, start_sha: str, entry: Any) -> PriorWork | None:
        """Apply the blocked development run's unfinished changes, so the session starts from them."""
        if entry is None or entry.record is None:
            return None
        meta = entry.record.outputs.get("wip")
        patch = entry.journal.dir / "wip" / "changes.patch"
        if not meta or not patch.exists():
            return None
        if meta.get("start_sha") != start_sha:
            self.ctx.journal.events.append(
                "wip_not_applied", {"from": entry.run_id, "error": "the branch moved on since it was saved"}
            )
            return None
        applied = await self.deps.repo.git("apply", "--index", "--binary", str(patch), cwd=wt, check=False)
        if applied.returncode != 0:
            self.ctx.journal.events.append(
                "wip_not_applied", {"from": entry.run_id, "error": applied.stderr[:300]}
            )
            return None
        self.ctx.record.outputs["continued_from"] = entry.run_id
        self.ctx.save("wip_applied", source=entry.run_id, files=len(meta.get("files", [])))
        tail = self.ctx.inputs_dir / "prior-session-tail.txt"
        tail.write_text("\n".join(self._transcript_tail(entry)) + "\n")
        return PriorWork(run_id=entry.run_id, files=list(meta.get("files", [])), session_tail_path=str(tail))

    async def work(self) -> Decision:
        ctx, repo = self.ctx, self.deps.repo
        resume = self.resume
        base = ctx.cfg.repository.base_branch
        entry = self._blocked_run()
        wt = ctx.worktree_path("feature")
        fresh = not wt.exists()
        if fresh:
            exists = await repo.remote_sha(ctx.feature_branch)
            start = f"origin/{ctx.feature_branch}" if exists else f"origin/{base}"
            await repo.add_worktree(wt, start=start, branch=ctx.feature_branch)
            ctx.record.worktrees["feature"] = str(wt)
        start_sha = ctx.record.outputs.get("start_sha") or await repo.worktree_head(wt)
        ctx.record.outputs["start_sha"] = start_sha
        ctx.save("worktree_ready", start_sha=start_sha)
        carry = resume is Stage.DEVELOPMENT
        spec = await self.spec_input()
        plan = await self.approved_input(GateKind.PLAN, ArtifactKind.PLAN)
        prior = await self._carry_unfinished(wt, start_sha, entry) if carry and fresh else None
        if carry and not fresh:
            files = await repo.changed_paths(wt, start_sha)
            prior = PriorWork(run_id=ctx.run_id, files=files) if files else None
        pause = ctx.shared.pause
        reason = pause.reason if pause else ctx.intake.reason
        record = entry.record if entry is not None else None
        # What the coordinator reads on this ticket, so that nothing Claude asks a person to do is a
        # guess: its gates, the request comments already there and how each is read, the human
        # templates, and the actions Jira offers when Blocked.
        blocked_actions = {
            ctx.cfg.workflow.action_name(r.action).lower(): r.resume_stage.value if r.resume_stage else None
            for r in ROUTES
            if r.source is Status.BLOCKED and r.actor is Actor.HUMAN
        }
        expect_extra: dict[str, Any] = {
            "tokens": current_tokens(ctx.shared.gates),
            "blocked_actions": blocked_actions,
            "resume_stage": resume.value,
            "release_environment": ctx.cfg.release.environment,
        }
        templates = install_root(ctx.cfg) / "docs" / "human-templates.md"
        if templates.is_file():
            shutil.copyfile(templates, ctx.inputs_dir / "human-templates.md")
        briefing = write_blocker_briefing(
            ctx.inputs_dir / "briefing.md",
            key=ctx.key,
            summary=ctx.ticket.issue.view.summary,
            blocked_stage=resume.value,
            blocker_kind=ctx.intake.blocker_kind,
            blocker_reason=reason,
            next_action=(record.next_action if record else "") or "",
            blocked_run={
                "run": entry.run_id,
                "state": record.state.value,
                "outcome": record.outcome.value if record.outcome else "",
                "reason": record.reason,
            }
            if entry is not None and record is not None
            else None,
            comments=[
                (f"{c.author_name or c.author_account_id}, {c.created:%a %d %b %H:%M}", c.body_text)
                for c in ctx.ticket.comments
            ],
            logs=self._failed_logs(entry),
            transcript_tail=self._transcript_tail(entry),
            ticket_state=ticket_state_lines(
                gates=ctx.shared.gates,
                comments=ctx.ticket.comments,
                resume_stage=resume.value,
                blocked_actions=blocked_actions,
            ),
        )
        ports = ctx.record.ports or self.deps.ports.allocate(ctx.run_id)
        ctx.record = ctx.record.model_copy(update={"ports": ports})
        out = ctx.output_dir(self.PROCEDURE)
        env = self.envelope(
            self.PROCEDURE,
            out,
            required=[],
            approved=[a for a in (spec, plan) if a is not None],
            ports=ports,
            source=self.source_refs(feature_commit=start_sha),
            write_globs=[f"{wt}/**", f"{out}/**"],
            prior_work=prior,
        ).model_copy(
            update={
                "resolution": ResolutionInput(
                    blocked_stage=resume,
                    blocker_kind=ctx.intake.blocker_kind,
                    blocker_reason=reason or "No reason recorded.",
                    next_action=(record.next_action if record else "") or "",
                    blocked_run_id=entry.run_id if entry is not None else None,
                    briefing_path=str(briefing),
                    code_changes_carried=carry and spec is not None and plan is not None,
                )
            }
        )
        can_keep = carry and spec is not None and plan is not None
        try:
            result = await self.run_procedure(self.PROCEDURE, wt, env, ports=ports, expect_extra=expect_extra)
        except WorkerFailure:
            if can_keep:
                await self._save_unfinished(wt, start_sha, spec, plan)  # type: ignore[arg-type]
            raise
        head = await repo.worktree_head(wt)
        if head != start_sha:
            await repo.git("reset", "--soft", start_sha, cwd=wt)  # the coordinator commits, not Claude
            ctx.journal.events.append("worker_commits_folded", {"head": head})
        changed = await repo.changed_paths(wt, start_sha)
        report = result.resolution or ResolutionReport()
        asked = asked_in_session(ctx.logs_dir / f"claude-{self.PROCEDURE}.jsonl")
        typed = typed_by_developer(read_events(ctx.journal.dir / "sessions" / self.PROCEDURE))
        info = {
            "summary": result.summary,
            "actions": report.actions,
            "decisions": attribute(report.decisions, asked, typed),
            "follow_ups": report.follow_ups,
            "next_steps": [s.model_dump(mode="json") for s in report.next_steps],
            "questions_asked": len(asked),
            "messages_typed": len(typed),
            "resume_stage": resume.value,
            "changed": changed,
        }
        ctx.record.outputs["resolution"] = info
        ctx.save(
            "resolution_recorded", questions=len(asked), typed=len(typed), decisions=len(report.decisions)
        )
        done = Decision(
            outcome="success",
            reason=result.summary,
            result=result.model_dump(mode="json"),
            resume_stage=resume.value,
            extra={"resolution": info},
        )
        protected = [p for p in changed if is_protected(p)]
        if protected:
            return self._blocked(
                done,
                f"the resolution changed protected paths: {protected}",
                "Protected policy files cannot change here; raise a separate human-owned change, then "
                "choose Request resolution again.",
                "protected_paths",
            )
        if changed and not can_keep:
            return self._blocked(
                done,
                f"the resolution changed {len(changed)} files, but {resume.value.replace('_', ' ')} "
                "does not carry code changes, so they were not kept",
                "Make the change by hand (or through development), then Resume.",
                "changes_not_carried",
            )
        if changed:
            await self._save_unfinished(wt, start_sha, spec, plan)  # type: ignore[arg-type]
        problem = check_next_steps(result.model_dump(mode="json"), {**expect_extra})
        if problem:
            # The session's hook asks for this to be fixed before it lets Claude stop; this is the
            # backstop if it was let go after repeated refusals. A step nobody can act on is not posted.
            return self._blocked(
                done,
                f"the resolution's next steps could not be used: {problem}",
                "Request resolution again, or deal with the blocker by hand and Resume.",
                "unusable_steps",
            )
        if result.outcome is not Outcome.COMPLETED:
            why = result.blocker_reason or result.summary
            return self._blocked(
                done,
                why,
                "Deal with what the report above says, then Resume or Request resolution again.",
                "unresolved",
            )
        return done

    async def publish(self, d: Decision) -> None:
        ctx = self.ctx
        pub = ctx.publisher()
        resume = self.resume
        info = d.extra.get("resolution") or {}
        developer = next(
            (
                c.author_name
                for c in reversed(ctx.ticket.comments)
                if c.author_account_id == ctx.cfg.identity.developer_jira_account_id and c.author_name
            ),
            "the developer",
        )
        report: dict[str, Any] = {
            "resume_stage": resume.value,
            "decisions": list(info.get("decisions") or []),
            "follow_ups": [str(f) for f in info.get("follow_ups") or []],
            "developer": developer,
        }
        if d.outcome != "success":
            d = d.model_copy(update={"resume_stage": resume.value})
            reason = d.reason or "The session ended without clearing the blocker."
            await self.publish_block(
                d,
                Status.RESOLVING,
                comments.unresolved(
                    reason=reason, ticket=ctx.key, next_steps=list(info.get("next_steps") or []), **report
                ),
            )
            return
        ctx.shared = ctx.shared.model_copy(
            update={
                "pause": None,
                "current_run_id": ctx.run_id,
                "current_stage": self.stage,
                "current_state": RunState.COMPLETED,
                "updated_at": utcnow(),
            }
        )
        await self.announce("resolved", comments.resolved(summary=d.reason, **report))
        await pub.save_record(ctx.key, ctx.shared, "resolved")
        await pub.set_resume_field(ctx.key, None, "clear")
        await pub.transition(ctx.key, Status.RESOLVING, RESOLVED_ACTIONS[resume])


STRATEGIES: dict[Stage, type[StageStrategy]] = {
    Stage.REFINEMENT: RefinementStage,
    Stage.PLANNING: PlanningStage,
    Stage.DEVELOPMENT: DevelopmentStage,
    Stage.VERIFICATION: VerificationStage,
    Stage.RELEASE_PREPARATION: ReleasePreparationStage,
    Stage.RELEASE_VERIFICATION: ReleaseVerificationStage,
    Stage.RESOLUTION: ResolutionStage,
}

__all__ = ["STRATEGIES", "OutputInvalid", "StageStrategy", "WorkerFailure", "commit_url"]
