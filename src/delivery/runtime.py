"""Shared runtime dependencies and per-run context."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import Field

from delivery.claude import ChildHandle, ClaudeRunner
from delivery.config import Config
from delivery.figma import FigmaPort
from delivery.git import ManagedRepo
from delivery.intake import Intake, TicketContext
from delivery.interactive import InteractiveRunner
from delivery.journal import JournalStore, RunJournal, ensure_private_dir
from delivery.models import (
    AttachmentRef,
    DesignRef,
    LinkedTicket,
    Model,
    RunRecord,
    SharedExecutionRecord,
    SkippedAttachment,
    SkippedDesign,
)
from delivery.ownership import RepoLocks
from delivery.plugin import PluginInfo
from delivery.ports import GitHubPort, JiraPort
from delivery.publication import Publisher
from delivery.resources import PortRegistry

ChildCallback = Callable[[str, "ChildHandle | None"], None]


@dataclass
class Deps:
    cfg: Config
    jira: JiraPort
    github: GitHubPort
    repo: ManagedRepo
    claude: ClaudeRunner | InteractiveRunner
    store: JournalStore
    plugin: PluginInfo
    locks: RepoLocks
    ports: PortRegistry = field(default_factory=PortRegistry)
    figma: FigmaPort | None = None


class Decision(Model):
    """The persisted outcome of a stage's work, published idempotently afterwards."""

    outcome: str  # success | clarification | blocked | verification_failed | cancelled
    reason: str = ""
    action: str = ""
    blocker_kind: str = ""
    resume_stage: str | None = None
    gate_token: str | None = None
    result: dict[str, Any] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


@dataclass
class RunContext:
    deps: Deps
    ticket: TicketContext
    intake: Intake
    record: RunRecord
    journal: RunJournal
    shared: SharedExecutionRecord
    on_child: ChildCallback | None = None
    worktrees: dict[str, Path] = field(default_factory=dict)
    stop_reason: str | None = None
    stop_hold: bool = False
    attachments: list[AttachmentRef] = field(default_factory=list)
    attachments_skipped: list[SkippedAttachment] = field(default_factory=list)
    designs: list[DesignRef] = field(default_factory=list)
    designs_skipped: list[SkippedDesign] = field(default_factory=list)
    linked: list[LinkedTicket] = field(default_factory=list)
    guidance: Path | None = None

    @property
    def key(self) -> str:
        return self.record.ticket_key

    @property
    def cfg(self) -> Config:
        return self.deps.cfg

    @property
    def run_id(self) -> str:
        return self.record.run_id

    @property
    def inputs_dir(self) -> Path:
        return ensure_private_dir(self.journal.dir / "inputs")

    @property
    def tmp_dir(self) -> Path:
        return self.journal.tmp_dir

    @property
    def logs_dir(self) -> Path:
        return self.journal.logs_dir

    def output_dir(self, procedure: str) -> Path:
        return ensure_private_dir(self.journal.dir / "output" / procedure)

    def worktree_path(self, name: str) -> Path:
        return self.cfg.repository.worktree_root / self.key / self.run_id / name

    @property
    def delivery_branch(self) -> str:
        return f"delivery/{self.key}"

    @property
    def feature_branch(self) -> str:
        return f"feature/{self.key}"

    @property
    def doc_root(self) -> str:
        return f"docs/delivery/{self.key}"

    def publisher(self) -> Publisher:
        return Publisher(
            self.cfg, self.deps.jira, self.deps.github, self.deps.repo, self.journal, self.run_id
        )

    def save(self, event: str | None = None, **data: Any) -> None:
        self.record = self.journal.save(self.record, event, **data)
