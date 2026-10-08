"""What a merged PR released: does the merge commit contain exactly the accepted candidate?

The release is the human merge of the ticket's PR (the coordinator never merges or deploys). When
the supervisor sees the merge it asks this module whether what landed on the base branch is the
candidate that was verified, reviewed and accepted, and says so in the Done comment. A difference
is flagged there and never holds the ticket back: the merge has happened and nothing here can
undo it (``delivery revert`` opens a revert PR if the release should not stand).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from delivery.git import ManagedRepo
from delivery.ports import GitHubPort


@dataclass(frozen=True)
class Provenance:
    ok: bool  # the merge commit contains the accepted candidate (possibly with merged-in base)
    summary: str  # one line for the Done comment
    warning: bool = False  # ok, but someone should look at the released commit
    info: dict[str, Any] = field(default_factory=dict)


async def establish(
    repo: ManagedRepo,
    github: GitHubPort,
    *,
    base_branch: str,
    pr_number: int | None,
    candidate: str,
) -> Provenance:
    """Establish that the PR's merge commit contains exactly the accepted ``candidate``."""
    await repo.fetch()
    if pr_number is None:
        return Provenance(False, "no pull request is recorded for this ticket")
    pr = await github.get_pr(pr_number)
    info: dict[str, Any] = {
        "pr": pr.number,
        "pr_head": pr.head_sha,
        "merged": pr.merged,
        "merge_commit": pr.merge_commit_sha,
        "merged_by": pr.merged_by,
        "candidate": candidate,
    }
    if not pr.merged or not pr.merge_commit_sha:
        return Provenance(False, f"PR #{pr.number} is not merged", info=info)
    merge = pr.merge_commit_sha
    base_sha = await repo.remote_sha(base_branch) or ""
    resolved: list[str] = []
    if pr.head_sha != candidate:
        # Conflicts are resolved when merging (e.g. GitHub's Resolve conflicts), which merges
        # the base into the PR branch. Accept only that: no other commits on top.
        extra = await _merge_only_update(repo, candidate, pr.head_sha, f"{merge}^1")
        if extra is None:
            return Provenance(
                False,
                f"the merged PR head {pr.head_sha[:12]} is not the accepted candidate "
                f"{candidate[:12]}: unapproved changes were merged",
                info=info,
            )
        resolved = extra
        info["merged_base_into_candidate"] = pr.head_sha
        info["resolved_paths"] = resolved
        candidate = pr.head_sha
    if not await repo.resolve(merge):
        return Provenance(False, f"merge commit {merge[:12]} does not exist in the repository", info=info)
    if not await repo.is_ancestor(merge, base_sha):
        return Provenance(False, f"merge commit {merge[:12]} is not on {base_branch}", info=info)
    details = await repo.commit_details(merge)
    strategy = "unknown"
    exact = False
    if len(details.parents) == 2 and details.parents[1] == candidate:
        strategy, exact = "merge commit", True
    else:
        # Squash or rebase: the merged tree must equal the candidate merged onto some first-parent
        # ancestor of the merge commit (the base the human merged onto).
        probe = merge
        for _ in range(60):
            parents = (await repo.commit_details(probe)).parents
            if not parents:
                break
            before = parents[0]
            mt = await repo.git("merge-tree", "--write-tree", before, candidate, check=False)
            expected_tree = mt.stdout.split("\n", 1)[0].strip() if mt.returncode == 0 else ""
            if expected_tree and expected_tree == details.tree:
                strategy, exact = ("squash" if probe == merge else "rebase"), True
                break
            probe = before
    info.update({"strategy": strategy, "merged_tree_matches_candidate": exact})
    if resolved:
        return Provenance(
            True,
            f"the accepted candidate was merged with {base_branch} when merging (conflict resolution); "
            f"check the resolved files: {', '.join(resolved)}",
            warning=True,
            info=info,
        )
    if not exact:
        return Provenance(
            True,
            "the merge differs from the reviewed candidate (its tree is not the candidate merged onto "
            "the base); run the checks on the released commit",
            warning=True,
            info=info,
        )
    return Provenance(True, f"{strategy}; the released commit contains the accepted candidate", info=info)


async def _merge_only_update(
    repo: ManagedRepo, candidate: str, head: str, base_before: str
) -> list[str] | None:
    """If ``head`` is ``candidate`` plus merges of the base branch (as it was before the PR
    merged) only, the files those merges changed beyond a plain merge (the conflict
    resolutions); otherwise None."""
    if not await repo.is_ancestor(candidate, head):
        return None
    own = await repo.git("rev-list", "--no-merges", head, f"^{candidate}", f"^{base_before}", check=False)
    if own.returncode != 0 or own.stdout.strip():
        return None  # commits that are neither the candidate's nor the base's
    merges = await repo.git("rev-list", "--merges", head, f"^{candidate}", check=False)
    paths: set[str] = set()
    for sha in merges.stdout.split():
        # `--cc` shows only what a merge changed beyond its parents: the resolutions.
        cc = await repo.git("show", "--cc", "--name-only", "--format=", sha, check=False)
        paths.update(n for n in cc.stdout.splitlines() if n.strip())
    return sorted(paths)
