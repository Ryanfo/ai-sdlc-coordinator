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
