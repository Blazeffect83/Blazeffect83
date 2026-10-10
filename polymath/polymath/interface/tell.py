""" "Tell me about X": a short paragraph written from the facts it knows, with sources.

There is no language model. Sentences come from templates chosen per relation (``P569`` date of birth, ``P36``
capital, ``P106`` occupation …), in a fixed order that reads like an encyclopedia opening:

1. **what it is**: the Wikidata description with the right article ("Paris is *the* capital and largest city of
   France", "Marie Curie was *a* Polish-French physicist and chemist"), else what it is an instance of;
2. **life** for people: born where and when, died where and when, combined into one sentence;
3. **the facts that matter most** for that kind of thing: capital, language, currency, population and borders for
   places; occupation, field, education, awards and notable works for people; founder, founding date and
   headquarters for organisations; author and publication date for works;
4. **what points to it** ("It is the capital of France", "People born here include …");
5. a few remaining facts in a plain form.

Tense follows the facts: a person with a date of death "was", a living one "is". People are referred to by surname
and never by pronoun, because a guessed pronoun can be wrong. Inferred facts say "(worked out by reasoning)",
disputed ones say that sources disagree, and every sentence carries numbered citations. When an article was read,
its opening follows as a quoted second paragraph.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from polymath.core.db import Database
from polymath.interface.answer import Answerer, Citation, render_value
from polymath.memory.graph import Entity

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]  # fmt: skip
STUB = re.compile(r"[QP]\d+")
HUMAN = "Q5"
COMMON_HEADS = {"type", "kind", "unit", "form", "member", "part", "group", "family", "species", "genus", "branch",
                "process", "study", "science", "field", "theory", "concept", "method", "system", "state"}  # fmt: skip
THE_HEADS = {"capital", "seat", "head", "founder", "birthplace", "home", "centre", "center", "flagship", "heart",
             "headquarters", "patron", "symbol", "national", "official"}  # fmt: skip
SUPERLATIVE = {"largest", "biggest", "highest", "longest", "oldest", "tallest", "smallest", "deepest", "most",
               "best", "greatest", "earliest", "fastest", "brightest", "nearest", "closest", "coldest", "hottest",
               "first", "last", "only", "main", "second", "third", "youngest", "heaviest", "lightest"}  # fmt: skip
MAX_LIST = 3
MAX_PLAIN = 3


@dataclass
class Fact:
    triple: int
    key: str  # predicate key, e.g. P36
    label: str  # predicate label
    value: str  # rendered object
    status: str
    confidence: float
    obj: int
    raw: str

    def raw_date(self) -> str:
        """The stored date (YYYY[-MM[-DD]]) behind a rendered time value."""
        try:
            v = json.loads(self.raw)
        except ValueError:
            return self.value
        return str(v.get("time", self.value)) if isinstance(v, dict) else self.value


@dataclass
class Story:
    subject: str
    paragraph: str
    article: str = ""  # the opening of the article it read about the subject, if any
    citations: list[Citation] = field(default_factory=list)
    facts_used: int = 0
    inferred: int = 0
    disputed: int = 0
    confidence: float = 0.0

    def render(self) -> str:
        if not self.paragraph:
            return f"I don't know enough about {self.subject} yet."
        out = [self.paragraph]
        if self.article:
            out += ["", f"From the article: “{self.article}”"]
        if self.citations:
            out += ["", "Sources:"] + [
                f"[{i + 1}] {c.title} — {c.url or c.source} — {c.license}" for i, c in enumerate(self.citations)
            ]
        return "\n".join(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "paragraph": self.paragraph,
            "article": self.article,
            "facts_used": self.facts_used,
            "inferred": self.inferred,
            "disputed": self.disputed,
            "confidence": round(self.confidence, 3),
            "citations": [c.__dict__ for c in self.citations],
        }


# ------------------------------------------------------------------ wording helpers
def join(items: list[str], more: int = 0) -> str:
    items = [i for i in items if i]
    if more > 0:
        return ", ".join(items) + f" and {more} more"
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def article(phrase: str) -> str:
    """'a', 'an' or 'the' for a Wikidata-style description or class name."""
    p = phrase.strip()
    first = p.split(" ", 1)[0].lower().strip("\"'(")
    if first in {"the", "a", "an"}:
        return ""
    if first in SUPERLATIVE or (first in THE_HEADS and " of " in f" {p} "):
        return "the"
    return "an" if first[:1] in "aeiou" and not first.startswith(("uni", "use", "eu", "one")) else "a"


def with_article(phrase: str) -> str:
    a = article(phrase)
    return f"{a} {phrase}" if a else phrase


def clean_description(text: str) -> str:
    text = re.sub(r"\s*\([^)]*\d{3,4}[^)]*\)\s*$", "", text.strip())  # "(1867–1934)": the dates come from facts
    first = text.split(" ", 1)[0]
    if first[:1].isupper() and first[1:].islower() and first.lower() in SUPERLATIVE | THE_HEADS | COMMON_HEADS:
        text = first.lower() + text[len(first) :]  # "Smallest unit of …" → "smallest unit of …"
    return text.rstrip(". ")


def format_date(value: str) -> tuple[str, str]:
    """('on'|'in', '7 November 1867') from a stored date (YYYY, YYYY-MM or YYYY-MM-DD; negative years are BC)."""
    m = re.fullmatch(r"(-?)(\d{1,6})(?:-(\d\d))?(?:-(\d\d))?", value.strip())
    if not m:
        return "in", value
    neg, year, month, day = m.groups()
    y = f"{int(year)} BC" if neg else str(int(year))
    if month and day and int(month) and int(day):
        return "on", f"{int(day)} {MONTHS[int(month) - 1]} {y}"
    if month and int(month):
        return "in", f"{MONTHS[int(month) - 1]} {y}"
    return "in", y


def round_number(value: str) -> str:
    """'67,750,000' → 'about 67.8 million' (other values unchanged)."""
    m = re.fullmatch(r"([\d,]+(?:\.\d+)?)(.*)", value.strip())
    if not m:
        return value
    n = float(m.group(1).replace(",", ""))
    rest = m.group(2)
    for size, word in ((1e9, "billion"), (1e6, "million")):
        if n >= size:
            return f"about {n / size:.1f}".rstrip("0").rstrip(".") + f" {word}{rest}"
    return value


# ------------------------------------------------------------------ the teller
class Teller:
    def __init__(self, db: Database, answerer: Answerer | None = None) -> None:
        self.db = db
        self.answerer = answerer or Answerer(db)

    def tell(self, phrase: str) -> Story:
        entity = self.answerer.find_entity(phrase.strip())
        if entity is None:
            return Story(subject=phrase.strip(), paragraph="")
        return self.tell_entity(entity)

    # ---------------------------------------------------------------- facts
    def facts_of(self, entity: Entity) -> list[Fact]:
        out = []
        for r in self.db.query(
            "SELECT t.id, t.o, t.value, t.status, t.confidence, p.key, p.label FROM triples t "
            "JOIN predicates p ON p.id = t.p WHERE t.s = ? AND t.holdout = 0 "
            "ORDER BY t.status = 'disputed', t.confidence DESC, t.id LIMIT 400",
            (entity.id,),
        ):
            value = render_value(self.db, int(r["o"]), str(r["value"]))
            if "not read yet" in value:
                continue
            out.append(Fact(int(r["id"]), str(r["key"]), str(r["label"]), value, str(r["status"]),
                            float(r["confidence"]), int(r["o"]), str(r["value"])))  # fmt: skip
        return out

    def pointing_at(self, entity: Entity, key: str, limit: int = MAX_LIST) -> list[tuple[int, str]]:
        """(triple id, subject label) of facts ``? —key→ entity``, most important subjects first."""
        return [
            (int(r["id"]), str(r["label"]))
            for r in self.db.query(
                "SELECT t.id, e.label FROM triples t JOIN predicates p ON p.id = t.p JOIN entities e ON e.id = t.s "
                "WHERE t.o = ? AND p.key = ? AND t.holdout = 0 AND t.status != 'disputed' "
                "ORDER BY e.pagerank DESC, t.confidence DESC LIMIT ?",
                (entity.id, key, limit + 1),
            )
            if not STUB.fullmatch(str(r["label"]))
        ]

    # ---------------------------------------------------------------- writing
    def tell_entity(self, entity: Entity) -> Story:
        facts = self.facts_of(entity)
        by: dict[str, list[Fact]] = {}
        for f in facts:
            by.setdefault(f.key, []).append(f)
        used: set[int] = set()
        cites: list[Citation] = []
        sentences: list[str] = []
        classes = {  # read directly: the class itself (Q5 "human") may not have been read yet
            str(r["key"])
            for r in self.db.query(
                "SELECT c.key FROM triples t JOIN predicates p ON p.id = t.p JOIN entities c ON c.id = t.o "
                "WHERE t.s = ? AND p.key = 'P31' AND t.holdout = 0",
                (entity.id,),
            )
        }
        human = HUMAN in classes or bool(by.get("P569") and (by.get("P19") or by.get("P106")))
        past = bool(by.get("P570") or by.get("P576"))
        be = "was" if past else "is"
        words = entity.label.split()
        short = words[-1] if human and len(words) > 1 and "of" not in words else entity.label
        name = short if human else "It"

        def mark(fs: list[Fact]) -> str:
            nums = []
            for f in fs:
                used.add(f.triple)
                for c in self.answerer._citations(f.triple):
                    if c not in cites:
                        cites.append(c)
                    n = str(cites.index(c) + 1)
                    if n not in nums:
                        nums.append(n)
            return f" [{', '.join(nums)}]" if nums else ""

        def note(fs: list[Fact]) -> str:
            if any(f.status == "disputed" for f in fs):
                return " (sources disagree)"
            if fs and all(f.status == "inferred" for f in fs):
                return " (worked out by reasoning)"
            return ""

        def say(text: str, fs: list[Fact]) -> None:
            sentences.append(text.rstrip(".") + note(fs) + "." + mark(fs))

        def values(key: str, limit: int = MAX_LIST) -> tuple[list[Fact], str]:
            fs = [f for f in by.get(key, []) if f.triple not in used and f.value != entity.label]
            if any(f.status == "sourced" for f in fs):  # what it read beats what it worked out
                fs = [f for f in fs if f.status == "sourced"]
            if not fs:
                return [], ""
            shown = fs[:limit]
            more = len(fs) - len(shown) if limit > 1 else 0
            return shown, join([f.value for f in shown], more)

        # 1. what it is
        desc = clean_description(entity.description or "")
        kinds, kind_text = values("P31", 2)
        if desc:
            say(f"{entity.label} {be} {with_article(desc)}", [])
        elif kind_text:
            say(f"{entity.label} {be} {with_article(kind_text)}", kinds)
        # 2. life
        if human:
            born = self._life(by, ("P569", "infobox:birth_date"), ("P19", "infobox:birth_place"), "born")
            died = self._life(by, ("P570", "infobox:death_date"), ("P20", "infobox:death_place"), "died")
            if born[0] or died[0]:
                text = f"{short} was " + " and ".join(x for x in (born[0], died[0]) if x)
                say(text, born[1] + died[1])
        # 3. what matters for this kind of thing
        for key, template in SENTENCES:
            if len(sentences) >= MAX_SENTENCES - 1:
                break
            fs, text = values(key, MAX_LIST if key not in SINGLE else 1)
            if not fs:
                continue
            if key == "P1082":
                text = round_number(text)
            if key in DATES:
                prep, text = format_date(fs[0].raw_date())
            else:
                prep = ""
            say(template(name, be, text, prep, len(fs) > 1, human), fs)
        # 4. what points to it
        capital_of = self.pointing_at(entity, "P36", 1)
        if capital_of and not any(f.key == "P1376" for f in facts):
            sentences.append(f"{name} {be} the capital of {capital_of[0][1]}." + self._mark_ids(capital_of, cites))
        born_here = self.pointing_at(entity, "P19")
        if born_here and not human:
            people = [label for _t, label in born_here[:MAX_LIST]]
            sentences.append(f"People born here include {join(people)}." + self._mark_ids(born_here[:MAX_LIST], cites))
        # 5. a few more, plainly (several values of one relation in one sentence)
        templated = {k for k, _t in SENTENCES}
        groups: dict[str, list[Fact]] = {}
        for f in facts:
            if f.triple in used or f.value == entity.label or not plain_ok(f) or f.key in templated:
                continue
            groups.setdefault(f.label, []).append(f)
        for label, fs in list(groups.items())[:MAX_PLAIN]:
            if len(sentences) >= MAX_SENTENCES:
                break
            shown = fs[:MAX_LIST]
            if fs[0].key in DATES or re.fullmatch(r"-?\d{1,6}(-\d\d){0,2}", fs[0].value):
                text = format_date(fs[0].value)[1]
                shown = fs[:1]
            else:
                text = join([f.value for f in shown], len(fs) - len(shown))
            many = len(shown) > 1
            verb = ("were" if many else "was") if past else ("are" if many else "is")
            say(f"Its {label}{'s' if many and not label.endswith('s') else ''} {verb} {text}", shown)
        paragraph = " ".join(sentences)
        if human:
            paragraph = paragraph.replace("Its ", f"{short}'s ")
        story = Story(
            subject=entity.label,
            paragraph=paragraph,
            citations=cites,
            facts_used=len(used),
            inferred=sum(1 for f in facts if f.triple in used and f.status == "inferred"),
            disputed=sum(1 for f in facts if f.triple in used and f.status == "disputed"),
        )
        conf = [f.confidence for f in facts if f.triple in used]
        story.confidence = sum(conf) / len(conf) if conf else (0.6 if desc else 0.0)
        story.article = self._article(entity, cites)
        return story

    def _mark_ids(self, rows: list[tuple[int, str]], cites: list[Citation]) -> str:
        nums: list[str] = []
        for tid, _label in rows:
            for c in self.answerer._citations(tid):
                if c not in cites:
                    cites.append(c)
                n = str(cites.index(c) + 1)
                if n not in nums:
                    nums.append(n)
        return f" [{', '.join(nums)}]" if nums else ""

    @staticmethod
    def _life(
        by: dict[str, list[Fact]], date_keys: tuple[str, ...], place_keys: tuple[str, ...], verb: str
    ) -> tuple[str, list[Fact]]:
        date = next((by[k][:1] for k in date_keys if by.get(k)), [])
        place = next((by[k][:1] for k in place_keys if by.get(k)), [])
        if date and not re.fullmatch(r"-?\d{1,6}(-\d\d){0,2}", date[0].raw_date()):
            date = []  # an infobox date in free text ("{{birth date|1471|5|21}}") is not worth repeating
        if not date and not place:
            return "", []
        text = verb
        if place:
            text += f" in {place[0].value}"
        if date:
            prep, when = format_date(date[0].raw_date())
            text += f" {prep} {when}"
        return text, date + place

    def _article(self, entity: Entity, cites: list[Citation]) -> str:
        if not entity.doc_id:
            return ""
        doc = self.answerer.store.get(entity.doc_id)
        if doc is None or not doc.text:
            return ""
        sentences = re.split(r"(?<=[.!?])\s+", doc.text.strip()[:1200])
        text = ""
        for sent in sentences:  # the article's own opening, up to two sentences
            if len(text) + len(sent) > 420 and text:
                break
            text = f"{text} {sent}".strip()
            if text.count(". ") >= 1:
                break
        c = Citation(doc.title, doc.url, doc.license, doc.source)
        if c not in cites:
            cites.append(c)
        return f"{text} [{cites.index(c) + 1}]" if text else ""


Template = Callable[[str, str, str, str, bool, bool], str]


def _t(text: str) -> Template:
    """A sentence template: {s} subject, {be} is/was, {v} value(s), {prep} on/in for dates, {pl} plural verb."""

    def render(s: str, be: str, v: str, prep: str, plural: bool, human: bool) -> str:
        are = ("were" if be == "was" else "are") if plural else be
        return text.format(s=s, be=be, v=v, prep=prep, are=are, ies="ies" if plural else "y",
                           s_="s" if plural else "", has="have" if plural else "has")  # fmt: skip

    return render


# Order matters: it is the order sentences appear in. Keys are Wikidata property ids.
SENTENCES: list[tuple[str, Template]] = [
    ("P106", lambda s, be, v, prep, pl, h: f"{s} worked as {with_article(v)}"),
    ("P101", _t("{s} worked in {v}")),
    ("P69", _t("{s} studied at {v}")),
    ("P166", _t("Awards include {v}")),
    ("P800", _t("Notable works include {v}")),
    ("P26", lambda s, be, v, prep, pl, h: f"{s} {'was' if be == 'was' else 'is'} married to {v}"),
    ("P1412", _t("{s} spoke {v}")),
    ("P1376", _t("{s} {be} the capital of {v}")),
    ("P36", _t("Its capital {be} {v}")),
    ("P37", _t("Its official language{s_} {are} {v}")),
    ("P38", _t("Its currenc{ies} {are} {v}")),
    ("P1082", _t("It has a population of {v}")),
    ("P2046", _t("It covers {v}")),
    ("P30", _t("It lies in {v}")),
    ("P17", _t("It is in {v}")),
    ("P131", _t("It is located in {v}")),
    ("P47", _t("It borders {v}")),
    ("P35", _t("Its head of state {be} {v}")),
    ("P6", _t("Its head of government {be} {v}")),
    ("P112", _t("It was founded by {v}")),
    ("P571", _t("It was founded {prep} {v}")),
    ("P159", _t("Its headquarters {are} in {v}")),
    ("P50", _t("It was written by {v}")),
    ("P577", _t("It was published {prep} {v}")),
    ("P279", _t("It is a kind of {v}")),
    ("P361", _t("It is part of {v}")),
    ("P463", _t("It is a member of {v}")),
    ("P150", _t("It includes {v}")),
    ("P206", _t("It lies on {v}")),
    ("P122", _t("Its form of government {be} {v}")),
    ("P1549", _t("Its people are called {v}")),
    ("P1451", lambda s, be, v, prep, pl, h: f"Its motto is “{v}”"),
    ("P610", _t("Its highest point is {v}")),
    ("P1589", _t("Its lowest point is {v}")),
    ("P2250", _t("Life expectancy there is {v}")),
    ("P1081", _t("Its Human Development Index is {v}")),
    ("P140", _t("Its religion{s_} {are} {v}")),
    ("P108", _t("{s} worked for {v}")),
    ("P39", _t("{s} held the position of {v}")),
    ("P102", _t("{s} was a member of {v}")),
    ("P737", _t("{s} was influenced by {v}")),
    ("P61", _t("It was discovered by {v}")),
    ("P575", _t("It was discovered {prep} {v}")),
    ("P170", _t("It was created by {v}")),
    ("P86", _t("Its music is by {v}")),
    ("P57", _t("It was directed by {v}")),
    ("P136", _t("Its genre{s_} {are} {v}")),
    ("P397", _t("It orbits {v}")),
    ("P171", _t("It belongs to {v}")),
    ("P141", _t("Its conservation status is {v}")),
]
MAX_SENTENCES = 9
SINGLE = {"P1082", "P2046", "P571", "P577", "P35", "P6", "P30", "P1451", "P1549", "P610", "P1589", "P2250",
          "P1081", "P122", "P575", "P397", "P141"}  # fmt: skip
DATES = {"P571", "P577", "P569", "P570", "P575"}
# relations worth a plain sentence when nothing better was said (identifiers and codes are not)
PLAIN = {"P1549", "P1451", "P122", "P85", "P610", "P1589", "P2131", "P1081", "P2250", "P140", "P463", "P1056",
         "P452", "P1128", "P2139", "P1448", "P206", "P1435", "P84", "P136", "P495", "P57", "P161", "P175", "P86",
         "P264", "P123", "P407", "P921", "P170", "P186", "P2067", "P2386", "P2583", "P397", "P61", "P575", "P1086",
         "P246", "P171", "P105", "P141", "P1843", "P2120", "P2044", "P150", "P39", "P108", "P102", "P641",
         "P54", "P413", "P118", "P1344", "P737", "P802", "P1066", "P184", "P185", "P512", "P1303", "P412"}  # fmt: skip
JUNK = re.compile(r"\b(code|id|identifier|hashtag|unicode|commons|coordinates?|gallery|category|image|logo|flag|"
                  r"plate|url|website|map|locator|signature|icon|seal|caption|footnote|ref|alt|size|width|"
                  r"common name|name format|out-of-school|diplomatic|symbol|type|style|color|colour|"
                  r"position|label|mapsize|pushpin|module|blank|footnotes?|other names|birth|death|"
                  r"\w*style)\b", re.I)  # fmt: skip


def plain_ok(f: Fact) -> bool:
    if f.key in PLAIN:
        return True
    if f.key.startswith("infobox:"):
        return not JUNK.search(f.label) and len(f.value) <= 80 and not re.fullmatch(r"[\d\W]+", f.value)
    return False


SKIP = {"P569", "P570", "P19", "P20", "P21", "P31", "P18", "P625", "P373", "P910", "P1343", "P646", "P227",
        "P214", "P244", "P213", "P268", "P269", "P1412", "P735", "P734", "P1559", "P2048"}  # fmt: skip
