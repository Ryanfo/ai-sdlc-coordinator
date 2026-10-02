"""Atlassian Document Format conversion.

``markdown_to_adf`` supports the small subset the coordinator writes: headings, paragraphs,
bullet and ordered lists, fenced code blocks, rules, inline code, bold and links.
``adf_to_text`` flattens any ADF document (including human-authored comments) to plain
text with one line per block, so decision tokens can be parsed deterministically.
"""

from __future__ import annotations

import re
from typing import Any

Node = dict[str, Any]

_INLINE = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\))")


def _inline(text: str) -> list[Node]:
    nodes: list[Node] = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            nodes.append({"type": "text", "text": part[1:-1], "marks": [{"type": "code"}]})
        elif part.startswith("**") and part.endswith("**") and len(part) > 3:
            nodes.append({"type": "text", "text": part[2:-2], "marks": [{"type": "strong"}]})
        elif m := re.fullmatch(r"\[([^\]]+)\]\(([^)\s]+)\)", part):
            nodes.append(
                {
                    "type": "text",
                    "text": m.group(1),
                    "marks": [{"type": "link", "attrs": {"href": m.group(2)}}],
                }
            )
        else:
            nodes.append({"type": "text", "text": part})
    return nodes


def _paragraph(text: str) -> Node:
    content: list[Node] = []
    for i, line in enumerate(text.split("\n")):
        if i:
            content.append({"type": "hardBreak"})
        content.extend(_inline(line))
    return {"type": "paragraph", "content": content} if content else {"type": "paragraph"}


def markdown_to_adf(markdown: str) -> Node:
    blocks: list[Node] = []
    lines = markdown.replace("\r\n", "\n").split("\n")
    i = 0
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(_paragraph("\n".join(para)))
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            lang = stripped[3:].strip()
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            node: Node = {
                "type": "codeBlock",
                "content": [{"type": "text", "text": "\n".join(code)}],
            }
            if not code:
                node = {"type": "codeBlock"}
            if lang:
                node["attrs"] = {"language": lang}
            blocks.append(node)
            i += 1
            continue
        if m := re.match(r"^(#{1,6})\s+(.*)$", stripped):
            flush()
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": len(m.group(1))},
                    "content": _inline(m.group(2)),
                }
            )
        elif stripped in ("---", "***"):
            flush()
            blocks.append({"type": "rule"})
        elif re.match(r"^[-*]\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
            flush()
            ordered = bool(re.match(r"^\d+\.\s+", stripped))
            items: list[Node] = []
            while i < len(lines):
                s = lines[i].strip()
                m2 = re.match(r"^\d+\.\s+(.*)$", s) if ordered else re.match(r"^[-*]\s+(.*)$", s)
                if not m2:
                    break
                items.append({"type": "listItem", "content": [_paragraph(m2.group(1))]})
                i += 1
            blocks.append({"type": "orderedList" if ordered else "bulletList", "content": items})
            continue
        elif not stripped:
            flush()
        else:
            para.append(line.rstrip())
        i += 1
    flush()
    return {"type": "doc", "version": 1, "content": blocks}


def _text_of(node: Node) -> str:
    t = node.get("type")
    if t == "text":
        text = str(node.get("text", ""))
        for mark in node.get("marks", []) or []:
            if mark.get("type") == "link":
                href = (mark.get("attrs") or {}).get("href", "")
                if href and href != text:
                    text = f"{text} ({href})"
        return text
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return str((node.get("attrs") or {}).get("text", "@user"))
    if t in ("inlineCard", "blockCard"):
        return str((node.get("attrs") or {}).get("url", ""))
    if t == "emoji":
        return str((node.get("attrs") or {}).get("text", ""))
    if t in ("media", "mediaInline"):
        attrs = node.get("attrs") or {}
        return f"[attachment: {attrs.get('alt') or attrs.get('id') or 'file'}]"
    return "".join(_text_of(c) for c in node.get("content", []) or [])


def adf_to_text(doc: Node | None) -> str:
    if not doc:
        return ""
    lines: list[str] = []

    def block(node: Node, prefix: str = "") -> None:
        t = node.get("type")
        children = node.get("content", []) or []
        if t in (
            "doc",
            "panel",
            "expand",
            "nestedExpand",
            "blockquote",
            "layoutSection",
            "layoutColumn",
            "table",
            "tableRow",
            "tableCell",
            "tableHeader",
        ):
            for c in children:
                block(c, prefix)
        elif t in ("bulletList", "orderedList"):
            for n, item in enumerate(children, start=1):
                marker = f"{n}. " if t == "orderedList" else "- "
                for j, c in enumerate(item.get("content", []) or []):
                    block(c, prefix + (marker if j == 0 else "  "))
        elif t == "codeBlock":
            text = "".join(_text_of(c) for c in children)
            lines.extend(prefix + ln for ln in text.split("\n"))
        elif t == "rule":
            lines.append("---")
        else:
            text = _text_of(node)
            for ln in text.split("\n"):
                lines.append(prefix + ln)

    block(doc)
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()
