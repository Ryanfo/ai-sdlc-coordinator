from __future__ import annotations

import hashlib
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import DEV, STATUS_IDS, ConfigFactory
from delivery.adf import adf_to_text
from delivery.attachments import fetch_attachments
from delivery.config import AttachmentsConfig, ConfigError
from delivery.intake import brief_text
from delivery.jira import JiraClient
from delivery.ports import Attachment, IntegrationError
from fakes.jira import FakeJira

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 64
MB = 1024 * 1024


def jira_with(*files: tuple[str, bytes]) -> tuple[FakeJira, tuple[Attachment, ...]]:
    jira = FakeJira(STATUS_IDS, me=DEV)
    jira.create("PILOT-1", "S", "D", DEV)
    atts = tuple(jira.attach("PILOT-1", name, data) for name, data in files)
    return jira, atts


async def test_designs_are_downloaded_read_only_and_fingerprinted(tmp_path: Path) -> None:
    jira, atts = jira_with(("Home screen.png", PNG), ("flows.pdf", PDF), ("notes.md", b"# Notes\n"))
    refs, skipped = await fetch_attachments(AttachmentsConfig(), jira, atts, tmp_path / "a")
    assert skipped == []
    assert [r.filename for r in refs] == ["Home screen.png", "flows.pdf", "notes.md"]
    assert [r.media_type for r in refs] == ["image/png", "application/pdf", "text/markdown"]
    for ref, data in zip(refs, (PNG, PDF, b"# Notes\n"), strict=True):
        path = Path(ref.path)
        assert path.parent == tmp_path / "a" and path.read_bytes() == data
        assert ref.sha256 == hashlib.sha256(data).hexdigest()
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert Path(refs[0].path).name == f"{atts[0].id}-Home_screen.png"


@pytest.mark.parametrize(
    ("name", "data", "reason"),
    [
        ("design.svg", b"<svg onload=alert(1)/>", "file type .svg"),
        ("tool.exe", b"MZ\x90\x00", "file type .exe"),
        ("fake.png", b"<html><script>x</script></html>", "content is not a real .png"),
        ("binary.txt", b"ok\x00\x01\x02", "content is not a real .txt"),
        ("huge.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * MB), "larger than 1 MB"),
    ],
)
async def test_unsafe_or_oversized_files_are_skipped_with_a_reason(
    tmp_path: Path, name: str, data: bytes, reason: str
) -> None:
    jira, atts = jira_with((name, data))
    refs, skipped = await fetch_attachments(AttachmentsConfig(max_file_mb=1), jira, atts, tmp_path)
    assert refs == [] and reason in skipped[0].reason
    assert not any(tmp_path.iterdir())  # nothing unsafe is left for the session to read


async def test_total_size_cap_applies_per_run(tmp_path: Path) -> None:
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MB - 100)
    jira, atts = jira_with(("a.png", big), ("b.png", big))
    refs, skipped = await fetch_attachments(AttachmentsConfig(max_total_mb=1), jira, atts, tmp_path)
    assert [r.filename for r in refs] == ["a.png"]
    assert "total" in skipped[0].reason


async def test_path_traversal_in_filename_stays_inside_inputs(tmp_path: Path) -> None:
    jira, atts = jira_with(("../../etc/passwd.txt", b"hello"))
    refs, _ = await fetch_attachments(AttachmentsConfig(), jira, atts, tmp_path / "a")
    assert Path(refs[0].path).parent == tmp_path / "a"


async def test_disabled_skips_everything_and_retryable_errors_propagate(tmp_path: Path) -> None:
    jira, atts = jira_with(("a.png", PNG))
    refs, skipped = await fetch_attachments(AttachmentsConfig(enabled=False), jira, atts, tmp_path)
    assert refs == [] and skipped[0].reason == "attachments disabled in config"
    jira.fail_next["download_attachment"] = IntegrationError("Jira unreachable", retryable=True)
    with pytest.raises(IntegrationError):
        await fetch_attachments(AttachmentsConfig(), jira, atts, tmp_path)


def test_attachments_are_part_of_the_brief_digest() -> None:
    jira, _ = jira_with()
    issue_before = jira._view(jira.issues["PILOT-1"])
    jira.attach("PILOT-1", "home.png", PNG)
    issue_after = jira._view(jira.issues["PILOT-1"])
    assert brief_text(issue_before) != brief_text(issue_after)
    assert "home.png (unknown type, 72 bytes" in brief_text(issue_after)


def test_images_in_the_description_leave_a_placeholder() -> None:
    doc: dict[str, Any] = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Match this layout:"}]},
            {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "m1", "alt": "home.png"}}]},
        ],
    }
    assert adf_to_text(doc) == "Match this layout:\n[attachment: home.png]"


def test_config_refuses_unsafe_types(make_config: ConfigFactory) -> None:
    with pytest.raises(ConfigError):
        make_config(overrides={"jira.attachments": {"file_types": ["png", "svg"]}})


async def test_jira_client_parses_and_streams_attachments(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "ATATT3xFfGF0" + "t" * 180)
    issue = {
        "key": "PILOT-1",
        "fields": {
            "summary": "S",
            "status": {"id": "10001", "name": "x"},
            "attachment": [
                {
                    "id": "77",
                    "filename": "home.png",
                    "mimeType": "image/png",
                    "size": len(PNG),
                    "created": "2026-10-02T10:00:00.000+0100",
                    "author": {"accountId": DEV},
                }
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/issue/PILOT-1":
            return httpx.Response(200, json=issue)
        if request.url.path == "/rest/api/3/attachment/content/77":
            return httpx.Response(303, headers={"Location": "https://media.example.net/file/77?token=t"})
        if request.url.host == "media.example.net":
            assert "Authorization" not in request.headers  # credentials never leave the Jira host
            return httpx.Response(200, content=PNG)
        return httpx.Response(404, json={})

    client = JiraClient(make_config(), transport=httpx.MockTransport(handler))
    try:
        got = await client.get_issue("PILOT-1")
        att = got.attachments[0]
        assert (att.id, att.filename, att.mime_type, att.size, att.author_account_id) == (
            "77",
            "home.png",
            "image/png",
            len(PNG),
            DEV,
        )
        assert att.created == datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
        dest = tmp_path / "77-home.png"
        assert await client.download_attachment("77", dest, max_bytes=MB) == len(PNG)
        assert dest.read_bytes() == PNG
        with pytest.raises(IntegrationError, match="exceeds"):
            await client.download_attachment("77", tmp_path / "small", max_bytes=10)
        assert not (tmp_path / "small.part").exists()
    finally:
        await client.close()
