"""`delivery revert <ticket>`: take a released change back out, and track the rework.

When a released ticket turns out to be wrong in use, one command, run by a person, does three
things and nothing more:

* opens a pull request that reverts the ticket's merged PR, as GitHub's Revert button does (for
  merge, squash and rebase merges alike). People review and merge it as usual: the coordinator
  never merges or deploys;
* creates a Bug in Backlog, unassigned and linked to the ticket, saying why, so the rework goes
  through delivery like any other bug (its refinement gets the ticket's approved specification
  and plan as linked context);
* comments on the ticket with both links.

It runs once per ticket (``<state_dir>/reverts``); ``--again`` opens another revert.
"""

from __future__ import annotations

import json

from delivery.adf import markdown_to_adf
from delivery.config import Config
from delivery.intake import load_record
from delivery.journal import RunJournal, atomic_write_json, ensure_private_dir
from delivery.ports import GitHubPort, JiraPort
from delivery.publication import Publisher


class RevertError(Exception):
    """The ticket cannot be reverted this way (the message says why)."""


def done_before(cfg: Config, key: str) -> dict[str, str] | None:
    try:
        return dict(json.loads((cfg.runtime.state_dir / "reverts" / f"{key}.json").read_text()))
    except (OSError, ValueError):
        return None


async def revert_release(
    cfg: Config, jira: JiraPort, github: GitHubPort, key: str, reason: str
) -> dict[str, str]:
    rec = await load_record(jira, cfg, key)
    record = rec.release.get("record") or {}
    if not rec.pr_number:
        raise RevertError(f"{key} has no pull request recorded")
    pr = await github.get_pr(rec.pr_number)
    if not pr.merged:
        raise RevertError(f"PR #{pr.number} of {key} is not merged, so there is nothing to revert")
    issue = await jira.get_issue(key)
    released = str(record.get("commit") or pr.merge_commit_sha or "")
    why = reason.strip() or "(no reason given)"
    revert = await github.revert_pr(
        pr.number,
        f"Revert {key}: {issue.view.summary}",
        f"Reverts #{pr.number} ({key}), released in `{released[:12]}`.\n\nWhy: {why}\n\n"
        f"Opened with `delivery revert {key}`. Review and merge it as usual to take the change out.",
    )
    bug_type = cfg.flow.bug_types[0] if cfg.flow.bug_types else "Bug"
    description = (
        f"{key} ({issue.view.summary}) was released in `{released[:12]}` and is being reverted.\n\n"
        f"Why: {why}\n\n"
        f"The revert: {revert.url}. The original change: {pr.url}.\n\n"
        f"Describe what the reworked change must do differently, then submit this ticket for "
        f"refinement: it gets {key}'s approved specification and plan as linked context."
    )
    bug = await jira.create_issue(
        cfg.jira.project_key,
        bug_type,
        f"Rework {key}: {issue.view.summary}",
        markdown_to_adf(description),
        [],
    )
    await jira.link_issues(cfg.flow.link_type, key, bug)
    journal = RunJournal(ensure_private_dir(cfg.runtime.state_dir / "reverts" / key))
    pub = Publisher(cfg, jira, None, None, journal, f"revert-{key}")
    await pub.comment(
        key,
        "revert",
        "\n".join(
            [
                "## Release being reverted",
                f"Why: {why}",
                f"- Revert pull request: {revert.url} (merge it to take the change out; nothing is "
                "merged automatically)",
                f"- Rework: {bug}, in Backlog, linked to this ticket",
            ]
        ),
        str(revert.number),
    )
    done = {"revert_pr": revert.url, "bug": bug, "released": released}
    atomic_write_json(cfg.runtime.state_dir / "reverts" / f"{key}.json", done)
    return done


__all__ = ["RevertError", "done_before", "revert_release"]
