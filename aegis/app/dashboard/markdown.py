"""Minimal, safe Markdown → HTML for reports (headings, lists, emphasis, citations, links).
Input is HTML-escaped *before* any markup is added, so report text can never inject HTML."""
from __future__ import annotations

import re
from html import escape

from markupsafe import Markup

_LINK = re.compile(r"&lt;(https?://[^\s&]+(?:&amp;[^\s&]+)*)&gt;")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITAL = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")


def _inline(text: str) -> str:
    t = escape(text, quote=True)
    t = _LINK.sub(lambda m: f'<a href="{m.group(1)}" rel="noopener noreferrer nofollow" target="_blank">'
                            f'{m.group(1)}</a>', t)
    t = _BOLD.sub(r"<b>\1</b>", t)
    t = _ITAL.sub(r"<i>\1</i>", t)
    return t.replace("  ", "&nbsp; ")


def render(md: str | None) -> Markup:
    if not md:
        return Markup("")
    out, list_tag = [], None
    for raw in md.splitlines():
        line = raw.rstrip()
        m_ul = re.match(r"^\s*- (.*)$", line)
        m_ol = re.match(r"^\s*(\d+)\. (.*)$", line)
        tag = "ul" if m_ul else "ol" if m_ol else None
        if list_tag and tag != list_tag and not (line.startswith("  ") and list_tag):
            out.append(f"</{list_tag}>")
            list_tag = None
        if m_ul or m_ol:
            if not list_tag:
                out.append(f"<{tag}>")
                list_tag = tag
            out.append(f"<li>{_inline((m_ul or m_ol).groups()[-1])}</li>")
        elif line.startswith("  ") and list_tag and out and out[-1].endswith("</li>"):
            out[-1] = out[-1][:-5] + "<br>" + _inline(line.strip()) + "</li>"
        elif line.startswith("#"):
            level = min(4, len(line) - len(line.lstrip("#")) + 1)
            out.append(f"<h{level}>{_inline(line.lstrip('#').strip())}</h{level}>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    if list_tag:
        out.append(f"</{list_tag}>")
    return Markup("\n".join(out))
