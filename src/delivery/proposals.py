"""Tickets Claude proposes, created in Jira only when a person asks for them.

Refinement may find a ticket too big for one delivery and propose slices of it; a spike's
findings may propose the follow-up work. Each proposal (S1, S2...) is a short brief, published
next to the document that proposed it (``<revision>.proposed.json``) and listed in its review
comment. Nothing is created until someone comments ``CREATE TICKETS <token>`` (the token of that
specification or findings revision) followed by the IDs to create, alone or with a note each.
The coordinator then creates them in Backlog, unassigned, linked to the ticket that proposed
them, and replies with their keys. The proposing ticket carries on as it was: narrow it with a
change request, or cancel it if the new tickets replace it.

A note after an ID (``S2: call it Export``) is added to that ticket's description.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Container

from pydantic import ValidationError

from delivery.adf import markdown_to_adf
from delivery.config import Config
from delivery.feedback import GATE_TOKEN, Decision, DecisionKind, is_coordinator_comment, parse_decision
from delivery.git import ManagedRepo
from delivery.journal import RunJournal, atomic_write_json, ensure_private_dir
from delivery.models import ProposedTicket
from delivery.ports import IntegrationError, JiraComment, JiraIssue, JiraPort
from delivery.publication import Publisher

log = logging.getLogger("delivery")
FOLDERS = {"SPEC": "specification", "PLAN": "findings"}


def proposals_path(doc_root: str, folder: str, revision: int) -> str:
    return f"{doc_root}/{folder}/v{revision:03d}.proposed.json"


def dump(proposals: list[ProposedTicket]) -> str:
    return json.dumps([p.model_dump(mode="json") for p in proposals], indent=2, sort_keys=True) + "\n"


async def load(repo: ManagedRepo, key: str, token: str) -> list[ProposedTicket]:
    """The proposals published with the revision ``token`` names (none if it proposed none)."""
    m = GATE_TOKEN.match(token)
    if not m or m.group("kind") not in FOLDERS:
        return []
    path = proposals_path(f"docs/delivery/{key}", FOLDERS[m.group("kind")], int(m.group("rev")))
    data = await repo.show_file(f"origin/delivery/{key}", path)
    if data is None:
        return []
    try:
        return [ProposedTicket.model_validate(x) for x in json.loads(data)]
    except (ValueError, ValidationError):
        return []


class Proposals:
    def __init__(self, cfg: Config, jira: JiraPort, repo: ManagedRepo) -> None:
        self.cfg = cfg
        self.jira = jira
        self.repo = repo
        self.dir = cfg.runtime.state_dir / "proposals"

    def _done(self, key: str) -> dict[str, dict[str, str]]:
        try:
            return dict(json.loads((self.dir / f"{key}.json").read_text()))
        except (OSError, ValueError):
            return {}

    def _save(self, key: str, done: dict[str, dict[str, str]]) -> None:
        atomic_write_json(ensure_private_dir(self.dir) / f"{key}.json", done)

    async def collect(
        self, issue: JiraIssue, comments: list[JiraComment], allowed_authors: Container[str]
    ) -> list[str]:
        """Act on new ``CREATE TICKETS`` comments on ``issue``. Returns the keys created."""
        key = issue.key
        done = self._done(key)
        asked = [
            (c, d)
            for c in comments
            if (d := _create_request(c)) is not None
            and c.author_account_id in allowed_authors
            and done.get(c.id, {}).get("_replied") != "yes"
        ]
        created: list[str] = []
        for comment, decision in asked:
            record = done.setdefault(comment.id, {})
            proposals = {p.id: p for p in await load(self.repo, key, decision.token)}
            if not proposals:
                await self._reply(key, comment, f"`{decision.token}` proposed no tickets; none were created.")
                record["_replied"] = "yes"
                self._save(key, done)
                continue
            unknown = sorted(set(decision.items) - set(proposals))
            for sid in [s for s in decision.items if s in proposals]:
                if sid in record:
                    continue
                record[sid] = await self._create(issue, proposals[sid], decision.items[sid], decision.token)
                created.append(record[sid])
                self._save(key, done)  # one at a time: a crash never creates a ticket twice
            made = [f"{record[s]} ({s}: {proposals[s].summary})" for s in decision.items if s in record]
            text = (
                "**Created in Backlog**, unassigned and linked to this ticket: " + "; ".join(made) + "."
                if made
                else "No tickets were created."
            )
            if unknown:
                text += f" Not proposed in `{decision.token}`, so not created: {', '.join(unknown)}."
            text += (
                " This ticket carries on as it is: narrow it with a change request, or cancel it if the "
                "new tickets replace it."
            )
            await self._reply(key, comment, text)
            record["_replied"] = "yes"
            self._save(key, done)
        return created

    async def _create(self, parent: JiraIssue, p: ProposedTicket, note: str, token: str) -> str:
        """Create one proposed ticket (or find the one created before a crash)."""
        for link in parent.links:
            try:
                other = await self.jira.get_issue(link.other_key)
            except IntegrationError:
                continue
            if other.view.summary == p.summary:
                return other.key
        issue_type = p.issue_type or (
            "Story" if self.cfg.flow.kind_of(parent.view.issue_type) == "spike" else parent.view.issue_type
        )
        body = p.description.strip()
        if note:
            body += f"\n\nNote from the person who asked for this ticket: {note}"
        body += f"\n\nProposed in {parent.key} (`{token}`) and created when asked for there."
        new_key = await self.jira.create_issue(
            self.cfg.jira.project_key, issue_type, p.summary, markdown_to_adf(body), []
        )
        try:
            await self.jira.link_issues(self.cfg.flow.link_type, parent.key, new_key)
        except IntegrationError as exc:
            log.warning("could not link %s to %s: %s", new_key, parent.key, exc)
        return new_key

    async def _reply(self, key: str, comment: JiraComment, text: str) -> None:
        journal = RunJournal(ensure_private_dir(self.dir / key))
        pub = Publisher(self.cfg, self.jira, None, None, journal, f"proposals-{key}")
        await pub.comment(key, "created-tickets", text, comment.id)


def _create_request(comment: JiraComment) -> Decision | None:
    if is_coordinator_comment(comment):
        return None
    d = parse_decision(comment.body_text)
    if d is None or d.kind is not DecisionKind.CREATE_TICKETS or d.problems:
        return None
    return d


__all__ = ["FOLDERS", "Proposals", "dump", "load", "proposals_path"]
