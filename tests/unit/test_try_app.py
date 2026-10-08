from __future__ import annotations

from typing import Any, cast

import pytest

from delivery.try_app import TryError, find_candidate


class FakeRepo:
    def __init__(self, refs: dict[str, str]) -> None:
        self.refs = refs

    async def resolve(self, ref: str) -> str | None:
        return self.refs.get(ref)


async def test_ref_prefers_the_fetched_remote_branch_over_a_stale_local_one() -> None:
    repo = cast(Any, FakeRepo({"main": "a" * 40, "origin/main": "b" * 40}))
    assert await find_candidate(None, repo, "SDLC-1", "main") == ("b" * 40, "main")


async def test_ref_falls_back_to_a_commit_or_local_ref() -> None:
    repo = cast(Any, FakeRepo({"c" * 40: "c" * 40}))
    assert await find_candidate(None, repo, "SDLC-1", "c" * 40) == ("c" * 40, "c" * 40)


async def test_unknown_ref_is_an_error() -> None:
    with pytest.raises(TryError):
        await find_candidate(None, cast(Any, FakeRepo({})), "SDLC-1", "nope")


def test_try_without_ticket_or_ref_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    from delivery.cli import EXIT_CONFIG, main

    assert main(["try"]) == EXIT_CONFIG
    assert "--ref" in capsys.readouterr().err


def test_try_ref_alone_runs_without_a_ticket_or_jira(monkeypatch: pytest.MonkeyPatch) -> None:
    import delivery.try_app as try_app
    from delivery import cli

    seen: dict[str, Any] = {}

    async def fake_try(cfg: Any, key: str, **kw: Any) -> int:
        seen.update(key=key, **kw)
        return 0

    monkeypatch.setattr(try_app, "try_candidate", fake_try)
    monkeypatch.setattr(cli, "_load", lambda args: object())
    monkeypatch.setattr(cli, "build_repo", lambda cfg: object())

    assert cli.main(["try", "--ref", "feature/x y"]) == 0
    assert seen["key"] == "feature-x-y"
    assert seen["ref"] == "feature/x y"
    assert seen["jira"] is None
