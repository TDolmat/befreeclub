"""Port circle/tiptap.ts + textToTiptap z circle/client.ts.

text_to_tiptap: plain text -> pelny envelope rich_text_body Circle.
tiptap_to_plain_text: envelope/doc Tiptap -> plain text (chodzenie po drzewie,
bo circle_ios_fallback_text skleja akapity bez separatora).
"""

import re
from typing import Any


def text_to_tiptap(text: str) -> dict:
    trimmed = text.replace("\r\n", "\n").strip()
    fallback = trimmed

    paragraphs = re.split(r"\n{2,}", trimmed) if trimmed else []
    doc_content: list[Any] = [{"type": "paragraph"}] if len(paragraphs) == 0 else []
    for idx, para in enumerate(paragraphs):
        doc_content.append(_build_paragraph(para))
        if idx < len(paragraphs) - 1:
            doc_content.append({"type": "paragraph"})

    return {
        "body": {"type": "doc", "content": doc_content},
        "polls": [],
        "format": "chat",
        "entities": [],
        "attachments": [],
        "group_mentions": [],
        "community_members": [],
        "inline_attachments": [],
        "sgids_to_object_map": {},
        "circle_ios_fallback_text": fallback,
    }


def _build_paragraph(para: str) -> dict:
    lines = para.split("\n")
    content: list[Any] = []
    for idx, line in enumerate(lines):
        if len(line) > 0:
            content.append(
                {
                    "type": "text",
                    "text": line,
                    "circle_ios_fallback_text": line,
                }
            )
        if idx < len(lines) - 1:
            content.append({"type": "hardBreak"})
    if len(content) > 0:
        return {"type": "paragraph", "content": content}
    return {"type": "paragraph"}


def tiptap_to_plain_text(doc: object) -> str:
    if not isinstance(doc, dict):
        return ""

    body = doc.get("body")
    root = body if isinstance(body, dict) else doc

    return re.sub(r"\n{3,}", "\n\n", _visit(root)).strip()


def _visit(node: object) -> str:
    if not isinstance(node, dict):
        return ""
    raw_content = node.get("content")
    children = raw_content if isinstance(raw_content, list) else []
    node_type = node.get("type")

    if node_type == "text" and isinstance(node.get("text"), str):
        return node["text"]
    if node_type == "hardBreak":
        return "\n"

    if node_type in ("paragraph", "blockquote", "heading"):
        return "".join(_visit(c) for c in children) + "\n\n"

    if node_type == "bulletList":
        return "\n".join(f"- {_render_list_item(c)}" for c in children) + "\n\n"

    if node_type == "orderedList":
        attrs = node.get("attrs")
        start = attrs.get("start") if isinstance(attrs, dict) else None
        if start is None:
            start = 1
        return (
            "\n".join(f"{start + i}. {_render_list_item(c)}" for i, c in enumerate(children))
            + "\n\n"
        )

    if node_type == "listItem":
        return _render_list_item(node)

    return "".join(_visit(c) for c in children)


def _render_list_item(node: object) -> str:
    if not isinstance(node, dict):
        return ""
    raw_content = node.get("content")
    children = raw_content if isinstance(raw_content, list) else []
    return re.sub(r"\n+$", "", "".join(_visit(c) for c in children))
