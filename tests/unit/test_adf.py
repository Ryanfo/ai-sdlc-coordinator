from __future__ import annotations

from delivery.adf import adf_to_text, markdown_to_adf
from delivery.feedback import parse_decision


def test_roundtrip_preserves_tokens_lists_and_code() -> None:
    md = (
        "## Specification ready: PILOT-1-SPEC-v2\n"
        "Read [v2](https://github.com/o/r/blob/abc/docs/x.md) then decide.\n\n"
        "- Approve: comment the block below\n- Then choose **Approve specification**\n\n"
        "```\nAPPROVE SPEC PILOT-1-SPEC-v2\n```\n\n1. one\n2. two\n\n---\n`delivery-op: x`"
    )
    doc = markdown_to_adf(md)
    assert doc["type"] == "doc" and doc["version"] == 1
    types = [b["type"] for b in doc["content"]]
    assert types == [
        "heading",
        "paragraph",
        "bulletList",
        "codeBlock",
        "orderedList",
        "rule",
        "paragraph",
    ]
    text = adf_to_text(doc)
    assert "APPROVE SPEC PILOT-1-SPEC-v2" in text.splitlines()
    assert "v2 (https://github.com/o/r/blob/abc/docs/x.md)" in text
    assert "- Then choose Approve specification" in text
    assert "1. one" in text and "delivery-op: x" in text


def test_human_comment_shapes_parse_as_decisions() -> None:
    # Enter creates paragraphs; Shift+Enter creates hard breaks; code blocks when pasted.
    paragraphs = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "CREATE TICKETS PILOT-1-SPEC-v1"}],
            },
            {"type": "paragraph", "content": [{"type": "text", "text": "S1: yes"}]},
        ],
    }
    hard = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "CREATE TICKETS PILOT-1-SPEC-v1"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "S1: yes"},
                ],
            }
        ],
    }
    code = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "codeBlock",
                "content": [{"type": "text", "text": "CREATE TICKETS PILOT-1-SPEC-v1\nS1: yes"}],
            }
        ],
    }
    for doc in (paragraphs, hard, code):
        d = parse_decision(adf_to_text(doc))
        assert d is not None and d.items == {"S1": "yes"}


def test_unknown_nodes_and_mentions_do_not_crash() -> None:
    doc = {
        "type": "doc",
        "content": [
            {"type": "mediaSingle", "content": [{"type": "media", "attrs": {}}]},
            {"type": "paragraph", "content": [{"type": "mention", "attrs": {"text": "@Ryan"}}]},
        ],
    }
    assert "@Ryan" in adf_to_text(doc)
    assert adf_to_text(None) == ""
