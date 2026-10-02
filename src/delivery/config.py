"""The single human-edited local configuration file.

Rules from the handoff: one explicitly selected file, unknown keys rejected, relative
paths resolved against the config file (never the caller's working directory), and
secrets only ever referenced by environment variable name.

There is deliberately no setting that limits the number of concurrent sessions.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from delivery.workflow import DEFAULT_ACTION_NAMES, Action, Status

ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
CHECK_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
ACCOUNT_ID = re.compile(r"^[A-Za-z0-9:_-]{6,128}$")
GITHUB_REPO_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


class ConfigError(Exception):
    def __init__(self, path: Path, problems: list[str]) -> None:
        self.path = path
        self.problems = problems
        super().__init__(f"{path}: " + "; ".join(problems))


MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,99}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _resolve_path(value: Path, info: ValidationInfo) -> Path:
    base = (info.context or {}).get("base_dir")
    p = value.expanduser()
    if not p.is_absolute():
        if base is None:
            raise ValueError("relative path needs a config file location")
        p = Path(base) / p
    return Path(_normalise(p))


def _normalise(p: Path) -> str:
    # Lexical normalisation only: paths may not exist yet.
    parts: list[str] = []
    for part in p.parts:
        if part == "..":
            if len(parts) > 1:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return str(Path(*parts))


ResolvedPath = Annotated[Path, Field()]


class IdentityConfig(StrictModel):
    developer_jira_account_id: str
    worker_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")

    @field_validator("developer_jira_account_id")
    @classmethod
    def _account(cls, v: str) -> str:
        if not ACCOUNT_ID.match(v):
            raise ValueError("must be a Jira account ID, not a display name or email")
        return v


class JiraFieldsConfig(StrictModel):
    resume_stage: str | None = Field(default=None, pattern=r"^customfield_\d+$")


class JiraConfig(StrictModel):
    base_url: str
    project_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,9}$")
    supported_issue_types: list[str] = Field(min_length=1)
    required_label: str = ""
    auth_profile: Literal["site_api_token", "scoped_api_token"] = "site_api_token"
    email_env: str = "JIRA_EMAIL"
    token_env: str = "JIRA_API_TOKEN"  # noqa: S105 - an env var name, not a secret
    # The account email is not a secret; the environment variable wins when both are set.
    email: str = ""
    # macOS Keychain service holding the API token (account = email). Never the token itself.
    token_keychain_service: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,100}$")
    cloud_id: str | None = None
    fields: JiraFieldsConfig = JiraFieldsConfig()

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        u = urlparse(v)
        if u.scheme != "https" or not u.netloc or u.path not in ("", "/") or u.query:
            raise ValueError("must be an https site URL such as https://example.atlassian.net")
        return f"https://{u.netloc}"

    @field_validator("email_env", "token_env")
    @classmethod
    def _env(cls, v: str) -> str:
        if not ENV_NAME.match(v):
            raise ValueError("must be an environment variable name, not a secret value")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if v and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("must be the Atlassian account email address")
        return v

    @field_validator("required_label")
    @classmethod
    def _label(cls, v: str) -> str:
        if v and not re.match(r"^[A-Za-z0-9_.-]{1,255}$", v):
            raise ValueError("Jira labels cannot contain spaces")
        return v

    @model_validator(mode="after")
    def _scoped(self) -> JiraConfig:
        if self.auth_profile == "scoped_api_token" and not self.cloud_id:
            raise ValueError("scoped_api_token requires jira.cloud_id (gateway URL uses it)")
        return self


class RepositoryConfig(StrictModel):
    url: str
    base_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]{1,200}$")
    checkout_path: Path
    worktree_root: Path
    github_auth_profile: Literal["gh"] = "gh"

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not GITHUB_REPO_URL.match(v):
            raise ValueError("must be https://github.com/<owner>/<repo>[.git]")
        return v

    @field_validator("checkout_path", "worktree_root", mode="after")
    @classmethod
    def _paths(cls, v: Path, info: ValidationInfo) -> Path:
        return _resolve_path(v, info)

    @property
    def owner(self) -> str:
        m = GITHUB_REPO_URL.match(self.url)
        assert m
        return m.group("owner")

    @property
    def name(self) -> str:
        m = GITHUB_REPO_URL.match(self.url)
        assert m
        return m.group("repo")

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


class RuntimeConfig(StrictModel):
    state_dir: Path
    poll_seconds: int = Field(default=60, ge=10, le=3600)
    poll_jitter_seconds: int = Field(default=10, ge=0, le=300)
    timeout_seconds: int = Field(default=1800, ge=60, le=86400)
    heartbeat_seconds: int = Field(default=30, ge=5, le=600)
    check_timeout_seconds: int = Field(default=1800, ge=30, le=86400)

    @field_validator("state_dir", mode="after")
    @classmethod
    def _paths(cls, v: Path, info: ValidationInfo) -> Path:
        return _resolve_path(v, info)


class ClaudeConfig(StrictModel):
    executable: str = "claude"
    plugin_path: Path
    auth_profile: Literal["subscription"] = "subscription"
    max_turns: int | None = Field(default=40, ge=1, le=500)
    allow_paid_api_fallback: bool = False
    permission_profile: Literal["local_pilot"] = "local_pilot"
    # Default model for every procedure; None means Claude Code's own default.
    model: str | None = None
    # Per-procedure overrides, e.g. {"implement-ticket": "opus"} (TOML table [claude.models]).
    models: dict[str, str] = Field(default_factory=dict)
    supported_versions: str = ">=2.1.0,<3"

    @field_validator("model")
    @classmethod
    def _model(cls, v: str | None) -> str | None:
        if v is not None and not MODEL_NAME.match(v):
            raise ValueError("must be a Claude model name or alias such as opus or claude-sonnet-5")
        return v

    @field_validator("models")
    @classmethod
    def _models(cls, v: dict[str, str]) -> dict[str, str]:
        from delivery.plugin import PROCEDURES

        unknown = sorted(set(v) - set(PROCEDURES))
        if unknown:
            raise ValueError(f"unknown procedures {unknown}; use one of {list(PROCEDURES)}")
        bad = sorted(k for k, m in v.items() if not MODEL_NAME.match(m))
        if bad:
            raise ValueError(f"invalid model name for {bad}")
        return v

    def model_for(self, procedure: str) -> str | None:
        return self.models.get(procedure, self.model)

    @field_validator("plugin_path", mode="after")
    @classmethod
    def _paths(cls, v: Path, info: ValidationInfo) -> Path:
        return _resolve_path(v, info)

    @field_validator("allow_paid_api_fallback")
    @classmethod
    def _no_fallback(cls, v: bool) -> bool:
        if v:
            raise ValueError("paid API fallback is not supported by this framework")
        return v


class ApprovalsConfig(StrictModel):
    jira_account_ids: list[str] = Field(min_length=1)
    github_logins: list[str] = Field(default_factory=list)
    require_independent_github_review: bool = True

    @field_validator("jira_account_ids")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        for a in v:
            if not ACCOUNT_ID.match(a):
                raise ValueError(f"{a!r} is not a Jira account ID")
        return v


class ReleaseConfig(StrictModel):
    profile: Literal["local_pilot"] = "local_pilot"
    environment: str = Field(default="local-pilot", pattern=r"^[A-Za-z0-9._-]{1,64}$")
    human_merge_required: Literal[True] = True
    human_deployment_required: Literal[True] = True
    smoke_commands: dict[str, list[str]] = Field(default_factory=dict)


class CIConfig(StrictModel):
    required_names: list[str] = Field(default_factory=list)
    expected_producer: str = "github-actions"
    allow_neutral: list[str] = Field(default_factory=list)
    allow_skipped: list[str] = Field(default_factory=list)
    provenance_context: str = "delivery/integration-provenance"


class ChecksConfig(StrictModel):
    # Run once in each fresh worktree before checks (for example ["npm", "ci"]).
    setup: list[str] = Field(default_factory=list)
    commands: dict[str, list[str]] = Field(default_factory=dict)
    ci: CIConfig = CIConfig()
    integration: list[str] | None = None

    @field_validator("commands")
    @classmethod
    def _commands(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for name, argv in v.items():
            if not CHECK_NAME.match(name):
                raise ValueError(f"check name {name!r} must be lower-case letters, digits, - or _")
            if not argv or not all(isinstance(a, str) and a for a in argv):
                raise ValueError(f"check {name!r} must be a non-empty argument list")
        return v

    @model_validator(mode="after")
    def _integration(self) -> ChecksConfig:
        if self.integration is not None:
            unknown = [n for n in self.integration if n not in self.commands]
            if unknown:
                raise ValueError(f"integration checks not defined in commands: {unknown}")
        return self

    @property
    def integration_names(self) -> list[str]:
        return list(self.commands) if self.integration is None else list(self.integration)


StatusMapping = dict[Status, str]


class WorkflowConfig(StrictModel):
    statuses: dict[Status, str] = Field(default_factory=dict)
    actions: dict[Action, str] = Field(default_factory=dict)

    @field_validator("statuses")
    @classmethod
    def _status_ids(cls, v: dict[Status, str]) -> dict[Status, str]:
        seen: dict[str, Status] = {}
        for key, sid in v.items():
            if not re.match(r"^\d{1,12}$", sid):
                raise ValueError(
                    f"{key.value} = {sid!r} is not a Jira status ID; "
                    "run `delivery workflow inspect` to resolve IDs"
                )
            if sid in seen:
                raise ValueError(f"{key.value} and {seen[sid].value} map to the same status {sid}")
            seen[sid] = key
        return v

    def action_name(self, action: Action) -> str:
        return self.actions.get(action, DEFAULT_ACTION_NAMES[action])

    def missing_statuses(self) -> list[Status]:
        return [s for s in Status if s not in self.statuses]


class OverlapConfig(StrictModel):
    component_map_path: str = "docs/architecture/components.toml"
    # Paths that are expected to change in many tickets (lockfiles, changelogs) and only warn.
    low_signal_paths: list[str] = Field(
        default_factory=lambda: ["package-lock.json", "uv.lock", "CHANGELOG.md"]
    )


class Config(StrictModel):
    config_version: Literal[1]
    identity: IdentityConfig
    jira: JiraConfig
    repository: RepositoryConfig
    runtime: RuntimeConfig
    claude: ClaudeConfig
    approvals: ApprovalsConfig
    release: ReleaseConfig = ReleaseConfig()
    checks: ChecksConfig = ChecksConfig()
    workflow: WorkflowConfig = WorkflowConfig()
    overlap: OverlapConfig = OverlapConfig()

    # Set by load_config; not part of the file.
    source_path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _paths_outside_checkout(self) -> Config:
        checkout = self.repository.checkout_path
        for label, p in (
            ("runtime.state_dir", self.runtime.state_dir),
            ("repository.worktree_root", self.repository.worktree_root),
        ):
            if p == checkout or checkout in p.parents:
                raise ValueError(f"{label} must be outside the application checkout ({checkout})")
        if self.runtime.state_dir == self.repository.worktree_root:
            raise ValueError("runtime.state_dir and repository.worktree_root must differ")
        return self

    @property
    def identity_key(self) -> str:
        """Stable key for the Jira site + developer identity (lock and supervisor scope)."""
        raw = f"{self.jira.base_url}|{self.identity.developer_jira_account_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def status_id(self, status: Status) -> str:
        try:
            return self.workflow.statuses[status]
        except KeyError:
            raise ConfigError(
                self.source_path or Path("?"),
                [f"workflow.statuses.{status.value} is not mapped"],
            ) from None

    def status_by_id(self) -> dict[str, Status]:
        return {sid: s for s, sid in self.workflow.statuses.items()}

    def digest(self) -> str:
        """Digest of the effective configuration. Contains no secrets by construction."""
        data = self.model_dump(mode="json")
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _format_error(err: dict[str, Any]) -> str:
    loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
    msg = str(err.get("msg", "invalid"))
    msg = msg.removeprefix("Value error, ")
    if err.get("type") == "extra_forbidden":
        msg = "unknown key (check spelling; unknown keys are rejected)"
    # Never echo input values back: they could contain a pasted secret.
    return f"{loc}: {msg}" if loc else msg


def load_config(path: Path) -> Config:
    path = path.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(_normalise(path))
    if not path.is_file():
        raise ConfigError(path, ["config file not found; create one with `delivery init`"])
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, [f"invalid TOML: {exc}"]) from None
    try:
        cfg = Config.model_validate(raw, context={"base_dir": str(path.parent)})
    except ValidationError as exc:
        raise ConfigError(path, [_format_error(dict(e)) for e in exc.errors()]) from None
    return cfg.model_copy(update={"source_path": path})


def template_text() -> str:
    from importlib.resources import files

    return (files("delivery") / "data" / "delivery.example.toml").read_text(encoding="utf-8")
