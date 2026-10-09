"""Own MediaWiki wikitext → plain text cleaner.

Produces clean prose plus the structure later stages learn from:

* ``links`` — (start, end, target) spans of wiki links in the clean text. These
  are the agent's free, self-collected supervision for entity linking
  (anchor → entity priors) and evaluation.
* ``templates`` — top-level templates with parsed parameters (infoboxes).
* ``categories`` and ``sections``.

The parser is a bracket-matching scanner (no regex catastrophes on hostile
input) with hard limits on nesting.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

MAX_DEPTH = 40

_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.S)
_REF_SELF = re.compile(r"<ref\b[^>]*/>", re.I)
_REF = re.compile(r"<ref\b[^>]*>.*?(?:</ref\s*>|$)", re.I | re.S)
_DROP_TAGS = re.compile(
    r"<(gallery|math|chem|score|timeline|graph|syntaxhighlight|source|pre|code|templatedata|imagemap|mapframe|"
    r"maplink|references|poem|hiero|inputbox|categorytree|templatestyles)\b[^>]*?(?:/>|>.*?(?:</\1\s*>|$))",
    re.I | re.S,
)
_NOWIKI = re.compile(r"<nowiki\s*>(.*?)</nowiki\s*>", re.I | re.S)
_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*\b[^<>]*>")
_BEHAVIOR = re.compile(r"__[A-Z]+__")
_HEADING = re.compile(r"^(={1,6})\s*(.*?)\s*\1\s*$")
_EXTLINK = re.compile(r"\[(?:https?:)?//[^\s\]]+(?:\s+([^\]]*))?\]")
_BARE_URL = re.compile(r"\bhttps?://[^\s<>\[\]]+")
_QUOTES = re.compile(r"'{2,5}")
_SPACES = re.compile(r"[ \t ]+")
_LINK_TRAIL = re.compile(r"[a-z]+")

NON_ARTICLE_PREFIXES = (
    "file:",
    "image:",
    "media:",
    "category:",
    "wikipedia:",
    "wp:",
    "help:",
    "template:",
    "portal:",
    "draft:",
    "user:",
    "special:",
    "talk:",
    "module:",
    "mediawiki:",
    "book:",
    "timedtext:",
    "wikt:",
    "wiktionary:",
    "s:",
    "q:",
    "n:",
    "b:",
    "v:",
    "voy:",
    "commons:",
    "meta:",
    "species:",
    "d:",
    "mw:",
)
_INTERLANG = re.compile(r"^[a-z]{2,3}(?:-[a-z]+)?:", re.I)

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


@dataclass
class Template:
    name: str
    params: dict[str, str]
    positional: list[str]


@dataclass
class CleanPage:
    text: str
    links: list[tuple[int, int, str]] = field(default_factory=list)
    templates: list[Template] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    sections: list[tuple[int, str]] = field(default_factory=list)  # (offset, heading)


def normalize_title(title: str) -> str:
    """MediaWiki title normalisation: underscores → spaces, first letter upper-cased."""
    t = html.unescape(title).replace("_", " ").strip()
    t = _SPACES.sub(" ", t)
    if "#" in t:
        t = t.split("#", 1)[0].strip()
    return t[:1].upper() + t[1:] if t else t


# --------------------------------------------------------------------- scanner


def _find_close(text: str, start: int, open_s: str, close_s: str) -> int:
    """Index just past the matching ``close_s`` for the ``open_s`` at ``start``; -1 if unbalanced."""
    depth = 0
    i = start
    n = len(text)
    lo, lc = len(open_s), len(close_s)
    while i < n:
        if text.startswith(open_s, i):
            depth += 1
            if depth > MAX_DEPTH:
                return -1
            i += lo
        elif text.startswith(close_s, i):
            depth -= 1
            i += lc
            if depth == 0:
                return i
        else:
            i += 1
    return -1


def _split_params(body: str) -> list[str]:
    """Split template/link body on top-level ``|`` (ignoring nested {{ }} and [[ ]])."""
    parts: list[str] = []
    depth_t = depth_l = 0
    cur: list[str] = []
    i = 0
    while i < len(body):
        two = body[i : i + 2]
        if two == "{{":
            depth_t += 1
            cur.append(two)
            i += 2
            continue
        if two == "}}" and depth_t:
            depth_t -= 1
            cur.append(two)
            i += 2
            continue
        if two == "[[":
            depth_l += 1
            cur.append(two)
            i += 2
            continue
        if two == "]]" and depth_l:
            depth_l -= 1
            cur.append(two)
            i += 2
            continue
        ch = body[i]
        if ch == "|" and depth_t == 0 and depth_l == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def parse_template(body: str) -> Template:
    parts = _split_params(body)
    name = parts[0].strip()
    if ":" in name and name.lower().startswith("template:"):
        name = name.split(":", 1)[1]
    params: dict[str, str] = {}
    positional: list[str] = []
    for part in parts[1:]:
        key, eq, value = part.partition("=")
        if eq and "{{" not in key and "[[" not in key and key.strip():
            params[key.strip().lower()] = value.strip()
        else:
            positional.append(part.strip())
    return Template(name=_SPACES.sub(" ", name.replace("_", " ")).strip(), params=params, positional=positional)


def _date_from_params(pos: list[str]) -> str:
    nums = [p for p in pos if p.strip().isdigit()][:3]
    if len(nums) == 3:
        y, m, d = (int(x) for x in nums)
        if 1 <= m <= 12:
            return f"{d} {MONTHS[m - 1]} {y}"
    if len(nums) == 2:
        y, m = (int(x) for x in nums)
        if 1 <= m <= 12:
            return f"{MONTHS[m - 1]} {y}"
    return nums[0] if nums else ""


def render_inline_template(t: Template) -> str:
    """Text for templates that carry prose; everything else renders as nothing."""
    name = t.name.lower()
    pos = [p for p in t.positional if p]
    if name in {"convert", "cvt"} and pos:
        unit = pos[1] if len(pos) > 1 else ""
        if unit in {"-", "to", "and", "or", "–"} and len(pos) > 3:
            return f"{pos[0]}–{pos[2]} {pos[3]}"
        return f"{pos[0]} {unit}".strip()
    if name in {
        "nowrap",
        "nobr",
        "small",
        "smaller",
        "big",
        "larger",
        "lang-rtl",
        "nobold",
        "noitalic",
        "mvar",
        "math",
        "abbr",
        "sic",
        "nihongo",
        "nihongo2",
        "ill",
        "interlanguage link",
        "flag",
        "flagcountry",
        "flagu",
        "keypress",
        "tooltip",
        "visible anchor",
        "vanchor",
        "anchor",
        "linktext",
        "lang-x",
        "ubl",
        "unbulleted list",
        "plainlist",
        "hlist",
        "flatlist",
        "bulleted list",
    }:
        if name in {"ubl", "unbulleted list", "hlist", "flatlist", "bulleted list", "plainlist"}:
            return ", ".join(pos)
        return pos[0] if pos else ""
    if name == "lang" and len(pos) >= 2:
        return pos[1]
    if name.startswith("lang-") and pos:
        return pos[0]
    if name in {"transl", "transliteration"} and pos:
        return pos[-1]
    if name in {"circa", "c."}:
        return f"c. {pos[0]}" if pos else "c."
    if name in {"as of"} and pos:
        return f"As of {_date_from_params(pos) or pos[0]}"
    if name in {
        "birth date",
        "death date",
        "start date",
        "end date",
        "birth date and age",
        "death date and age",
        "dob",
        "film date",
        "start date and age",
        "birth-date",
        "date",
    }:
        return _date_from_params(pos)
    if name in {"frac", "sfrac"} and pos:
        return "/".join(pos[-2:]) if len(pos) >= 2 else f"1/{pos[0]}"
    if name == "val" and pos:
        unit = t.params.get("u", t.params.get("ul", ""))
        return f"{pos[0]} {unit}".strip()
    if name in {"snd", "spaced ndash", "spaced en dash", "ndash", "–"}:
        return " – "
    if name in {"mdash", "spaced mdash"}:
        return " — "
    if name in {"'", "' \""}:
        return "'"
    if name in {"\"'"}:
        return '"'
    if name in {"·", "dot", "middot", "dot-sep"}:
        return " · "
    if name in {"=", "equals"}:
        return "="
    if name == "!":
        return "|"
    if name in {"au", "aud"}:
        return "AU"
    if name in {"us$", "usd"} and pos:
        return f"US${pos[0]}"
    if name in {"inflation", "formatprice", "format price"} and len(pos) >= 2:
        return pos[1]
    if name in {"citation needed", "cn", "fact", "clarify", "when", "who", "which", "dubious", "verify source"}:
        return ""
    if name in {"quote", "blockquote", "cquote", "quotation"} and pos:
        return pos[0]
    return ""


# ------------------------------------------------------------------ main pass


def _strip_templates(text: str, out_templates: list[Template], depth: int = 0) -> str:
    res: list[str] = []
    i = 0
    while True:
        j = text.find("{{", i)
        if j < 0:
            res.append(text[i:])
            break
        res.append(text[i:j])
        placeholder = text.startswith("{{{", j)  # template parameter placeholder: dropped
        end = _find_close(text, j, "{{{", "}}}") if placeholder else _find_close(text, j, "{{", "}}")
        if end < 0:
            res.append(text[j + 2 :].replace("{{", " ").replace("}}", " ") if depth == 0 else "")
            break
        body = text[j + 2 : end - 2]
        if not text.startswith("{{{", j) and not body.startswith("#"):
            tpl = parse_template(body)
            if depth == 0:
                out_templates.append(tpl)
            rendered = render_inline_template(tpl)
            if rendered:
                res.append(_strip_templates(rendered, out_templates, depth + 1) if "{{" in rendered else rendered)
        i = end
    return "".join(res)


def _strip_tables(text: str) -> str:
    res: list[str] = []
    i = 0
    while True:
        j = text.find("{|", i)
        if j < 0:
            res.append(text[i:])
            break
        res.append(text[i:j])
        end = _find_close(text, j, "{|", "|}")
        if end < 0:
            break
        i = end
        res.append("\n")
    return "".join(res)


def _strip_file_links(text: str) -> tuple[str, list[str]]:
    """Remove [[File:..]]/[[Category:..]]/interlanguage links (nested-safe); collect categories."""
    cats: list[str] = []
    res: list[str] = []
    i = 0
    while True:
        j = text.find("[[", i)
        if j < 0:
            res.append(text[i:])
            break
        res.append(text[i:j])
        head = text[j + 2 : j + 40].lstrip(" :").lower()
        if head.startswith(NON_ARTICLE_PREFIXES[:5]) or (
            _INTERLANG.match(head) and not head.startswith(NON_ARTICLE_PREFIXES[5:])
        ):
            end = _find_close(text, j, "[[", "]]")
            if end < 0:
                res.append(text[j:])
                break
            inner = text[j + 2 : end - 2]
            if head.startswith("category:"):
                cats.append(normalize_title(inner.split(":", 1)[1].split("|", 1)[0]))
            i = end
            continue
        res.append("[[")
        i = j + 2
    return "".join(res), cats


def _render_line(line: str, out: list[str], links: list[tuple[int, int, str]], pos: int) -> int:
    """Render one line of inline markup (links, external links, quotes) appending to ``out``."""
    i = 0
    n = len(line)
    while i < n:
        j = line.find("[[", i)
        if j < 0:
            seg = _inline(line[i:])
            out.append(seg)
            return pos + len(seg)
        seg = _inline(line[i:j])
        out.append(seg)
        pos += len(seg)
        end = line.find("]]", j + 2)
        if end < 0:
            rest = _inline(line[j + 2 :])
            out.append(rest)
            return pos + len(rest)
        inner = line[j + 2 : end]
        target, _, anchor = inner.partition("|")
        target = target.strip()
        if not anchor:
            anchor = target.lstrip(":")
        anchor = _inline(anchor)
        i = end + 2
        trail = _LINK_TRAIL.match(line, i)
        if trail:
            anchor += trail.group(0)
            i = trail.end()
        lower = target.lower().lstrip(":")
        if anchor:
            if target and not lower.startswith(NON_ARTICLE_PREFIXES) and "{" not in target:
                links.append((pos, pos + len(anchor), normalize_title(target.lstrip(":"))))
            out.append(anchor)
            pos += len(anchor)
    return pos


def _inline(s: str) -> str:
    s = _EXTLINK.sub(lambda m: m.group(1) or "", s)
    s = _BARE_URL.sub("", s)
    s = _QUOTES.sub("", s)
    return s


def clean_wikitext(raw: str) -> CleanPage:
    templates: list[Template] = []
    text = _COMMENT.sub("", raw)
    text = _NOWIKI.sub(lambda m: html.escape(m.group(1)), text)
    text = _REF_SELF.sub("", text)
    text = _REF.sub("", text)
    text = _DROP_TAGS.sub("", text)
    text = _strip_templates(text, templates)
    text = _strip_tables(text)
    text, categories = _strip_file_links(text)
    text = _BEHAVIOR.sub("", text)
    text = _TAG.sub("", text)

    out: list[str] = []
    links: list[tuple[int, int, str]] = []
    sections: list[tuple[int, str]] = []
    pos = 0
    blank = True
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith(("----", "|", "!")):
            if not blank and out:
                out.append("\n\n")
                pos += 2
                blank = True
            continue
        m = _HEADING.match(line)
        if m:
            heading = _QUOTES.sub("", m.group(2)).strip()
            heading = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", heading)
            if not blank and out:
                out.append("\n\n")
                pos += 2
            sections.append((pos, html.unescape(heading)))
            blank = True
            continue
        stripped = line.lstrip("*#:;").strip()
        if not stripped:
            continue
        if not blank:
            out.append(" " if not raw_line.startswith(("*", "#", ":", ";")) else "\n")
            pos += 1
        line_parts: list[str] = []
        line_links: list[tuple[int, int, str]] = []
        _render_line(stripped, line_parts, line_links, 0)
        rendered, mapped = _normalise_segment("".join(line_parts), line_links)
        if not rendered:
            continue
        out.append(rendered)
        links.extend((pos + a, pos + b, t) for a, b, t in mapped)
        pos += len(rendered)
        blank = False
    clean = "".join(out).rstrip()
    links = [(a, b, t) for a, b, t in links if b <= len(clean) and clean[a:b].strip()]
    return CleanPage(text=clean, links=links, templates=templates, categories=categories, sections=sections)


def _normalise_segment(s: str, links: list[tuple[int, int, str]]) -> tuple[str, list[tuple[int, int, str]]]:
    """Unescape entities and collapse whitespace while remapping link offsets."""
    # Character-level mapping keeps offsets exact through both transformations.
    chars: list[str] = []
    origin: list[int] = []
    i = 0
    while i < len(s):
        if s[i] == "&":
            semi = s.find(";", i + 1, i + 12)
            if semi > 0:
                ent = s[i : semi + 1]
                un = html.unescape(ent)
                if un != ent:
                    for ch in un:
                        chars.append(ch)
                        origin.append(i)
                    i = semi + 1
                    continue
        chars.append(s[i])
        origin.append(i)
        i += 1
    out: list[str] = []
    out_origin: list[int] = []
    prev_space = True
    for ch, o in zip(chars, origin):
        if ch in " \t  ​":
            if prev_space:
                continue
            ch = " "
            prev_space = True
        else:
            prev_space = False
        out.append(ch)
        out_origin.append(o)
    while out and out[-1] == " ":
        out.pop()
        out_origin.pop()
    text = "".join(out)
    mapped: list[tuple[int, int, str]] = []
    if links and out_origin:
        import bisect

        for a, b, t in links:
            na = bisect.bisect_left(out_origin, a)
            nb = bisect.bisect_left(out_origin, b)
            while na < nb and text[na] == " ":
                na += 1
            while nb > na and text[nb - 1] == " ":
                nb -= 1
            if nb > na:
                mapped.append((na, nb, t))
    return text, mapped


def redirect_target(raw: str) -> str | None:
    m = re.match(r"\s*#REDIRECT\s*:?\s*\[\[([^\]|#]+)", raw, re.I)
    return normalize_title(m.group(1)) if m else None
