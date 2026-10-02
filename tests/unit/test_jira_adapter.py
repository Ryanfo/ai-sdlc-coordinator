from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conftest import ConfigFactory
from delivery.jira import JiraClient, JiraCredentialsMissing
from delivery.ports import AuthError, IntegrationError, NotFound, UncertainResult


def issue(key: str, status_id: str = "10001", assignee: str = "dev-account-0001") -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "summary": f"S {key}",
            "status": {"id": status_id, "name": "Ready for refinement"},
            "assignee": {"accountId": assignee, "displayName": "Dev"},
            "issuetype": {"name": "Story"},
            "labels": ["agent-enabled"],
            "project": {"key": "PILOT"},
            "description": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "AC1: works"}]}],
            },
            "created": "2026-10-01T09:00:00.000+0100",
            "updated": "2026-10-01T10:00:00.000+0100",
            "issuelinks": [
                {
                    "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    "inwardIssue": {"key": "PILOT-9"},
                }
            ],
            "customfield_10050": {"value": "planning"},
        },
    }


@pytest.fixture
def client_factory(make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "ATATT3xFfGF0testtokenvalue")
    cfg = make_config()

    def make(handler):  # type: ignore[no-untyped-def]
        return JiraClient(cfg, transport=httpx.MockTransport(handler))

    return make


def test_missing_credentials_are_reported_by_name(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    with pytest.raises(JiraCredentialsMissing, match="JIRA_EMAIL"):
        JiraClient(make_config())


async def test_enhanced_search_follows_next_page_token(client_factory) -> None:  # type: ignore[no-untyped-def]
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/rest/api/3/search/jql"  # never the deprecated /search
        body = json.loads(req.content)
        seen.append(body)
        if "nextPageToken" not in body:
            return httpx.Response(
                200, json={"issues": [issue("PILOT-1")], "nextPageToken": "t2", "isLast": False}
            )
        return httpx.Response(200, json={"issues": [issue("PILOT-2")], "isLast": True})

    jira = client_factory(handler)
    issues = await jira.search("project = PILOT")
    assert [i.key for i in issues] == ["PILOT-1", "PILOT-2"]
    assert seen[1]["nextPageToken"] == "t2" and "customfield_10050" in seen[0]["fields"]
    v = issues[0].view
    assert v.assignee_account_id == "dev-account-0001" and v.resume_stage == "planning"
    assert issues[0].description_text == "AC1: works"
    assert issues[0].links[0].description == "is blocked by" and issues[0].links[0].other_key == "PILOT-9"
    assert issues[0].created and issues[0].created.utcoffset() is not None
    await jira.close()


async def test_comments_and_changelog_paginate(client_factory) -> None:  # type: ignore[no-untyped-def]
    def handler(req: httpx.Request) -> httpx.Response:
        start = int(req.url.params.get("startAt", "0"))
        if req.url.path.endswith("/comment"):
            comments = [
                {
                    "id": str(i),
                    "author": {"accountId": "a"},
                    "created": "2026-10-01T09:00:00.000+0000",
                    "updated": "2026-10-01T09:00:00.000+0000",
                    "body": {
                        "type": "doc",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"c{i}"}]}],
                    },
                }
                for i in range(start, min(start + 2, 3))
            ]
            return httpx.Response(200, json={"comments": comments, "total": 3, "startAt": start})
        values = [
            {
                "id": f"h{start}",
                "author": {"accountId": "a"},
                "created": "2026-10-01T09:00:00.000+0000",
                "items": [
                    {
                        "field": "status",
                        "fieldId": "status",
                        "from": "1",
                        "to": "2",
                        "fromString": "A",
                        "toString": "B",
                    },
                    {"field": "labels", "fieldId": "labels", "from": None, "to": None},
                ],
            }
        ]
        return httpx.Response(200, json={"values": values, "isLast": start >= 1, "startAt": start})

    jira = client_factory(handler)
    comments = await jira.comments("PILOT-1")
    assert [c.body_text for c in comments] == ["c0", "c1", "c2"]
    changes = await jira.status_changes("PILOT-1")
    assert [c.history_id for c in changes] == ["h0", "h1"] and changes[0].to_id == "2"
    await jira.close()


async def test_reads_retry_with_retry_after_and_auth_fails_fast(client_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("delivery.jira.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
        return httpx.Response(200, json={"accountId": "dev-account-0001", "displayName": "Dev"})

    jira = client_factory(handler)
    me = await jira.myself()
    assert me.account_id == "dev-account-0001" and sleeps and sleeps[0] >= 7
    auth = client_factory(lambda r: httpx.Response(401, text="nope"))
    with pytest.raises(AuthError):
        await auth.myself()
    missing = client_factory(lambda r: httpx.Response(404, text="none"))
    assert await missing.get_property("PILOT-1", "delivery.execution") is None
    with pytest.raises(NotFound):
        await missing.get_issue("PILOT-404")


async def test_writes_are_not_retried_and_lost_responses_are_uncertain(client_factory) -> None:  # type: ignore[no-untyped-def]
    calls = {"n": 0}

    def timeout(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("lost", request=req)

    jira = client_factory(timeout)
    with pytest.raises(UncertainResult):
        await jira.add_comment("PILOT-1", {"type": "doc", "version": 1, "content": []})
    assert calls["n"] == 1  # no blind retry

    def refused(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(IntegrationError) as exc:
        await client_factory(refused).do_transition("PILOT-1", "21")
    assert not isinstance(exc.value, UncertainResult)  # never sent: definitely not applied
    server = client_factory(lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(UncertainResult):
        await server.set_property("PILOT-1", "delivery.execution", {"a": 1})


async def test_transition_property_and_field_payloads(client_factory) -> None:  # type: ignore[no-untyped-def]
    sent: list[tuple[str, str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path.endswith("/transitions") and req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "31", "name": "Start refinement", "to": {"id": "10002", "name": "Refining"}}
                    ]
                },
            )
        return httpx.Response(204)

    jira = client_factory(handler)
    t = await jira.transitions("PILOT-1")
    assert t[0].id == "31" and t[0].to_status_id == "10002"
    await jira.do_transition("PILOT-1", "31")
    await jira.set_property("PILOT-1", "delivery.execution", {"x": 1})
    await jira.set_fields("PILOT-1", {"customfield_10050": {"value": "refinement"}})
    assert sent[1] == ("POST", "/rest/api/3/issue/PILOT-1/transitions", {"transition": {"id": "31"}})
    assert sent[2] == ("PUT", "/rest/api/3/issue/PILOT-1/properties/delivery.execution", {"x": 1})
    assert sent[3][2] == {"fields": {"customfield_10050": {"value": "refinement"}}}
