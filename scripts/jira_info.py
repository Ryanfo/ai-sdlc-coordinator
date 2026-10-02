"""Read-only Jira lookup used during setup: who am I, project type and issue types.

Usage: uv run python scripts/jira_info.py <config>   (JIRA_EMAIL / JIRA_API_TOKEN in the environment)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from delivery.config import load_config
from delivery.jira import JiraClient


async def main() -> None:
    cfg = load_config(Path(sys.argv[1]))
    jira = JiraClient(cfg)
    try:
        me = await jira.myself()
        project = (await jira._read("GET", f"/rest/api/3/project/{cfg.jira.project_key}")).json()
    finally:
        await jira.close()
    print(f"\nAuthenticated as: {me.display_name}  account ID: {me.account_id}")
    print(f"Project {project.get('key')}: {project.get('name')!r}  type={project.get('projectTypeKey')}"
          f"  style={project.get('style')}  (classic = company-managed, next-gen = team-managed)")
    print("Issue types:", ", ".join(sorted({t.get('name', '') for t in project.get('issueTypes', [])})))


asyncio.run(main())
