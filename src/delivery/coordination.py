"""Cross-ticket coordination: footprints of every in-flight ticket, any assignee.

The supervisor executes only its developer's tickets, but reads all active tickets in
the project and their published footprints so overlapping work is visible to everyone.
Warnings use stable IDs and are deduplicated on both tickets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from delivery import comments
from delivery.feedback import DecisionKind, decisions
from delivery.models import PROPERTY_KEY, Footprint, OverlapContext, SharedExecutionRecord, utcnow
from delivery.overlap import OverlapFinding, OverlapKind, Severity, compare, warning_id
from delivery.ownership import coordination_jql
from delivery.ports import IntegrationError, JiraIssue
from delivery.workflow import TERMINAL_STATUSES

if TYPE_CHECKING:
    from delivery.runtime import Deps, RunContext


class Coordinator:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps
        self.cfg = deps.cfg

    async def in_flight(self, exclude: str) -> list[tuple[JiraIssue, SharedExecutionRecord | None]]:
        out: list[tuple[JiraIssue, SharedExecutionRecord | None]] = []
        for issue in await self.deps.jira.search(coordination_jql(self.cfg)):
            if issue.key == exclude:
                continue
            raw = await self.deps.jira.get_property(issue.key, PROPERTY_KEY)
            rec = None
            if raw:
                try:
                    rec = SharedExecutionRecord.model_validate(raw)
                except ValidationError:
                    rec = None
            out.append((issue, rec))
        return out

    async def footprint_of(self, rec: SharedExecutionRecord | None) -> Footprint | None:
        if rec is None or not rec.footprint_ref:
            return None
        ref = rec.footprint_ref
        data = await self.deps.repo.show_file(str(ref.get("commit")), str(ref.get("path")))
        if data is None:
            await self.deps.repo.fetch()
            data = await self.deps.repo.show_file(str(ref.get("commit")), str(ref.get("path")))
        if data is None:
            return None
        try:
            fp = Footprint.model_validate_json(data)
        except ValidationError:
            return None
        return fp.model_copy(
            update={
                "actual_paths": list(ref.get("actual_paths") or []),
                "actual_commit": ref.get("candidate_sha"),
            }
        )

    async def own_footprint(self, shared: SharedExecutionRecord) -> Footprint | None:
        return await self.footprint_of(shared)

    async def related_contexts(self, key: str) -> list[OverlapContext]:
        out = []
        for issue, rec in await self.in_flight(key):
            fp = await self.footprint_of(rec)
            out.append(
                OverlapContext(
                    ticket_key=issue.key,
                    assignee=issue.assignee_name or issue.view.assignee_account_id,
                    status=issue.view.status_name,
                    footprint_path=(rec.footprint_ref or {}).get("path") if rec else None,
                    footprint=fp.model_dump(mode="json") if fp else None,
                )
            )
        return out

    async def _is_done(self, key: str) -> bool:
        try:
            issue = await self.deps.jira.get_issue(key)
        except IntegrationError:
            return False
        status = self.cfg.status_by_id().get(issue.view.status_id)
        return status in TERMINAL_STATUSES

    async def check(
        self,
        fp: Footprint,
        shared: SharedExecutionRecord,
        *,
        checkpoint: str,
        use_actual: bool = False,
    ) -> list[OverlapFinding]:
        findings: list[OverlapFinding] = []
        seen: set[str] = set()
        for issue, rec in await self.in_flight(fp.ticket_key):
            other = await self.footprint_of(rec)
            if other is None:
                continue
            for f in compare(
                fp,
                other,
                other_assignee=issue.assignee_name or issue.view.assignee_account_id,
                low_signal_paths=self.cfg.overlap.low_signal_paths,
                use_actual=use_actual,
            ):
                if f.warning_id not in seen:
                    seen.add(f.warning_id)
                    findings.append(f)
        # Declared dependencies via Jira "is blocked by" links and the plan's own list.
        mine = await self.deps.jira.get_issue(fp.ticket_key)
        blockers = {link.other_key for link in mine.links if "blocked by" in link.description.lower()}
        blockers |= set(fp.ticket_dependencies)
        for other_key in sorted(blockers):
            if await self._is_done(other_key):
                continue
            details = (f"{fp.ticket_key} depends on {other_key}",)
            tickets = tuple(sorted((fp.ticket_key, other_key)))
            wid = warning_id(OverlapKind.DECLARED_DEPENDENCY, tickets, details)  # type: ignore[arg-type]
            if wid not in seen:
                seen.add(wid)
                findings.append(
                    OverlapFinding(
                        wid,
                        OverlapKind.DECLARED_DEPENDENCY,
                        Severity.BLOCK,
                        fp.ticket_key,
                        other_key,
                        None,
                        details,
                        {},
                    )
                )
        return findings

    def unresolved_blocks(self, findings: list[OverlapFinding], ctx: RunContext) -> list[OverlapFinding]:
        humans = set(self.cfg.approvals.jira_account_ids) | {self.cfg.identity.developer_jira_account_id}
        unresolved = []
        for f in findings:
            if f.severity is not Severity.BLOCK:
                continue
            ds = [
                cd
                for cd in decisions(ctx.ticket.comments, token=f.warning_id, kinds={DecisionKind.OVERLAP})
                if cd.comment.author_account_id in humans
            ]
            choice = ds[-1].decision.choice if ds else ctx.shared.overlap_decisions.get(f.warning_id, "")
            if choice:
                ctx.shared = ctx.shared.model_copy(
                    update={"overlap_decisions": {**ctx.shared.overlap_decisions, f.warning_id: choice}}
                )
            if choice.upper() != "PROCEED":
                unresolved.append(f)
        return unresolved

    async def interacting_candidates(self, key: str, shared: SharedExecutionRecord) -> list[tuple[str, str]]:
        """Other tickets with published candidates whose footprints overlap this ticket."""
        mine = await self.own_footprint(shared)
        if mine is None:
            return []
        out = []
        for issue, rec in await self.in_flight(key):
            other = await self.footprint_of(rec)
            if other is None or not other.actual_commit:
                continue
            # Planned or actual overlap both count as a known interaction.
            low = self.cfg.overlap.low_signal_paths
            overlap = compare(mine, other, low_signal_paths=low) + compare(
                mine, other, low_signal_paths=low, use_actual=True
            )
            if overlap:
                out.append((issue.key, str(other.actual_commit)))
        return sorted(out)

    async def publish_warnings(self, ctx: RunContext, findings: list[OverlapFinding]) -> None:
        """Post each warning once per ticket, on this ticket and mirrored on the other."""
        if not findings:
            return
        pub = ctx.publisher()
        assignees = {}
        for issue, _ in await self.in_flight(ctx.key):
            assignees[issue.key] = issue.assignee_name or issue.view.assignee_account_id
        posted = set(ctx.shared.overlap_warnings)
        for f in findings:
            body = comments.overlap_warning(f, assignees, ctx.key)
            if f.warning_id not in posted and not await self._has_warning(ctx.key, f.warning_id):
                await pub.comment(ctx.key, "overlap", body, f.warning_id)
            posted.add(f.warning_id)
            other = f.other if f.ticket == ctx.key else f.ticket
            try:
                if not await self._has_warning(other, f.warning_id):
                    await pub.comment(
                        other,
                        "overlap-mirror",
                        comments.overlap_warning(f, {**assignees, ctx.key: "this ticket's owner"}, other),
                        f"{f.warning_id}:{other}",
                    )
            except Exception as exc:
                ctx.journal.events.append("overlap_mirror_failed", {"ticket": other, "error": str(exc)})
        ctx.shared = ctx.shared.model_copy(update={"overlap_warnings": sorted(posted)})
        ctx.journal.events.append(
            "overlap_checked",
            {"at": utcnow().isoformat(), "warnings": [f.warning_id for f in findings]},
        )

    async def _has_warning(self, key: str, wid: str) -> bool:
        return any(
            wid in c.body_text and "delivery-op:" in c.body_text for c in await self.deps.jira.comments(key)
        )
