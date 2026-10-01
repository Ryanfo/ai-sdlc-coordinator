"""Secret redaction for logs, journals and diagnostics."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

# Each pattern either has a ``label`` group that is kept, or is replaced entirely.
_PATTERNS = [
    re.compile(r"ATATT[A-Za-z0-9_\-=]{20,}"),  # Atlassian API tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),  # Anthropic keys
    re.compile(r"(?i)(?P<label>authorization:\s*(?:basic|bearer)\s+)[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"(?i)(?P<label>\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s'\"]{8,}"),
]

_SENSITIVE_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|CREDENTIAL)", re.I)


def secret_values_from_env(names: Iterable[str] = ()) -> list[str]:
    wanted = set(names)
    return [
        value
        for name, value in os.environ.items()
        if (name in wanted or _SENSITIVE_ENV.search(name)) and len(value) >= 8
    ]


def _sub(m: re.Match[str]) -> str:
    label = m.groupdict().get("label")
    return (label or "") + REDACTED


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    for secret in sorted({s for s in extra_secrets if s and len(s) >= 8}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pat in _PATTERNS:
        text = pat.sub(_sub, text)
    return text
