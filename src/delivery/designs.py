"""Figma designs linked from a ticket, snapshotted for Claude as read-only inputs.

How a design is linked: in Figma, select a frame and choose "Copy link to selection", then
paste the link into the Jira ticket description (one link per frame or state). A link to a
page exports that page's top-level frames; a link to a whole file is skipped because it does
not say which design is meant.

What Claude gets for each frame, in the run's inputs directory:

* ``<frame>.png`` - the frame rendered by Figma (2x by default),
* ``<frame>.summary.md`` - copy text in reading order, typography, colours, layout, components,
* ``<frame>.json`` - a condensed layer tree (sizes, auto-layout, fills, text styles).

Version pinning: refinement fetches the current version of each linked file and records it in
the ticket's shared execution record. Planning, development and verification fetch that same
version, so what is built and checked is what the specification was written from. If the
linked frames have changed in Figma since, the stage is told and the ticket gets a comment;
adopting the new design is a scope decision for a human (Revise scope).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from delivery.config import FigmaConfig
from delivery.figma import FigmaPort
from delivery.models import DesignRef, SkippedDesign
from delivery.ports import AuthError, IntegrationError, NotFound

MB = 1024 * 1024
FIGMA_URL = re.compile(
    r"https?://(?:www\.)?figma\.com/(?:design|file|proto|board)/[A-Za-z0-9]{10,64}[^\s<>\"'\])]*"
)
_PATH = re.compile(
    r"^/(?P<kind>design|file|proto|board)/(?P<key>[A-Za-z0-9]{10,64})(?:/branch/(?P<branch>[A-Za-z0-9]{10,64}))?"
)
FRAME_TYPES = {"FRAME", "COMPONENT", "COMPONENT_SET", "SECTION", "INSTANCE", "GROUP"}
MAX_NODES = 2500


@dataclass(frozen=True)
class FigmaLink:
    url: str
    file_key: str
    node_id: str | None


def normalise_node_id(raw: str) -> str:
    v = unquote(raw).strip()
    if re.fullmatch(r"\d+-\d+", v):
        return v.replace("-", ":")
    return v


def figma_links(text: str) -> list[FigmaLink]:
    """Every distinct Figma link in the text, in order of appearance."""
    out: list[FigmaLink] = []
    seen: set[tuple[str, str | None]] = set()
    for m in FIGMA_URL.finditer(text):
        raw = m.group(0).rstrip(".,;:")
        parts = urlsplit(raw)
        pm = _PATH.match(parts.path)
        if not pm:
            continue
        key = pm.group("branch") or pm.group("key")
        node = parse_qs(parts.query).get("node-id", [None])[0]
        node_id = normalise_node_id(node) if node else None
        if (key, node_id) in seen:
            continue
        seen.add((key, node_id))
        out.append(FigmaLink(raw.split("&t=")[0], key, node_id))
    return out


# --------------------------------------------------------------------------- condensing


def _hex(c: dict[str, Any], opacity: float = 1.0) -> str:
    r, g, b = (round(float(c.get(k, 0)) * 255) for k in ("r", "g", "b"))
    a = float(c.get("a", 1)) * opacity
    return f"#{r:02X}{g:02X}{b:02X}" + ("" if a >= 0.999 else f"{round(a * 255):02X}")


def _paints(paints: list[dict[str, Any]] | None) -> list[str]:
    out = []
    for p in paints or []:
        if p.get("visible") is False:
            continue
        t = p.get("type")
        if t == "SOLID":
            out.append(_hex(p.get("color") or {}, float(p.get("opacity", 1))))
        elif t == "IMAGE":
            out.append("image")
        elif t and t.startswith("GRADIENT"):
            out.append(t.lower())
    return out


def condense(node: dict[str, Any], components: dict[str, Any], budget: list[int]) -> dict[str, Any] | None:
    """The parts of a Figma layer that matter for building UI, without vector geometry."""
    if node.get("visible") is False or budget[0] <= 0:
        return None
    budget[0] -= 1
    out: dict[str, Any] = {"name": node.get("name", ""), "type": node.get("type", "")}
    box = node.get("absoluteBoundingBox") or {}
    if box:
        out["box"] = [round(float(box.get(k, 0))) for k in ("x", "y", "width", "height")]
    if node.get("layoutMode") in ("HORIZONTAL", "VERTICAL"):
        out["layout"] = {
            "direction": node["layoutMode"].lower(),
            "gap": node.get("itemSpacing", 0),
            "padding": [node.get(f"padding{s}", 0) for s in ("Top", "Right", "Bottom", "Left")],
            "align": [node.get("primaryAxisAlignItems", "MIN"), node.get("counterAxisAlignItems", "MIN")],
        }
    fills = _paints(node.get("fills"))
    if fills:
        out["fills"] = fills
    strokes = _paints(node.get("strokes"))
    if strokes:
        out["strokes"] = strokes
        out["stroke_weight"] = node.get("strokeWeight")
    if node.get("cornerRadius"):
        out["radius"] = node["cornerRadius"]
    if node.get("opacity", 1) < 1:
        out["opacity"] = round(float(node["opacity"]), 2)
    effects = [e.get("type", "").lower() for e in node.get("effects") or [] if e.get("visible", True)]
    if effects:
        out["effects"] = effects
    if node.get("type") == "TEXT":
        st = node.get("style") or {}
        out["text"] = node.get("characters", "")
        out["font"] = {
            "family": st.get("fontFamily"),
            "weight": st.get("fontWeight"),
            "size": st.get("fontSize"),
            "line_height": round(float(st["lineHeightPx"]), 1) if st.get("lineHeightPx") else None,
            "letter_spacing": st.get("letterSpacing") or None,
            "case": st.get("textCase"),
            "align": st.get("textAlignHorizontal"),
        }
    if node.get("type") == "INSTANCE" and node.get("componentId"):
        out["component"] = (components.get(node["componentId"]) or {}).get("name", node["componentId"])
    kids = [c for c in (condense(k, components, budget) for k in node.get("children") or []) if c]
    if kids:
        out["children"] = kids
    if budget[0] <= 0 and node.get("children") and not kids:
        out["truncated"] = True
    return out


def _walk(n: dict[str, Any]) -> list[dict[str, Any]]:
    out = [n]
    for c in n.get("children", []):
        out += _walk(c)
    return out


def summarise(frame: dict[str, Any], *, url: str, file_name: str, version: str, modified: str) -> str:
    nodes = _walk(frame)
    texts = sorted(
        (n for n in nodes if "text" in n and n["text"].strip()),
        key=lambda n: (n.get("box", [0, 0])[1], n.get("box", [0, 0])[0]),
    )
    w, h = (frame.get("box") or [0, 0, 0, 0])[2:4]
    lines = [
        f"# {frame.get('name')} (Figma design)",
        f"Source: {url}",
        f"File: {file_name} - version {version} - last modified {modified}",
        f"Frame size: {w} x {h}",
    ]
    if frame.get("layout"):
        lay = frame["layout"]
        padding = " ".join(map(str, lay["padding"]))
        lines.append(f"Layout: {lay['direction']} auto-layout, gap {lay['gap']}, padding {padding}")

    def font(n: dict[str, Any]) -> str:
        f = n.get("font") or {}
        lh = f"/{f['line_height']:g}" if f.get("line_height") else ""
        return f"{f.get('family')} {f.get('size')}{lh} weight {f.get('weight')}"

    lines += ["", "## Copy (top to bottom)"]
    lines += [f'- "{n["text"]}" - {font(n)}, colour {", ".join(n.get("fills", [])) or "-"}' for n in texts]
    colours = Counter(
        c for n in nodes for c in n.get("fills", []) + n.get("strokes", []) if c.startswith("#")
    )
    lines += ["", "## Colours"] + [f"- {c} (x{k})" for c, k in colours.most_common(20)]
    fonts = Counter(font(n) for n in texts)
    lines += ["", "## Typography"] + [f"- {f} (x{k})" for f, k in fonts.most_common(12)]
    comps = Counter(n["component"] for n in nodes if n.get("component"))
    if comps:
        lines += ["", "## Components used"] + [f"- {c} (x{k})" for c, k in comps.most_common(30)]
    if any(n.get("truncated") for n in nodes):
        lines += ["", f"(Layer tree truncated after {MAX_NODES} layers; see the PNG for the rest.)"]
    return "\n".join(lines) + "\n"


def _fingerprint(frame: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(frame, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- fetching


@dataclass
class DesignFetch:
    refs: list[DesignRef] = field(default_factory=list)
    skipped: list[SkippedDesign] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    changed: list[DesignRef] = field(default_factory=list)


def _write(path: Path, text: str) -> None:
    path.unlink(missing_ok=True)  # an earlier attempt of this run left it read-only
    path.write_text(text)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._")[:80] or "frame"


async def fetch_designs(
    cfg: FigmaConfig,
    figma: FigmaPort | None,
    links: list[FigmaLink],
    dest: Path,
    pinned: dict[str, str],
) -> DesignFetch:
    out = DesignFetch()
    if not links:
        return out

    def skip(link: FigmaLink, reason: str) -> None:
        out.skipped.append(SkippedDesign(url=link.url, reason=reason))

    if not cfg.enabled or figma is None:
        reason = (
            "Figma integration is disabled in the config"
            if not cfg.enabled
            else "no Figma token is configured (run `delivery credentials set figma`)"
        )
        for link in links:
            skip(link, reason)
        return out
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    by_file: dict[str, list[FigmaLink]] = {}
    for link in links:
        if link.node_id is None:
            skip(
                link, "link points at a whole file; in Figma select the frame and use Copy link to selection"
            )
        else:
            by_file.setdefault(link.file_key, []).append(link)
    budget_frames = cfg.max_frames
    for file_key, file_links in by_file.items():
        try:
            info = await figma.file_info(file_key)
        except (AuthError, NotFound) as exc:
            for link in file_links:
                skip(link, f"Figma refused access to the file: {exc}")
            continue
        current = str(info.get("version", ""))
        version = pinned.get(file_key) or current
        out.versions[file_key] = version
        ids = [link.node_id for link in file_links if link.node_id]
        try:
            data = await figma.nodes(file_key, ids, version)
        except (AuthError, NotFound) as exc:
            for link in file_links:
                skip(link, f"Figma could not read the linked frames at version {version}: {exc}")
            continue
        components: dict[str, Any] = {}
        frames: list[tuple[FigmaLink, dict[str, Any]]] = []
        for link in file_links:
            entry = (data.get("nodes") or {}).get(link.node_id or "")
            if not entry or not entry.get("document"):
                skip(link, "the linked frame no longer exists in this version of the file")
                continue
            components.update(entry.get("components") or {})
            doc = entry["document"]
            if doc.get("type") == "CANVAS":  # a page: its top-level frames
                kids = [
                    k
                    for k in doc.get("children") or []
                    if k.get("type") in FRAME_TYPES and k.get("visible", True)
                ]
                if not kids:
                    skip(link, "the linked page has no frames")
                frames += [(link, k) for k in kids]
            else:
                frames.append((link, doc))
        if len(frames) > budget_frames:
            for link, node in frames[budget_frames:]:
                skip(
                    link,
                    f"frame {node.get('name')!r} is over the limit of {cfg.max_frames} frames per ticket",
                )
            frames = frames[:budget_frames]
        budget_frames -= len(frames)
        if not frames:
            continue
        renders = await figma.render(file_key, [n["id"] for _, n in frames], version, cfg.image_scale)
        current_frames: dict[str, dict[str, Any]] = {}
        if pinned.get(file_key) and current and current != version:
            try:
                now = await figma.nodes(file_key, [n["id"] for _, n in frames], current)
                for nid, entry in (now.get("nodes") or {}).items():
                    if entry and entry.get("document"):
                        current_frames[nid] = (
                            condense(entry["document"], entry.get("components") or {}, [MAX_NODES]) or {}
                        )
            except IntegrationError:
                current_frames = {}
        for link, node in frames:
            stem = f"{file_key}-{_safe(node['id'])}-{_safe(node.get('name', ''))}"
            condensed = condense(node, components, [MAX_NODES]) or {}
            image_url = renders.get(node["id"])
            if not image_url:
                skip(link, f"Figma could not render frame {node.get('name')!r}")
                continue
            png = dest / f"{stem}.png"
            png.unlink(missing_ok=True)  # an earlier attempt of this run left it read-only
            await figma.download(image_url, png, cfg.max_image_mb * MB)
            if not png.read_bytes()[:8].startswith(b"\x89PNG\r\n\x1a\n"):
                png.unlink(missing_ok=True)
                skip(link, f"render of {node.get('name')!r} was not a PNG")
                continue
            data_path = dest / f"{stem}.json"
            _write(data_path, json.dumps(condensed, indent=1))
            summary_path = dest / f"{stem}.summary.md"
            _write(
                summary_path,
                summarise(
                    condensed,
                    url=link.url,
                    file_name=str(info.get("name", "")),
                    version=version,
                    modified=str(info.get("lastModified", "")),
                ),
            )
            for p in (png, data_path, summary_path):
                p.chmod(0o400)
            changed = bool(
                node["id"] in current_frames
                and _fingerprint(current_frames[node["id"]]) != _fingerprint(condensed)
            )
            box = condensed.get("box") or [0, 0, 0, 0]
            ref = DesignRef(
                url=link.url,
                file_key=file_key,
                file_name=str(info.get("name", "")),
                node_id=node["id"],
                frame_name=str(node.get("name", "")),
                version=version,
                last_modified=str(info.get("lastModified", "")),
                image_path=str(png),
                summary_path=str(summary_path),
                data_path=str(data_path),
                image_sha256=hashlib.sha256(png.read_bytes()).hexdigest(),
                width=int(box[2]),
                height=int(box[3]),
                changed_in_figma_since=changed,
            )
            out.refs.append(ref)
            if changed:
                out.changed.append(ref)
    return out
