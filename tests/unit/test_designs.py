from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest

from conftest import ConfigFactory
from delivery.config import ConfigError, FigmaConfig
from delivery.credentials import resolve_figma
from delivery.designs import FigmaLink, condense, fetch_designs, figma_links, summarise
from delivery.figma import FigmaClient
from fakes.figma import PNG, FakeFigma, frame, text

KEY = "AbCdEfGhIj1234567890"
OTHER = "ZyXwVuTsRq0987654321"


def link(node: str | None = "1:2", key: str = KEY) -> FigmaLink:
    return FigmaLink(f"https://www.figma.com/design/{key}/App?node-id={node}", key, node)


def test_links_in_all_common_shapes_are_found() -> None:
    desc = (
        "Designs:\n"
        f"- Home (https://www.figma.com/design/{KEY}/Task-app?node-id=12-345&t=abc123-1).\n"
        f"- Same frame again https://www.figma.com/design/{KEY}/Task-app?node-id=12-345\n"
        f"- Old style https://figma.com/file/{OTHER}/x?node-id=7%3A8\n"
        f"- Branch https://www.figma.com/design/{KEY}/branch/{OTHER}/App?node-id=1-2\n"
        f"- Whole file https://www.figma.com/design/{KEY}/Task-app\n"
        "- Not Figma https://example.com/design/abc"
    )
    got = [(lk.file_key, lk.node_id) for lk in figma_links(desc)]
    assert got == [(KEY, "12:345"), (OTHER, "7:8"), (OTHER, "1:2"), (KEY, None)]
    assert figma_links(desc)[0].url == f"https://www.figma.com/design/{KEY}/Task-app?node-id=12-345"


def test_frame_summary_lists_copy_type_colours_and_layout() -> None:
    node = frame(
        "1:2", "Home", text("1:4", "Add task", y=80, size=16), text("1:3", "Task list", y=24, size=32)
    )
    condensed = condense(node, {}, [100])
    assert condensed is not None
    md = summarise(condensed, url="u", file_name="App", version="1001", modified="m")
    assert md.index('"Task list"') < md.index('"Add task"')  # reading order, not layer order
    assert "Inter 32/48 weight 600" in md and "#1A1A1A" in md and "#FFFFFF" in md
    assert "Layout: vertical auto-layout, gap 16, padding 24 24 24 24" in md


async def test_linked_frames_are_snapshotted_read_only_and_pinned(tmp_path: Path) -> None:
    fig = FakeFigma()
    v1 = fig.publish(KEY, {"1:2": frame("1:2", "Home", text("1:3", "Task list"))})
    got = await fetch_designs(FigmaConfig(), fig, [link()], tmp_path, pinned={})
    [ref] = got.refs
    assert (ref.frame_name, ref.version, ref.file_name, ref.width, ref.height) == (
        "Home",
        v1,
        "App designs",
        390,
        844,
    )
    assert got.versions == {KEY: v1} and got.skipped == [] and not ref.changed_in_figma_since
    assert Path(ref.image_path).read_bytes() == PNG
    assert '"Task list"' in Path(ref.summary_path).read_text()
    assert json.loads(Path(ref.data_path).read_text())["name"] == "Home"
    for p in (ref.image_path, ref.summary_path, ref.data_path):
        assert stat.S_IMODE(Path(p).stat().st_mode) == 0o400
        assert Path(p).parent == tmp_path


async def test_later_stages_use_the_pinned_version_and_flag_design_changes(tmp_path: Path) -> None:
    fig = FakeFigma()
    v1 = fig.publish(
        KEY, {"1:2": frame("1:2", "Home", text("1:3", "Task list")), "5:6": frame("5:6", "Empty")}
    )
    fig.publish(KEY, {"1:2": frame("1:2", "Home", text("1:3", "My tasks")), "5:6": frame("5:6", "Empty")})
    got = await fetch_designs(FigmaConfig(), fig, [link("1:2"), link("5:6")], tmp_path, pinned={KEY: v1})
    home, empty = got.refs
    assert home.version == v1 and '"Task list"' in Path(home.summary_path).read_text()
    assert home.changed_in_figma_since and not empty.changed_in_figma_since
    assert [r.frame_name for r in got.changed] == ["Home"]
    assert ("render", f"{KEY}@{v1}") in fig.calls
    # Re-running the same stage (for example after an interruption) rewrites the read-only files.
    again = await fetch_designs(FigmaConfig(), fig, [link("1:2")], tmp_path, pinned={KEY: v1})
    assert again.refs[0].image_sha256 == home.image_sha256


async def test_problem_links_are_skipped_with_a_reason(tmp_path: Path) -> None:
    fig = FakeFigma()
    fig.publish(KEY, {"1:2": frame("1:2", "Home")})
    fig.publish(OTHER, {"1:2": frame("1:2", "Secret")})
    fig.denied.add(OTHER)
    links = [link(None), link("9:9"), link("1:2", OTHER), link("1:2")]
    got = await fetch_designs(FigmaConfig(), fig, links, tmp_path, pinned={})
    assert [r.frame_name for r in got.refs] == ["Home"]
    reasons = [s.reason for s in got.skipped]
    assert "whole file" in reasons[0] and "no longer exists" in reasons[1] and "refused access" in reasons[2]


async def test_page_links_expand_to_frames_within_the_limit(tmp_path: Path) -> None:
    fig = FakeFigma()
    page = {
        "id": "0:1",
        "name": "Screens",
        "type": "CANVAS",
        "children": [frame(f"2:{i}", f"S{i}") for i in range(4)],
    }
    fig.publish(KEY, {"0:1": page})
    got = await fetch_designs(FigmaConfig(max_frames=3), fig, [link("0:1")], tmp_path, pinned={})
    assert [r.frame_name for r in got.refs] == ["S0", "S1", "S2"]
    assert "limit of 3 frames" in got.skipped[0].reason


async def test_without_a_token_or_when_disabled_links_are_listed_as_skipped(tmp_path: Path) -> None:
    got = await fetch_designs(FigmaConfig(), None, [link()], tmp_path, pinned={})
    assert "delivery credentials set figma" in got.skipped[0].reason
    got = await fetch_designs(FigmaConfig(enabled=False), FakeFigma(), [link()], tmp_path, pinned={})
    assert "disabled" in got.skipped[0].reason


async def test_figma_client_sends_token_to_api_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, bool]] = []
    attempts = {"n": 0}

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("delivery.figma.asyncio.sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, "X-Figma-Token" in request.headers))
        if request.url.path == f"/v1/files/{KEY}":
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"name": "App", "version": "1001"})
        if request.url.path == f"/v1/images/{KEY}":
            assert request.url.params["version"] == "1001" and request.url.params["format"] == "png"
            return httpx.Response(200, json={"images": {"1:2": "https://s3.example.com/r.png?sig=x"}})
        if request.url.host == "s3.example.com":
            return httpx.Response(200, content=PNG)
        return httpx.Response(403, json={"err": "Invalid token"})

    client = FigmaClient("figd_test_token_value", transport=httpx.MockTransport(handler))
    try:
        assert (await client.file_info(KEY))["version"] == "1001"
        urls = await client.render(KEY, ["1:2"], "1001", 2.0)
        assert await client.download(str(urls["1:2"]), tmp_path / "r.png", 1024) == len(PNG)
        with pytest.raises(Exception, match="403"):
            await client.me()
    finally:
        await client.close()
    assert ("s3.example.com", False) in seen  # the token never goes to the render host
    assert all(has for host, has in seen if host == "api.figma.com")


def test_figma_token_comes_from_env_or_keychain(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config()
    monkeypatch.setenv("FIGMA_TOKEN", "figd_from_env_value")
    assert resolve_figma(cfg, lambda argv: (44, "")) == "figd_from_env_value"
    monkeypatch.delenv("FIGMA_TOKEN")
    calls: list[list[str]] = []
    assert resolve_figma(cfg, lambda argv: (calls.append(argv), (0, "figd_keychain\n"))[1]) == "figd_keychain"
    assert calls[0][calls[0].index("-s") + 1] == "delivery-figma"
    assert resolve_figma(cfg, lambda argv: (44, "")) is None


def test_figma_config_is_validated(make_config: ConfigFactory) -> None:
    with pytest.raises(ConfigError):
        make_config(overrides={"figma": {"image_scale": 8}})
    with pytest.raises(ConfigError):
        make_config(overrides={"figma": {"token_env": "figd_abc"}})
