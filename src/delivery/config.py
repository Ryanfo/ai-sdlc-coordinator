"""The single human-edited local configuration file.

Rules from the handoff: one explicitly selected file, unknown keys rejected, relative
paths resolved against the config file (never the caller's working directory), and
secrets only ever referenced by environment variable name.

A team can keep the settings that are the same for everyone on a project (Jira site and
workflow, repository, approvers, checks, models) in one shared project file. A personal
config then names it with ``project = "<path>"`` and holds only what is personal: identity,
email, local folders and terminal preferences. Personal values win where both set a key; the
project file may not contain ``[identity]``.

There is deliberately no setting that limits the number of concurrent sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
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


# File types handed to Claude: images and PDFs it can view, plain text it can read.
# Excluded on purpose: SVG/HTML (active content), archives, office files and executables.
SAFE_ATTACHMENT_TYPES = ("png", "jpg", "jpeg", "gif", "webp", "pdf", "txt", "md", "csv", "json")


class AttachmentsConfig(StrictModel):
    enabled: bool = True
    max_file_mb: int = Field(default=20, ge=1, le=100)
    max_total_mb: int = Field(default=100, ge=1, le=500)
    file_types: list[str] = Field(default_factory=lambda: list(SAFE_ATTACHMENT_TYPES))

    @field_validator("file_types")
    @classmethod
    def _types(cls, v: list[str]) -> list[str]:
        v = [t.lower().lstrip(".") for t in v]
        unsafe = sorted(set(v) - set(SAFE_ATTACHMENT_TYPES))
        if unsafe:
            raise ValueError(f"{unsafe} are not allowed; choose from {list(SAFE_ATTACHMENT_TYPES)}")
        return v


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
    attachments: AttachmentsConfig = AttachmentsConfig()

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


# Long procedures get more room by default; [claude.turn_limits] / timeout_minutes override.
DEFAULT_TURN_LIMITS = {"implement-ticket": 500, "verify-ticket": 250, "review-ticket": 200}
DEFAULT_TIMEOUT_MINUTES = {"implement-ticket": 120, "verify-ticket": 60, "review-ticket": 45}


class GuardrailsConfig(StrictModel):
    """Stop a session early when it is clearly not making progress (see delivery.guardrails)."""

    # Same tool call with the same result this many times within its last 30 steps.
    loop_repeats: int = Field(default=6, ge=3, le=50)
    # No new activity in the session log for this long.
    stall_minutes: int = Field(default=15, ge=2, le=180)


class InteractiveConfig(StrictModel):
    """Run each Claude session as a normal interactive session inside tmux (see delivery.interactive).

    You can watch it work and type to it. The coordinator still reads a schema-checked result,
    from a file the session writes; a Stop hook keeps Claude working until that file is valid.
    """

    enabled: bool = False
    tmux: str = "tmux"
    # Name of the coordinator's private tmux server (tmux -L). Change it only to run two
    # coordinators on one machine.
    socket: str = Field(default="delivery", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    # Open a terminal window attached to each session as it starts ("none": attach yourself
    # with `delivery attach <ticket>`). Opening windows works on macOS only.
    window: Literal["Terminal", "iTerm", "none"] = "Terminal"
    # Once Claude has handed its result to the coordinator, leave the session open for
    # questions. In a development session, changes you ask for there are pushed as a new
    # candidate and the ticket goes back to Ready for verification; verification and review
    # wait until you end the session (/exit), so they run once on your final candidate.
    keep_open: bool = True
    # Close an open session after this long with nothing happening in it.
    idle_close_hours: int = Field(default=12, ge=1, le=336)

    @property
    def follow_ups(self) -> bool:
        return self.enabled and self.keep_open


class PreviewConfig(StrictModel):
    """Run the app from a finished development session's worktree so you can try the change.

    Needs [claude.interactive] with keep_open: the worktree stays while that session is open,
    and changes you ask for there show up in the running app (see delivery.preview).
    """

    # Argument list, never a shell string. "{port}" is replaced by the preview's own port,
    # which is also exported as PORT. Empty: no preview.
    command: list[str] = Field(default_factory=list)
    # Run first in the worktree. None: checks.setup; [] for nothing.
    setup: list[str] | None = None
    url: str = "http://localhost:{port}/"
    open_browser: bool = True
    # Mention it in the terminal if the app has not answered by then (it keeps waiting).
    ready_timeout_seconds: int = Field(default=180, ge=10, le=3600)

    @field_validator("command", "setup")
    @classmethod
    def _argv(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and not all(isinstance(a, str) and a for a in v):
            raise ValueError("must be a list of non-empty arguments")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not re.match(r"^https?://", v):
            raise ValueError("must start with http:// or https://")
        return v

    @property
    def enabled(self) -> bool:
        return bool(self.command)


def bundled_plugin_path() -> Path:
    """The delivery plugin of this installation (``plugins/delivery`` next to ``src``)."""
    return Path(__file__).resolve().parents[2] / "plugins" / "delivery"


class ClaudeConfig(StrictModel):
    executable: str = "claude"
    plugin_path: Path = Field(default_factory=bundled_plugin_path)
    auth_profile: Literal["subscription"] = "subscription"
    # Turn limit for procedures not listed in turn_limits (built-in defaults raise it for the
    # long procedures). The loop and stall guardrails are the main protection, not this.
    max_turns: int | None = Field(default=150, ge=1, le=2000)
    turn_limits: dict[str, int] = Field(default_factory=dict)
    # Per-procedure session timeouts in minutes (others use runtime.timeout_seconds).
    timeout_minutes: dict[str, int] = Field(default_factory=dict)
    guardrails: GuardrailsConfig = GuardrailsConfig()
    interactive: InteractiveConfig = InteractiveConfig()
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

    @field_validator("turn_limits", "timeout_minutes")
    @classmethod
    def _per_procedure(cls, v: dict[str, int]) -> dict[str, int]:
        from delivery.plugin import PROCEDURES

        unknown = sorted(set(v) - set(PROCEDURES))
        if unknown:
            raise ValueError(f"unknown procedures {unknown}; use one of {list(PROCEDURES)}")
        if any(not 1 <= n <= 2000 for n in v.values()):
            raise ValueError("values must be between 1 and 2000")
        return v

    def turns_for(self, procedure: str) -> int | None:
        if procedure in self.turn_limits:
            return self.turn_limits[procedure]
        if self.max_turns is None:
            return None
        return max(DEFAULT_TURN_LIMITS.get(procedure, 0), self.max_turns)

    def timeout_for(self, procedure: str, default_seconds: int) -> int:
        if procedure in self.timeout_minutes:
            return self.timeout_minutes[procedure] * 60
        return max(DEFAULT_TIMEOUT_MINUTES.get(procedure, 0) * 60, default_seconds)

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


class FigmaConfig(StrictModel):
    """Figma frames linked in a ticket, fetched by the coordinator (never by Claude)."""

    enabled: bool = True
    # macOS Keychain service and account holding a Figma personal access token
    # (scopes: file_content:read, current_user:read). FIGMA_TOKEN in the environment wins.
    token_keychain_service: str = Field(default="delivery-figma", pattern=r"^[A-Za-z0-9._-]{1,100}$")
    token_account: str = Field(default="figma", pattern=r"^[A-Za-z0-9._@+-]{1,100}$")
    token_env: str = "FIGMA_TOKEN"  # noqa: S105 - an env var name, not a secret
    image_scale: float = Field(default=2.0, ge=0.5, le=4.0)
    max_frames: int = Field(default=10, ge=1, le=50)
    max_image_mb: int = Field(default=20, ge=1, le=100)

    @field_validator("token_env")
    @classmethod
    def _env(cls, v: str) -> str:
        if not ENV_NAME.match(v):
            raise ValueError("must be an environment variable name, not a secret value")
        return v


class Config(StrictModel):
    config_version: Literal[1]
    identity: IdentityConfig
    jira: JiraConfig
    repository: RepositoryConfig
    runtime: RuntimeConfig
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    approvals: ApprovalsConfig
    release: ReleaseConfig = ReleaseConfig()
    checks: ChecksConfig = ChecksConfig()
    workflow: WorkflowConfig = WorkflowConfig()
    overlap: OverlapConfig = OverlapConfig()
    figma: FigmaConfig = FigmaConfig()
    preview: PreviewConfig = PreviewConfig()

    # Set by load_config; not part of the file.
    source_path: Path | None = Field(default=None, exclude=True)
    project_path: Path | None = Field(default=None, exclude=True)

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


# Settings that belong to one developer and machine: a shared project file never sets them,
# and `delivery project export` leaves them out. None means the whole table.
PERSONAL_KEYS: dict[str, frozenset[str] | None] = {
    "identity": None,
    "jira": frozenset({"email", "email_env", "token_env", "token_keychain_service"}),
    "repository": frozenset({"checkout_path", "worktree_root"}),
    "runtime": frozenset({"state_dir"}),
    "claude": frozenset({"executable", "plugin_path", "interactive"}),
    "figma": frozenset({"token_keychain_service", "token_account", "token_env"}),
}


def personal_keys_in(raw: dict[str, Any]) -> list[str]:
    """Dotted names of the personal settings present in ``raw``."""
    found = []
    for table, keys in PERSONAL_KEYS.items():
        body = raw.get(table)
        if not isinstance(body, dict):
            continue
        found += [table] if keys is None else [f"{table}.{k}" for k in sorted(keys) if k in body]
    return found


def split_personal(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a full config into (shared project settings, personal settings)."""
    project: dict[str, Any] = {}
    personal: dict[str, Any] = {}
    for table, body in raw.items():
        keys = PERSONAL_KEYS.get(table, frozenset())
        if not isinstance(body, dict):
            (personal if table in ("config_version", "project") else project)[table] = body
        elif keys is None:
            personal[table] = body
        else:
            mine = {k: v for k, v in body.items() if k in keys}
            team = {k: v for k, v in body.items() if k not in keys}
            if mine:
                personal[table] = mine
            if team:
                project[table] = team
    return project, personal


def merge_tables(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """``over`` on top of ``base``: tables merge key by key, anything else is replaced."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_tables(out[key], value)
        else:
            out[key] = value
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, [f"invalid TOML: {exc}"]) from None


def default_config_path() -> Path:
    return Path(os.environ.get("DELIVERY_CONFIG") or Path.home() / "delivery.local.toml")


def load_config(path: Path) -> Config:
    path = path.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(_normalise(path))
    if not path.is_file():
        raise ConfigError(
            path, ["config file not found; create one with `coordinator setup` (or `delivery init`)"]
        )
    raw = _read_toml(path)
    project_path: Path | None = None
    if "project" in raw:
        ref = raw.pop("project")
        if not isinstance(ref, str) or not ref:
            raise ConfigError(path, ["project: must be the path of the shared project file"])
        project_path = Path(ref).expanduser()
        if not project_path.is_absolute():
            project_path = path.parent / project_path
        project_path = Path(_normalise(project_path))
        if not project_path.is_file():
            raise ConfigError(path, [f"project: shared project file {project_path} not found"])
        shared = _read_toml(project_path)
        personal = personal_keys_in(shared)
        if personal:
            raise ConfigError(
                project_path,
                [
                    f"{k} is personal; set it in your own config, not the shared project file"
                    for k in personal
                ],
            )
        raw = merge_tables(shared, raw)
    try:
        cfg = Config.model_validate(raw, context={"base_dir": str(path.parent)})
    except ValidationError as exc:
        problems = [_format_error(dict(e)) for e in exc.errors()]
        if project_path:
            problems.append(f"(settings come from {path} on top of the project file {project_path})")
        raise ConfigError(path, problems) from None
    return cfg.model_copy(update={"source_path": path, "project_path": project_path})


def template_text() -> str:
    from importlib.resources import files

    return (files("delivery") / "data" / "delivery.example.toml").read_text(encoding="utf-8")
