"""Stage strategies: prepare isolated inputs, run procedures, decide, publish.

``work`` returns a persisted :class:`Decision`; ``publish`` turns it into side effects
through the idempotent :class:`Publisher`. Publication can therefore be repeated after
a crash without duplicating comments, commits, PRs or transitions.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import shutil
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ValidationError

from delivery import comments
from delivery.checks import all_passed, run_checks
from delivery.claude import ClaudeInvocation, ClaudeOutcome, ClaudeStatus
from delivery.gates import (
    current_gate,
    evaluate_ci,
    gate_token,
    supersede_for_new_revision,
)
from delivery.git import blob_url, commit_url
from delivery.journal import atomic_write_json, ensure_private_dir
from delivery.models import (
    ArtefactPointer,
    ArtifactKind,
    Brief,
    CheckResult,
    Finding,
    Footprint,
    GateKind,
    GateRecord,
    InputEnvelope,
    Outcome,
    OutputContract,
    OverlapContext,
    PauseInfo,
    RunState,
    SelectedComment,
    Severity,
    SourceRefs,
    StageResult,
    digest,
    result_json_schema,
    utcnow,
)
from delivery.overlap import OverlapFinding
from delivery.overlap import Severity as OverlapSeverity
from delivery.permissions import PROCEDURE_ROLES, PROTECTED_WORKTREE_PATHS, build_profile
from delivery.publication import Posted
from delivery.resources import port_env
from delivery.runtime import Decision, RunContext
from delivery.workflow import Action, Stage, Status

MAX_OUTPUT_FILE_BYTES = 2_000_000


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


class OutputInvalid(Exception):
    pass


# --------------------------------------------------------------------------- validation


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
        dest = await self.copy_input(
            gate.artefact_commit, gate.artefact_path, f"approved/{Path(gate.artefact_path).name}"
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
            brief=brief,
            selected_comments=selected,
            clarification_round=ctx.intake.round_token,
            feedback_token=ctx.intake.feedback_token,
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

    # ------------------------------------------------------------------ procedures
    async def run_procedure(
        self,
        procedure: str,
        worktree: Path,
        envelope: InputEnvelope,
        *,
        ports: dict[str, int] | None = None,
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
        inv = ClaudeInvocation(
            run_id=ctx.run_id,
            procedure=procedure,
            envelope_path=env_path,
            cwd=worktree,
            plugin_dir=ctx.cfg.claude.plugin_path,
            schema=result_json_schema(),
            settings_path=settings_path,
            tools=profile.tools,
            add_dirs=profile.add_dirs,
            timeout=ctx.cfg.runtime.timeout_seconds,
            stdout_path=ctx.logs_dir / f"claude-{procedure}.jsonl",
            stderr_path=ctx.logs_dir / f"claude-{procedure}.stderr.log",
            max_turns=ctx.cfg.claude.max_turns,
            model=ctx.cfg.claude.model_for(procedure),
            extra_env={"TMPDIR": str(ctx.tmp_dir), **port_env(ports or {})},
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

        def on_start(proc: asyncio.subprocess.Process) -> None:
            if ctx.on_child:
                ctx.on_child(ctx.key, proc)
            from delivery.models import ChildProcess

            ctx.record = ctx.record.model_copy(
                update={
                    "child": ChildProcess(
                        pid=proc.pid,
                        pgid=proc.pid,
                        started_at=utcnow(),
                        argv0=ctx.cfg.claude.executable,
                        session_label=inv.session_id,
                    ),
                    "state": RunState.RUNNING,
                }
            )
            ctx.save("child_started", procedure=procedure, pid=proc.pid)

        try:
            outcome = await self.deps.claude.run(inv, on_start=on_start)
        finally:
            if ctx.on_child:
                ctx.on_child(ctx.key, None)
            ctx.record = ctx.record.model_copy(update={"child": None})
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
            return validate_result(
                outcome.structured,
                contract_id=ctx.deps.plugin.contracts[procedure],
                procedure=procedure,
                run_id=ctx.run_id,
                ticket=ctx.key,
                stage=self.stage,
                input_revision=ctx.record.input_revision or "",
            )
        except OutputInvalid as exc:
            so = outcome.structured or {}
            hint = (
                f" (worker reported: {str(so.get('blocker_reason') or so.get('summary'))[:300]})"
                if (so.get("outcome") in ("blocked", "failed"))
                else ""
            )
            raise WorkerFailure(procedure, outcome, f"output rejected: {exc}{hint}") from None

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

    async def publish_block(self, d: Decision, source: Status) -> None:
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
            "blocked", comments.blocked(self.stage.value, d.reason, d.action, resume.value), pause=True
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
        for name, path in list(self.ctx.record.worktrees.items()):
            try:
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
        return Decision(
            outcome=outcome,
            reason=result.summary,
            result=result.model_dump(mode="json"),
            extra={"revision": nxt},
        )

    async def publish(self, d: Decision) -> None:
        ctx = self.ctx
        if d.outcome == "blocked":
            await self.publish_block(d, Status.REFINING)
            return
        result = StageResult.model_validate(d.result)
        rev = int(d.extra["revision"])
        out = ctx.output_dir("refine-ticket")
        spec_rel = f"{ctx.doc_root}/specification/v{rev:03d}.md"
        header = provenance_header(
            ctx,
            "specification",
            f"v{rev:03d}",
            {
                "status": "draft with open questions" if d.outcome == "clarification" else "for review",
                "selected_comments": ",".join(ctx.record.selected_comment_ids) or "none",
            },
        )
        files = {
            spec_rel: header + (out / "specification.md").read_text(),
            f"{ctx.doc_root}/executions/{ctx.run_id}.json": self.execution_summary(
                d, {"artefact": spec_rel, "questions": [q.id for q in result.questions]}
            ),
        }
        sha = await self.publish_files(files, "spec", f"v{rev}", f"{ctx.key}: specification v{rev:03d}")
        url = blob_url(ctx.cfg.repository.url, sha, spec_rel)
        pub = ctx.publisher()
        shared = ctx.shared.model_copy(
            update={
                "spec_revision": rev,
                "artefacts": {**ctx.shared.artefacts, "specification": f"{spec_rel}@{sha}"},
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
                comments.questions(
                    token, url, result.questions, "the assignee or an approver", self.stage.value
                ),
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
            comments.spec_gate(token, url, rev, result.summary, "authorised approvers"),
            f"v{rev}",
            gate_tokens=(token,),
        )
        await pub.save_record(ctx.key, ctx.shared, "spec-gate")
        await pub.set_resume_field(ctx.key, None, "clear")
        await pub.transition(ctx.key, Status.REFINING, Action.COMPLETE_REFINEMENT)


# --------------------------------------------------------------------------- planning


class PlanningStage(StageStrategy):
    stage = Stage.PLANNING

    async def work(self) -> Decision:
        ctx = self.ctx
        wt = await self.delivery_worktree()
        spec = await self.approved_input(GateKind.SPEC, ArtifactKind.SPECIFICATION)
        if spec is None:
            return Decision(
                outcome="blocked",
                reason="approved specification could not be read",
                action="Check the delivery branch and resume.",
                blocker_kind="missing_input",
            )
        revs = await self.revisions("plan")
        nxt = max([*revs, ctx.shared.plan_revision, 0]) + 1
        prior = []
        if revs:
            path = f"{ctx.doc_root}/plan/v{max(revs):03d}.md"
            dest = await self.copy_input(f"origin/{ctx.delivery_branch}", path, f"prior/{Path(path).name}")
            if dest:
                prior.append(ArtefactPointer(kind=ArtifactKind.PLAN, path=str(dest), revision=max(revs)))
        base = await self.deps.repo.remote_sha(ctx.cfg.repository.base_branch)
        related = await self.related_work()
        out = ctx.output_dir("plan-ticket")
        env = self.envelope(
            "plan-ticket",
            out,
            required=[ArtifactKind.PLAN],
            next_revision=nxt,
            approved=[spec],
            prior=prior,
            related=related,
            source=self.source_refs(base_commit=base),
        )
        result = await self.run_procedure("plan-ticket", wt, env)
        if (d := self.worker_decision(result)) is not None:
            return d
        require_artifact(result, out, ArtifactKind.PLAN, "plan.md")
        if result.outcome is Outcome.COMPLETED and result.footprint is None:
            raise WorkerFailure("plan-ticket", None, "completed plan has no change footprint")
        for adr in [a for a in result.artifacts if a.kind is ArtifactKind.ARCHITECTURE]:
            safe_output_file(out, adr.path)
        outcome = "clarification" if result.outcome is Outcome.NEEDS_CLARIFICATION else "success"
        extra: dict[str, Any] = {"revision": nxt, "base": base}
        if result.footprint:
            fp = Footprint(
                ticket_key=ctx.key,
                owner_account_id=ctx.cfg.identity.developer_jira_account_id,
                stage=self.stage,
                plan_revision=nxt,
                source_commit=base or "",
                published_at=utcnow(),
                **result.footprint.model_dump(),
            )
            from delivery.coordination import Coordinator

            findings = await Coordinator(self.deps).check(fp, ctx.shared, checkpoint="plan")
            extra["footprint"] = fp.model_dump(mode="json")
            extra["overlap"] = findings_json(findings)
        return Decision(
            outcome=outcome,
            reason=result.summary,
            result=result.model_dump(mode="json"),
            extra=extra,
        )

    async def related_work(self) -> list[OverlapContext]:
        from delivery.coordination import Coordinator

        return await Coordinator(self.deps).related_contexts(self.ctx.key)

    async def publish(self, d: Decision) -> None:
        ctx = self.ctx
        if d.outcome == "blocked":
            await self.publish_block(d, Status.PLANNING)
            return
        result = StageResult.model_validate(d.result)
        rev = int(d.extra["revision"])
        out = ctx.output_dir("plan-ticket")
        plan_rel = f"{ctx.doc_root}/plan/v{rev:03d}.md"
        fp_rel = f"{ctx.doc_root}/plan/v{rev:03d}.footprint.json"
        spec_gate = current_gate(ctx.shared.gates, GateKind.SPEC)
        header = provenance_header(
            ctx,
            "plan",
            f"v{rev:03d}",
            {
                "approved_specification": spec_gate.token if spec_gate else "none",
                "source_commit": d.extra.get("base"),
            },
        )
        files = {plan_rel: header + (out / "plan.md").read_text()}
        for i, adr in enumerate([a for a in result.artifacts if a.kind is ArtifactKind.ARCHITECTURE], 1):
            files[f"{ctx.doc_root}/architecture/adr-{rev:03d}-{i}.md"] = (
                provenance_header(ctx, "architecture", f"v{rev:03d}", {}) + (out / adr.path).read_text()
            )
        if d.extra.get("footprint"):
            files[fp_rel] = json.dumps(d.extra["footprint"], indent=2, sort_keys=True) + "\n"
        files[f"{ctx.doc_root}/executions/{ctx.run_id}.json"] = self.execution_summary(
            d, {"artefact": plan_rel, "overlap": d.extra.get("overlap", [])}
        )
        sha = await self.publish_files(files, "plan", f"v{rev}", f"{ctx.key}: plan v{rev:03d}")
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
                comments.questions(
                    token, url, result.questions, "the assignee or an approver", self.stage.value
                ),
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
            comments.plan_gate(
                token,
                url,
                blob_url(ctx.cfg.repository.url, sha, fp_rel),
                rev,
                result.summary,
                "authorised approvers",
                overlap,
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


class DevelopmentStage(StageStrategy):
    stage = Stage.DEVELOPMENT

    async def preflight(self) -> Decision | None:
        """Overlap checkpoint 2: immediately before implementation begins."""
        from delivery.coordination import Coordinator

        coord = Coordinator(self.deps)
        fp = await coord.own_footprint(self.ctx.shared)
        if fp is None:
            return None
        findings = await coord.check(fp, self.ctx.shared, checkpoint="pre-implementation")
        await coord.publish_warnings(self.ctx, findings)
        blocking = coord.unresolved_blocks(findings, self.ctx)
        if blocking:
            f = blocking[0]
            return Decision(
                outcome="blocked",
                reason=f"sequencing decision needed with {f.other}: {f.kind.value} "
                f"({', '.join(f.details[:3])})",
                action=f"Comment `OVERLAP {f.warning_id} PROCEED`, `WAIT {f.other}` or `RESCOPE`, "
                "then resume development.",
                blocker_kind="overlap_dependency",
                extra={"overlap": findings_json(blocking)},
            )
        return None

    async def work(self) -> Decision:
        ctx = self.ctx
        repo = self.deps.repo
        base = ctx.cfg.repository.base_branch
        wt = ctx.worktree_path("feature")
        if not wt.exists():
            exists = await repo.remote_sha(ctx.feature_branch)
            start = f"origin/{ctx.feature_branch}" if exists else f"origin/{base}"
            await repo.add_worktree(wt, start=start, branch=ctx.feature_branch)
            ctx.record.worktrees["feature"] = str(wt)
            base_sha = await repo.remote_sha(base)
            if exists and base_sha and not await repo.is_ancestor(base_sha, await repo.worktree_head(wt)):
                merged = await repo.merge(wt, f"origin/{base}", f"{ctx.key}: merge {base} into candidate")
                if not merged.ok:
                    return Decision(
                        outcome="blocked",
                        reason=f"merging the latest {base} conflicts in: {', '.join(merged.conflicts)}",
                        action="A human must decide how the conflicting changes combine (resolve on "
                        "the feature branch or revise scope), then resume development.",
                        blocker_kind="merge_conflict",
                    )
        start_sha = ctx.record.outputs.get("start_sha") or await repo.worktree_head(wt)
        ctx.record.outputs["start_sha"] = start_sha
        ctx.save("worktree_ready", start_sha=start_sha)
        spec = await self.approved_input(GateKind.SPEC, ArtifactKind.SPECIFICATION)
        plan = await self.approved_input(GateKind.PLAN, ArtifactKind.PLAN)
        if spec is None or plan is None:
            return Decision(
                outcome="blocked",
                reason="approved specification or plan could not be read",
                action="Check the delivery branch and resume.",
                blocker_kind="missing_input",
            )
        if ctx.intake.feedback_items:
            (ctx.inputs_dir / "feedback.json").write_text(
                json.dumps(ctx.intake.feedback_items, indent=2, sort_keys=True)
            )
        ports = ctx.record.ports or self.deps.ports.allocate(ctx.run_id)
        ctx.record = ctx.record.model_copy(update={"ports": ports})
        out = ctx.output_dir("implement-ticket")
        env = self.envelope(
            "implement-ticket",
            out,
            required=[],
            approved=[spec, plan],
            ports=ports,
            source=self.source_refs(feature_commit=start_sha),
            write_globs=[f"{wt}/**", f"{out}/**"],
        )
        result = await self.run_procedure("implement-ticket", wt, env, ports=ports)
        if (d := self.worker_decision(result)) is not None:
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
                comments.questions(
                    token, pr_url, result.questions, "the assignee or an approver", self.stage.value
                ),
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
            sha = await self.deps.repo.worktree_head(wt)
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
        fp_ref.update({"actual_paths": changed[:200], "candidate_sha": sha})
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
            ctx.key, "candidate", comments.candidate_ready(n, sha, pr.url, result.summary), f"c{n}"
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
        spec = await self.approved_input(GateKind.SPEC, ArtifactKind.SPECIFICATION)
        plan = await self.approved_input(GateKind.PLAN, ArtifactKind.PLAN)
        approved = [a for a in (spec, plan) if a]
        diff = await repo.git("diff", f"{base_sha}...{candidate}")
        (ctx.inputs_dir / "candidate.diff").write_text(diff.stdout)
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
        conflicts: list[str] = []
        res = await repo.merge(integration_wt, base_sha, f"integration: {base} into {candidate[:12]}")
        if not res.ok:
            conflicts.append(f"{base}: {', '.join(res.conflicts)}")
        for other_key, other_sha in interacting:
            r2 = await repo.merge(integration_wt, other_sha, f"integration: {other_key}")
            if r2.ok:
                merged_with.append(f"{other_key}@{other_sha[:12]}")
            else:
                conflicts.append(f"{other_key}: {', '.join(r2.conflicts)}")
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

        reasons: list[str] = []
        failed = [c for c in all_checks if c.conclusion != "passed"]
        if failed:
            reasons += [f"coordinator check {c.name} ({c.target}) {c.conclusion}" for c in failed]
        if conflicts:
            reasons.append("integration merge conflicts: " + "; ".join(conflicts))
        if ci and not ci.ok and not ci.pending:
            reasons.append("CI failed: " + "; ".join(ci.problems[:5]))
        if provenance["state"] == "mismatch":
            reasons.append(f"CI integration provenance does not match the candidate: {provenance['detail']}")
        findings = [
            *review.findings,
            *[f.model_copy(update={"id": f"F{100 + i}"}) for i, f in enumerate(verify.findings, 1)],
        ]
        serious = [f for f in findings if f.severity in (Severity.BLOCKER, Severity.MAJOR)]
        if serious:
            reasons.append(f"{len(serious)} blocker/major findings")
        not_met = sorted(
            {e.criterion_id for e in [*review.evidence, *verify.evidence] if e.status == "not_met"}
        )
        if not_met:
            reasons.append(f"criteria not met: {', '.join(not_met)}")
        verified = {e.criterion_id for e in verify.evidence if e.status == "met"}
        unverified = sorted({e.criterion_id for e in [*review.evidence, *verify.evidence]} - verified)
        extra = {
            "candidate": candidate,
            "base_sha": base_sha,
            "integration_tree": tree,
            "integration_head": int_head,
            "integration_with": merged_with,
            "ci": [c.model_dump(mode="json") for c in ci_results],
            "ci_pending": bool(ci and ci.pending),
            "ci_provenance": provenance,
            "findings": [f.model_dump(mode="json") for f in findings],
            "unverified": unverified,
            "overlap": findings_json(overlap),
            "review": review.model_dump(mode="json"),
            "verify": verify.model_dump(mode="json"),
        }
        outcome = "verification_failed" if reasons else "success"
        return Decision(
            outcome=outcome,
            reason="; ".join(reasons) or "verification passed",
            result=verify.model_dump(mode="json"),
            extra=extra,
        )

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
        meta = {
            "candidate_sha": candidate,
            "base_sha": d.extra.get("base_sha"),
            "integration_tree": d.extra.get("integration_tree"),
            "integration_with": ",".join(d.extra.get("integration_with", [])) or "base only",
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
                    **meta,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            f"{ctx.doc_root}/executions/{ctx.run_id}.json": self.execution_summary(d, meta),
        }
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
        base = {
            "current_run_id": ctx.run_id,
            "current_stage": self.stage,
            "updated_at": utcnow(),
            "artefacts": {
                **ctx.shared.artefacts,
                "review": f"{rdir}/review.md@{sha}",
                "verification": f"{rdir}/verification.md@{sha}",
            },
        }
        if d.outcome == "verification_failed":
            ctx.shared = ctx.shared.model_copy(
                update={
                    **base,
                    "current_state": RunState.AWAITING_HUMAN,
                    "pending_feedback": [
                        {"id": f.id, "description": f.description, "severity": f.severity.value}
                        for f in findings
                    ]
                    + [{"id": f"R{i}", "description": r} for i, r in enumerate(d.reason.split("; "), 1)],
                }
            )
            await self.announce(
                "verification-failed",
                comments.verification_failed(
                    code_token,
                    pr_url,
                    candidate,
                    review_url,
                    verify_url,
                    checks,
                    findings,
                    d.reason.split("; "),
                ),
                candidate[:12],
            )
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
        await self.announce(
            "code-gate",
            comments.code_gate(
                code_token,
                accept_token,
                pr_url,
                candidate,
                review_url,
                verify_url,
                checks,
                [f for f in findings if f.severity not in (Severity.BLOCKER, Severity.MAJOR)],
                d.extra.get("unverified", []),
                overlap,
                ", ".join(ctx.cfg.approvals.github_logins) or "an independent reviewer",
                "{state}: {detail}".format(
                    **{"state": "missing", "detail": "", **d.extra.get("ci_provenance", {})}
                ),
            ),
            f"c{n}",
            gate_tokens=(code_token, accept_token),
        )
        await pub.save_record(ctx.key, ctx.shared, "code-gate")
        await pub.transition(ctx.key, Status.VERIFYING, Action.COMPLETE_VERIFICATION)


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
        if pr.merged:
            return Decision(
                outcome="blocked",
                reason="the PR was merged before release approval",
                action="Investigate the bypassed gate; the coordinator will not continue.",
                blocker_kind="gate_bypassed",
            )
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
        wt = await self.detached_worktree("release", candidate)
        approved = [
            a
            for a in (
                await self.approved_input(GateKind.SPEC, ArtifactKind.SPECIFICATION),
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
            extra={"revision": nxt, "candidate": candidate},
        )

    async def publish(self, d: Decision) -> None:
        ctx = self.ctx
        pub = ctx.publisher()
        if d.outcome == "blocked":
            await self.publish_block(d, Status.PREPARING_RELEASE)
            return
        rev = int(d.extra["revision"])
        rel = f"{ctx.doc_root}/releases/v{rev:03d}.md"
        header = provenance_header(ctx, "release", f"v{rev:03d}", {"candidate_sha": d.extra["candidate"]})
        files = {
            rel: header + (ctx.output_dir("prepare-release") / "release.md").read_text(),
            f"{ctx.doc_root}/executions/{ctx.run_id}.json": self.execution_summary(d, {"artefact": rel}),
        }
        sha = await self.publish_files(files, "release", f"v{rev}", f"{ctx.key}: release proposal v{rev:03d}")
        token = gate_token(ctx.key, GateKind.RELEASE, rev)
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
        ctx.shared = ctx.shared.model_copy(
            update={
                "release_revision": rev,
                "gates": supersede_for_new_revision(ctx.shared.gates, GateKind.RELEASE, token) + [gate],
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
                token,
                blob_url(ctx.cfg.repository.url, sha, rel),
                rev,
                d.extra["candidate"],
                "authorised approvers",
                ctx.cfg.release.environment,
            ),
            f"v{rev}",
            gate_tokens=(token,),
        )
        await pub.save_record(ctx.key, ctx.shared, "release-gate")
        await pub.transition(ctx.key, Status.PREPARING_RELEASE, Action.COMPLETE_RELEASE_PREPARATION)


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
        if pr.head_sha != candidate:
            return (
                False,
                (
                    f"merged PR head {pr.head_sha[:12]} is not the approved candidate "
                    f"{candidate[:12]} (unapproved changes merged)"
                ),
                info,
            )
        if not await repo.resolve(released):
            return False, f"recorded commit {released[:12]} does not exist in the repository", info
        base_sha = await repo.remote_sha(ctx.cfg.repository.base_branch) or ""
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
                str(d.extra.get("provenance_summary", "")),
            ),
            release_id,
        )
        await pub.set_resume_field(ctx.key, None, "clear")
        await pub.transition(ctx.key, Status.VERIFYING_RELEASE, Action.COMPLETE_RELEASE_VERIFICATION)


STRATEGIES: dict[Stage, type[StageStrategy]] = {
    Stage.REFINEMENT: RefinementStage,
    Stage.PLANNING: PlanningStage,
    Stage.DEVELOPMENT: DevelopmentStage,
    Stage.VERIFICATION: VerificationStage,
    Stage.RELEASE_PREPARATION: ReleasePreparationStage,
    Stage.RELEASE_VERIFICATION: ReleaseVerificationStage,
}

__all__ = ["STRATEGIES", "OutputInvalid", "StageStrategy", "WorkerFailure", "commit_url"]
