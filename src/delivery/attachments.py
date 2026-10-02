"""Ticket attachments (designs, screenshots, documents) handed to Claude as read-only inputs.

Claude's sessions have no network access, so the coordinator downloads attachments with the
developer's Jira login into the run's private inputs directory, which the session may read
but not write. Each file is filtered by type and size, checked against its declared type,
fingerprinted, and listed in the input envelope. The attachment list is part of the brief
digest, so adding or replacing a design is a change to the ticket's input.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from delivery.config import AttachmentsConfig
from delivery.models import AttachmentRef, SkippedAttachment
from delivery.ports import Attachment, IntegrationError, JiraPort
from delivery.workflow import Stage

# Stages whose procedures use designs: writing the spec and plan, building, reviewing and
# verifying against them. Release stages work from approved artefacts only.
ATTACHMENT_STAGES = frozenset({Stage.REFINEMENT, Stage.PLANNING, Stage.DEVELOPMENT, Stage.VERIFICATION})

MB = 1024 * 1024
_MEDIA = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "json": "application/json",
}
# File signatures: a renamed executable or HTML page is refused, not handed over.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
    "pdf": (b"%PDF-",),
}


def extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def safe_name(att: Attachment) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", att.filename).strip("._") or "file"
    return f"{att.id}-{stem[:120]}"


def _content_ok(path: Path, ext: str) -> bool:
    head = path.read_bytes()[:16]
    if ext == "webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext in _MAGIC:
        return any(head.startswith(m) for m in _MAGIC[ext])
    try:  # text types must be UTF-8 text
        path.read_bytes()[: 64 * 1024].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return b"\x00" not in head


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def fetch_attachments(
    cfg: AttachmentsConfig, jira: JiraPort, attachments: tuple[Attachment, ...], dest: Path
) -> tuple[list[AttachmentRef], list[SkippedAttachment]]:
    """Download the usable attachments into ``dest``; explain every one left out."""
    refs: list[AttachmentRef] = []
    skipped: list[SkippedAttachment] = []
    if not attachments:
        return refs, skipped
    if not cfg.enabled:
        return refs, [
            SkippedAttachment(
                attachment_id=a.id, filename=a.filename, reason="attachments disabled in config"
            )
            for a in attachments
        ]
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    total = 0
    for att in sorted(attachments, key=lambda a: (a.created is None, a.created, a.id)):
        ext = extension(att.filename)

        def skip(reason: str, att: Attachment = att) -> None:
            skipped.append(SkippedAttachment(attachment_id=att.id, filename=att.filename, reason=reason))

        if ext not in cfg.file_types:
            skip(f"file type .{ext or '?'} is not handed to Claude (allowed: {', '.join(cfg.file_types)})")
            continue
        if att.size > cfg.max_file_mb * MB:
            skip(f"larger than {cfg.max_file_mb} MB")
            continue
        if total + att.size > cfg.max_total_mb * MB:
            skip(f"would exceed the {cfg.max_total_mb} MB total for one run")
            continue
        path = dest / safe_name(att)
        if not path.exists() or path.stat().st_size != att.size:
            try:
                await jira.download_attachment(att.id, path, cfg.max_file_mb * MB)
            except IntegrationError as exc:
                if exc.retryable:
                    raise
                skip(f"download failed: {exc}")
                continue
        if not _content_ok(path, ext):
            path.unlink(missing_ok=True)
            skip(f"content is not a real .{ext} file")
            continue
        path.chmod(0o400)
        size = path.stat().st_size
        total += size
        refs.append(
            AttachmentRef(
                attachment_id=att.id,
                filename=att.filename,
                path=str(path),
                media_type=_MEDIA[ext],
                size=size,
                sha256=_sha256(path),
                created=att.created,
                author_account_id=att.author_account_id,
            )
        )
    return refs, skipped
