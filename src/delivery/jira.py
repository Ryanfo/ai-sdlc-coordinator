"""Jira Cloud REST v3 adapter (httpx).

* Enhanced JQL search (``/rest/api/3/search/jql`` with ``nextPageToken``); the legacy
  ``/rest/api/3/search`` endpoint is deprecated and not used.
* Reads retry with bounded exponential backoff, jitter and ``Retry-After``.
* Writes are never retried blindly: a response lost after the request was sent raises
  :class:`UncertainResult` so the caller reconciles by querying Jira first.
* Authentication and permission errors surface immediately.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from delivery.adf import adf_to_text
from delivery.config import Config
from delivery.credentials import CredentialsMissing, JiraCredentials, Runner, resolve_jira
from delivery.ownership import IssueView
from delivery.ports import (
    Attachment,
    AuthError,
    IntegrationError,
    IssueLink,
    JiraComment,
    JiraFieldInfo,
    JiraIssue,
    JiraStatusInfo,
    JiraTransition,
    JiraUser,
    NotFound,
    StatusChange,
    UncertainResult,
)

PAGE = 100
READ_ATTEMPTS = 5


def _ts(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0).astimezone()
    # Jira uses e.g. 2026-10-01T09:00:00.000+0100
    v = value.replace("Z", "+00:00")
    if len(v) > 5 and v[-5] in "+-" and v[-3] != ":":
        v = v[:-2] + ":" + v[-2:]
    return datetime.fromisoformat(v)


class JiraCredentialsMissing(AuthError):
    pass


class JiraClient:
    def __init__(
        self,
        cfg: Config,
        transport: httpx.AsyncBaseTransport | None = None,
        keychain: Runner | None = None,
        credentials: JiraCredentials | None = None,
    ) -> None:
        self.cfg = cfg
        try:
            creds = credentials or resolve_jira(cfg, keychain)
        except CredentialsMissing as exc:
            raise JiraCredentialsMissing(str(exc)) from None
        self.credential_source = creds.source
        if cfg.jira.auth_profile == "scoped_api_token":
            base = f"https://api.atlassian.com/ex/jira/{cfg.jira.cloud_id}"
        else:
            base = cfg.jira.base_url
        self.http = httpx.AsyncClient(
            base_url=base,
            auth=(creds.email, creds.token),
            headers={"Accept": "application/json", "User-Agent": "delivery-coordinator"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
        )
        self._resume_field = cfg.jira.fields.resume_stage

    async def close(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------------ transport
    def _error(self, r: httpx.Response) -> IntegrationError:
        detail = r.text[:400]
        if r.status_code in (401, 403):
            return AuthError(
                f"Jira {r.status_code}: authentication or permission failure: {detail}", status=r.status_code
            )
        if r.status_code == 404:
            return NotFound(f"Jira 404: {detail}", status=404)
        retryable = r.status_code == 429 or r.status_code >= 500
        return IntegrationError(f"Jira {r.status_code}: {detail}", status=r.status_code, retryable=retryable)

    async def _read(self, method: str, url: str, **kw: Any) -> httpx.Response:
        delay = 1.0
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                r = await self.http.request(method, url, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == READ_ATTEMPTS:
                    raise IntegrationError(f"Jira unreachable: {exc}", retryable=True) from None
            else:
                if r.status_code < 400:
                    return r
                err = self._error(r)
                if not err.retryable or attempt == READ_ATTEMPTS:
                    raise err
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 60)
        raise IntegrationError("unreachable")  # pragma: no cover

    async def _write(self, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            r = await self.http.request(method, url, **kw)
        except httpx.ConnectError as exc:
            # The request never reached Jira: nothing was applied.
            raise IntegrationError(f"Jira unreachable: {exc}", retryable=True) from None
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise UncertainResult(f"Jira {method} {url} outcome unknown: {exc}") from None
        if r.status_code >= 500:
            raise UncertainResult(f"Jira {r.status_code} on {method} {url}; outcome unknown")
        if r.status_code >= 400:
            raise self._error(r)
        return r

    # ------------------------------------------------------------------ parsing
    def _issue(self, data: dict[str, Any]) -> JiraIssue:
        f = data.get("fields") or {}
        status = f.get("status") or {}
        assignee = f.get("assignee") or {}
        resume = f.get(self._resume_field) if self._resume_field else None
        links = []
        for link in f.get("issuelinks") or []:
            t = link.get("type") or {}
            if "inwardIssue" in link:
                links.append(
                    IssueLink(t.get("name", ""), "inward", t.get("inward", ""), link["inwardIssue"]["key"])
                )
            if "outwardIssue" in link:
                links.append(
                    IssueLink(t.get("name", ""), "outward", t.get("outward", ""), link["outwardIssue"]["key"])
                )
        view = IssueView(
            key=data["key"],
            project_key=(f.get("project") or {}).get("key", data["key"].split("-")[0]),
            issue_type=(f.get("issuetype") or {}).get("name", ""),
            status_id=str(status.get("id", "")),
            status_name=str(status.get("name", "")),
            assignee_account_id=assignee.get("accountId"),
            labels=tuple(f.get("labels") or ()),
            resume_stage=resume.get("value") if isinstance(resume, dict) else None,
            summary=f.get("summary") or "",
        )
        return JiraIssue(
            view=view,
            description_text=adf_to_text(f.get("description"))
            if isinstance(f.get("description"), dict)
            else str(f.get("description") or ""),
            created=_ts(f.get("created")),
            updated=_ts(f.get("updated")),
            links=tuple(links),
            resolution=(f.get("resolution") or {}).get("name") if f.get("resolution") else None,
            assignee_name=assignee.get("displayName", ""),
            attachments=tuple(
                Attachment(
                    str(a.get("id", "")),
                    str(a.get("filename", "")),
                    str(a.get("mimeType", "")),
                    int(a.get("size") or 0),
                    _ts(a.get("created")) if a.get("created") else None,
                    (a.get("author") or {}).get("accountId"),
                )
                for a in f.get("attachment") or []
                if a.get("id")
            ),
        )

    @property
    def _fields(self) -> list[str]:
        fields = [
            "summary",
            "status",
            "assignee",
            "issuetype",
            "labels",
            "description",
            "project",
            "created",
            "updated",
            "issuelinks",
            "resolution",
            "attachment",
        ]
        if self._resume_field:
            fields.append(self._resume_field)
        return fields

    # ------------------------------------------------------------------ JiraPort
    async def myself(self) -> JiraUser:
        d = (await self._read("GET", "/rest/api/3/myself")).json()
        return JiraUser(
            d["accountId"], d.get("displayName", ""), d.get("active", True), d.get("accountType", "atlassian")
        )

    async def user(self, account_id: str) -> JiraUser | None:
        try:
            d = (await self._read("GET", "/rest/api/3/user", params={"accountId": account_id})).json()
        except NotFound:
            return None
        return JiraUser(
            d["accountId"], d.get("displayName", ""), d.get("active", True), d.get("accountType", "atlassian")
        )

    async def search(self, jql: str) -> list[JiraIssue]:
        out: list[JiraIssue] = []
        token: str | None = None
        while True:
            body: dict[str, Any] = {"jql": jql, "fields": self._fields, "maxResults": PAGE}
            if token:
                body["nextPageToken"] = token
            d = (await self._read("POST", "/rest/api/3/search/jql", json=body)).json()
            out.extend(self._issue(i) for i in d.get("issues", []))
            token = d.get("nextPageToken")
            if d.get("isLast", True) or not token:
                return out

    async def get_issue(self, key: str) -> JiraIssue:
        d = (
            await self._read("GET", f"/rest/api/3/issue/{key}", params={"fields": ",".join(self._fields)})
        ).json()
        return self._issue(d)

    async def comments(self, key: str) -> list[JiraComment]:
        out: list[JiraComment] = []
        start = 0
        while True:
            d = (
                await self._read(
                    "GET",
                    f"/rest/api/3/issue/{key}/comment",
                    params={"startAt": start, "maxResults": PAGE, "orderBy": "created"},
                )
            ).json()
            for c in d.get("comments", []):
                body = c.get("body")
                out.append(
                    JiraComment(
                        id=str(c["id"]),
                        author_account_id=(c.get("author") or {}).get("accountId", ""),
                        created=_ts(c.get("created")),
                        updated=_ts(c.get("updated")),
                        body_text=adf_to_text(body) if isinstance(body, dict) else str(body or ""),
                        author_name=(c.get("author") or {}).get("displayName", ""),
                        body_adf=body if isinstance(body, dict) else None,
                    )
                )
            got = len(d.get("comments", []))
            start += got
            if got == 0 or start >= int(d.get("total", start)):
                return out

    async def status_changes(self, key: str) -> list[StatusChange]:
        out: list[StatusChange] = []
        start = 0
        while True:
            d = (
                await self._read(
                    "GET", f"/rest/api/3/issue/{key}/changelog", params={"startAt": start, "maxResults": PAGE}
                )
            ).json()
            values = d.get("values", [])
            for h in values:
                for item in h.get("items", []):
                    if item.get("fieldId") == "status" or item.get("field") == "status":
                        out.append(
                            StatusChange(
                                history_id=str(h["id"]),
                                author_account_id=(h.get("author") or {}).get("accountId"),
                                created=_ts(h.get("created")),
                                from_id=str(item.get("from") or ""),
                                to_id=str(item.get("to") or ""),
                                from_name=item.get("fromString") or "",
                                to_name=item.get("toString") or "",
                            )
                        )
            start += len(values)
            if d.get("isLast", True) or not values:
                return out

    async def transitions(self, key: str) -> list[JiraTransition]:
        d = (await self._read("GET", f"/rest/api/3/issue/{key}/transitions")).json()
        return [
            JiraTransition(
                str(t["id"]),
                t.get("name", ""),
                str((t.get("to") or {}).get("id", "")),
                (t.get("to") or {}).get("name", ""),
            )
            for t in d.get("transitions", [])
        ]

    async def do_transition(self, key: str, transition_id: str, fields: dict[str, Any] | None = None) -> None:
        body: dict[str, Any] = {"transition": {"id": transition_id}}
        if fields:
            body["fields"] = fields
        await self._write("POST", f"/rest/api/3/issue/{key}/transitions", json=body)

    async def download_attachment(self, attachment_id: str, dest: Path, max_bytes: int) -> int:
        """Stream one attachment to ``dest`` (never more than ``max_bytes``). Returns its size.

        Jira answers with a redirect to its media service; the redirect URL carries its own
        short-lived token, and httpx drops our Authorization header on the cross-host hop.
        """
        tmp = dest.with_name(dest.name + ".part")
        size = 0
        try:
            async with self.http.stream(
                "GET", f"/rest/api/3/attachment/content/{attachment_id}", follow_redirects=True
            ) as r:
                if r.status_code >= 400:
                    await r.aread()
                    raise self._error(r)
                with tmp.open("wb") as fh:
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise IntegrationError(f"attachment {attachment_id} exceeds {max_bytes} bytes")
                        fh.write(chunk)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            tmp.unlink(missing_ok=True)
            raise IntegrationError(
                f"attachment {attachment_id} download failed: {exc}", retryable=True
            ) from None
        except IntegrationError:
            tmp.unlink(missing_ok=True)
            raise
        tmp.chmod(0o600)
        tmp.replace(dest)
        return size

    async def create_issue(
        self, project: str, issue_type: str, summary: str, description: dict[str, Any], labels: list[str]
    ) -> str:
        """Setup and checks only (``workflow verify``); the coordinator never creates tickets."""
        fields = {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": description,
            "labels": labels,
        }
        r = await self._write("POST", "/rest/api/3/issue", json={"fields": fields})
        return str(r.json()["key"])

    async def add_comment(self, key: str, adf: dict[str, Any]) -> JiraComment:
        r = await self._write("POST", f"/rest/api/3/issue/{key}/comment", json={"body": adf})
        c = r.json()
        return JiraComment(
            str(c["id"]),
            (c.get("author") or {}).get("accountId", ""),
            _ts(c.get("created")),
            _ts(c.get("updated")),
            adf_to_text(c.get("body")),
            body_adf=c.get("body"),
        )

    async def get_property(self, key: str, name: str) -> dict[str, Any] | None:
        try:
            d = (await self._read("GET", f"/rest/api/3/issue/{key}/properties/{name}")).json()
        except NotFound:
            return None
        value = d.get("value")
        return value if isinstance(value, dict) else None

    async def set_property(self, key: str, name: str, value: dict[str, Any]) -> None:
        await self._write("PUT", f"/rest/api/3/issue/{key}/properties/{name}", json=value)

    async def set_fields(self, key: str, fields: dict[str, Any]) -> None:
        # notifyUsers=false would need project admin rights; ordinary access is enough here.
        await self._write("PUT", f"/rest/api/3/issue/{key}", json={"fields": fields})

    async def project_statuses(self, project_key: str) -> list[JiraStatusInfo]:
        d = (await self._read("GET", f"/rest/api/3/project/{project_key}/statuses")).json()
        seen: dict[str, JiraStatusInfo] = {}
        for issue_type in d:
            for s in issue_type.get("statuses", []):
                cat = (s.get("statusCategory") or {}).get("key", "")
                seen[str(s["id"])] = JiraStatusInfo(str(s["id"]), s.get("name", ""), cat)
        return list(seen.values())

    async def issue_type_statuses(self, project_key: str) -> dict[str, set[str]]:
        """Status IDs per issue type: team-managed projects can give each type its own workflow."""
        d = (await self._read("GET", f"/rest/api/3/project/{project_key}/statuses")).json()
        return {it.get("name", ""): {str(s["id"]) for s in it.get("statuses", [])} for it in d}

    async def fields(self) -> list[JiraFieldInfo]:
        d = (await self._read("GET", "/rest/api/3/field")).json()
        return [
            JiraFieldInfo(
                f["id"], f.get("name", ""), bool(f.get("custom")), (f.get("schema") or {}).get("type", "")
            )
            for f in d
        ]
