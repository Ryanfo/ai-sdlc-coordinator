"""Review comments on the candidate's pull request, as change items for development.

Reviewers comment where they read the code: on GitHub. When a change request is submitted
(Submit implementation changes), the unresolved review conversations and the written reviews on
the ticket's pull request since its current candidate was published become ``G`` items (G1,
G2...) alongside the ``F`` items from Jira, so nobody has to copy them across. Resolving a
conversation on GitHub leaves it out. A conversation that was already there before the candidate
counts again only if someone added to it since.
"""

from __future__ import annotations

from datetime import datetime

from delivery.ports import GitHubPort, Review, ReviewThread

MAX_ITEMS = 30
MAX_TEXT = 1500


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def describe_thread(t: ReviewThread) -> str:
    first, *replies = t.comments
    where = f"{t.path}:{t.line}" if t.line else t.path or "the pull request"
    text = f"GitHub review comment by @{first.author_login} on {where}: {_clip(first.body)}"
    for r in replies:
        text += f" / @{r.author_login} replied: {_clip(r.body, 600)}"
    if first.url:
        text += f" ({first.url})"
    return text


def describe_review(r: Review) -> str:
    verdict = "requested changes" if r.state == "CHANGES_REQUESTED" else "commented"
    return f"GitHub review by @{r.user_login} ({verdict}): {_clip(r.body)}"


def _since(when: datetime | None, since: datetime | None) -> bool:
    return since is None or (when is not None and when >= since)


async def review_items(github: GitHubPort, pr_number: int, since: datetime | None) -> dict[str, str]:
    """``G1``... for the PR's unresolved conversations and written reviews since ``since``."""
    texts: list[str] = []
    for t in await github.review_threads(pr_number):
        if t.resolved or not t.comments:
            continue
        if not any(_since(c.created_at, since) for c in t.comments):
            continue
        texts.append(describe_thread(t))
    for r in await github.reviews(pr_number):
        if r.state not in ("CHANGES_REQUESTED", "COMMENTED") or not r.body.strip():
            continue
        if r.user_type == "Bot" or not _since(r.submitted_at, since):
            continue
        texts.append(describe_review(r))
    return {f"G{i}": text for i, text in enumerate(texts[:MAX_ITEMS], 1)}


__all__ = ["describe_review", "describe_thread", "review_items"]
