"""Context from linked tickets.

A ticket rarely stands alone: a bug is found in a story that was delivered last month, a story
continues an earlier one, a slice was split from a bigger ticket. For each ticket linked to this
one in Jira (any link type, up to ``MAX_LINKED``), Claude gets the link as Jira words it, the
linked ticket's summary, type, status and description, and, when it went through delivery, its
approved specification and plan (copied into the run's read-only inputs from the repository),
its pull request and the commit it was released in.

Nothing here is required: a linked ticket that cannot be read is left out and the run carries on.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError

from delivery.gates import approved_gate
from delivery.git import ManagedRepo, blob_url
from delivery.journal import ensure_private_dir
from delivery.models import (
    PROPERTY_KEY,
    ArtefactPointer,
    ArtifactKind,
    GateKind,
    LinkedTicket,
    SharedExecutionRecord,
)
from delivery.ports import IntegrationError, JiraIssue, JiraPort

log = logging.getLogger("delivery")
MAX_LINKED = 8
MAX_DESCRIPTION = 4000
DOCUMENTS = ((GateKind.SPEC, ArtifactKind.SPECIFICATION), (GateKind.PLAN, ArtifactKind.PLAN))


async def linked_tickets(
    jira: JiraPort, repo: ManagedRepo, repo_url: str, issue: JiraIssue, dest: Path
) -> list[LinkedTicket]:
    out: list[LinkedTicket] = []
    seen: set[str] = set()
    for link in issue.links:
        if link.other_key in seen or link.other_key == issue.key or len(out) >= MAX_LINKED:
            continue
        seen.add(link.other_key)
        try:
            other = await jira.get_issue(link.other_key)
        except IntegrationError as exc:
            log.info("linked ticket %s not readable: %s", link.other_key, exc)
            continue
        rec = await _record(jira, other.key)
        docs: list[ArtefactPointer] = []
        if rec is not None:
            for kind, art in DOCUMENTS:
                gate = approved_gate(rec.gates, kind)
                if gate is None or not gate.artefact_path or not gate.artefact_commit:
                    continue
                data = await repo.show_file(gate.artefact_commit, gate.artefact_path)
                if data is None:
                    continue
                path = ensure_private_dir(dest) / f"{other.key}-{art.value}-v{gate.revision:03d}.md"
                path.write_bytes(data)
                docs.append(
                    ArtefactPointer(
                        kind=art,
                        path=str(path),
                        revision=gate.revision,
                        commit=gate.artefact_commit,
                        url=blob_url(repo_url, gate.artefact_commit, gate.artefact_path),
                    )
                )
        released = (rec.release.get("record") or {}).get("commit") if rec else None
        out.append(
            LinkedTicket(
                key=other.key,
                relation=link.description or link.link_type,
                summary=other.view.summary,
                issue_type=other.view.issue_type,
                status=other.view.status_name,
                description=other.description_text[:MAX_DESCRIPTION],
                documents=docs,
                pull_request=f"{repo_url.removesuffix('.git')}/pull/{rec.pr_number}"
                if rec and rec.pr_number
                else None,
                released_commit=str(released) if released else None,
            )
        )
    return out


async def _record(jira: JiraPort, key: str) -> SharedExecutionRecord | None:
    try:
        raw = await jira.get_property(key, PROPERTY_KEY)
    except IntegrationError:
        return None
    if not raw:
        return None
    try:
        return SharedExecutionRecord.model_validate(raw)
    except ValidationError:
        return None


__all__ = ["MAX_LINKED", "linked_tickets"]
