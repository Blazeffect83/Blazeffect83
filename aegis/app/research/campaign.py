"""Deep research campaigns: give AEGIS a topic and a time budget; it researches
rigorously and autonomously until the budget is spent or the topic is exhausted.

Loop (each unit of work is checkpointed, so pause/crash/reboot resume exactly):

1. **Questions.** The topic is broken into sub-questions and search queries (by the
   planning model when available, otherwise deterministically from the topic,
   newly discovered key terms and open questions found in sources).
2. **Discovery.** Each query runs against Wikipedia (Wikimedia API), OpenAlex
   (scholarly abstracts) and, if configured, SearXNG. Results enter a frontier
   ranked by relevance, source type, depth and domain diversity.
3. **Ingestion.** The best candidate is fetched through the SSRF-safe pipeline:
   dedupe, injection screening, extraction, quality scoring, source-backed claims.
4. **Expansion.** Relevant outbound links of good documents join the frontier
   (bounded depth).
5. **Verification.** Important claims backed by a single source trigger targeted
   corroboration searches; contradictions are flagged by the knowledge store.
6. **Saturation.** When new documents stop yielding new claims, fresh questions
   are generated; persistent saturation ends the campaign ("diminishing returns").
7. **Report.** A cited Markdown report: answers per sub-question, key findings
   with confidence and corroboration, contested points, definitions, procedures,
   limitations, open questions/gaps, method statistics and the numbered sources.

Campaigns run in time slices (``slice_seconds``) and yield to the worker in
between, so other objectives keep running during a multi-hour campaign.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from urllib.parse import urlsplit

from ..db import dumps, loads, now_iso
from ..models.base import BudgetExceeded, ModelError
from ..security.network_policy import URLRejected, domain_matches
from . import discovery
from .deduplicator import _STOP, normalize, stem, stems, tokens
from .extractor import _FIRST_PERSON
from .injection import wrap_untrusted

log = logging.getLogger(__name__)

DEFAULTS = {
    "hours": 2.0,
    "max_documents": 200,
    "max_depth": 2,
    "max_per_domain": 15,
    "sources": ["wikipedia", "openalex", "searxng", "web", "links"],
    "stop_on_saturation": True,
    "slice_seconds": 300,
    "allowed_domains": [],
    "seed_urls": [],
}
_SKIP_URL = re.compile(
    r"(/login|/signin|/signup|/register|/cart|/checkout|/account|/privacy|/terms|/cookie|/share|/print|"
    r"/tag/|/tags/|/category/|/author/|/feed/?$|/search\?|[?&](action|oldid|diff|printable)=|"
    r"/wiki/(Special|Talk|File|Help|Wikipedia|Template|Category|Portal|User|Template_talk):|"
    r"\.(pdf|zip|gz|tar|png|jpe?g|gif|svg|webp|mp4|mp3|exe|dmg|iso|docx?|xlsx?|pptx?)(\?|$))", re.I)
# Facet vocabulary for the built-in sub-question templates (FTS5 prefix terms, porter-stemmed index).
_FACETS = {
    "What is known about": [],
    "What causes or drives": ["cause", "due", "because", "result", "lead", "trigger", "mechanism", "driven"],
    "What measurements or evidence": ["measur", "benchmark", "test", "result", "data", "experiment", "observ",
                                      "found", "show"],
    "What are the limitations": ["limit", "risk", "however", "caveat", "drawback", "cannot", "fail", "problem",
                                 "only", "not"],
    "How do approaches or alternatives": ["compar", "versus", "than", "alternativ", "better", "worse",
                                          "outperform", "instead", "differ", "whereas", "while", "vs"],
    "What are recent developments": ["recent", "new", "latest", "introduc", "release", "update", "2024", "2025",
                                     "2026"],
}
_STOP_WORDS = _STOP | {"vs", "versus", "about", "between", "using", "via", "into", "within", "across", "among",
                       "toward", "towards", "over", "under", "per", "effect", "effects", "impact", "role"}
# Words too generic to steer a search ("such", "study", "results"...).
_GENERIC = {"such", "also", "however", "many", "several", "various", "study", "studies", "result", "results",
            "using", "used", "use", "based", "may", "might", "one", "two", "three", "new", "first", "well", "within",
            "including", "include", "includes", "found", "show", "shows", "shown", "paper", "article", "review",
            "reviewed", "systematically", "relevant", "evaluate", "evaluated", "present", "sought", "theory", "test",
            "data", "analysis", "approach", "method", "methods", "research", "literature", "different", "significant",
            "significantly", "high", "low", "large", "small", "important", "known", "general", "case", "cases",
            "level", "levels", "effect", "effects", "impact", "role", "number", "time", "year", "years", "other",
            "would", "could", "should", "will", "can", "make", "made", "work", "works", "trial", "trials"}
SATURATION_WINDOW = 20
SATURATION_MIN_CLAIMS = 3


def _stems(text: str) -> set[str]:
    return stems(text)


class Campaign:
    def __init__(self, services):
        self.s = services
        self.db = services.db

    # -- state --------------------------------------------------------------
    @staticmethod
    def new_state(params: dict, topic: str) -> dict:
        p = {**DEFAULTS, **{k: v for k, v in (params or {}).items() if v is not None}}
        p["hours"] = max(0.01, min(48.0, float(p["hours"])))
        p["max_documents"] = max(1, min(5000, int(p["max_documents"])))
        return {
            "version": 1, "topic": topic, "params": p, "phase": "questions",
            "active_seconds": 0.0, "queries": [], "done_queries": [], "subquestions": [],
            "frontier": [], "visited": [], "doc_ids": [], "domain_counts": {},
            "yields": [], "saturation_strikes": 0, "refreshes": 0, "model_disabled": False,
            "verified_claims": [],
            "stats": {"examined": 0, "stored": 0, "rejected": 0, "failed": 0, "duplicates": 0, "skipped": 0,
                      "claims_created": 0, "claims_reinforced": 0, "contradictions": 0, "queries_run": 0,
                      "api_items": 0, "links_followed": 0, "injection_flagged": 0},
            "log": [], "stop_reason": None, "created_at": now_iso(),
        }

    def load(self, o: dict) -> dict:
        cp = loads(o["checkpoint"], {}) or {}
        if "state" not in cp:
            cp["state"] = self.new_state(cp.get("params", {}), o["goal"])
        return cp

    def save(self, oid: str, cp: dict) -> None:
        st = cp["state"]
        st["frontier"] = sorted(st["frontier"], key=lambda c: -c["score"])[:400]
        st["log"] = st["log"][-40:]
        st["yields"] = st["yields"][-SATURATION_WINDOW:]
        self.db.execute("UPDATE objectives SET checkpoint = ?, updated_at = ? WHERE id = ?", (dumps(cp), now_iso(), oid))

    def _log(self, st: dict, msg: str) -> None:
        st["log"].append(f"{now_iso()[11:19]} {msg}"[:300])

    # -- relevance ----------------------------------------------------------
    # Topic terms are weighted by how *discriminative* they are inside this campaign: a
    # term present in nearly every on-topic document ("raspberry", "pi") carries little
    # signal, a term like "throttling" carries a lot. Digits/very short tokens count less.
    ON_TOPIC = 0.5        # minimum weighted topic coverage for a document to be cited
    LINK_MIN = 0.3        # minimum coverage of anchor text/URL for a link to be followed
    CLAIM_MIN = 0.3       # minimum coverage for a claim to appear as a key finding

    def _topic_terms(self, st: dict) -> list[str]:
        return list(dict.fromkeys(tokens(st["topic"])))

    def _weights(self, st: dict) -> dict[str, float]:
        n = st.get("df_docs", 0)
        df = st.get("df", {})
        w = {}
        for t in self._topic_terms(st):
            base = 0.3 if (t.isdigit() or len(t) <= 2) else 1.0
            if n >= 5:
                base *= 1.6 - min(1.0, df.get(t, 0) / n)  # 0.6 (in every doc) .. 1.6 (in none yet)
            w[t] = base
        return w

    def concepts(self, st: dict) -> list[set[str]]:
        """Split the topic into concepts at function words:
        "thermal throttling and cooling of the Raspberry Pi 5" →
        [{thermal, throttling}, {cooling}, {raspberry, pi}]. Digits and very short
        tokens identify but do not define a concept, so they are left out."""
        if "concepts" not in st:
            groups, cur = [], []
            for w in re.findall(r"[a-z0-9]+", st["topic"].lower()):
                if w in _STOP_WORDS:
                    if cur:
                        groups.append(cur)
                    cur = []
                else:
                    cur.append(w)
            if cur:
                groups.append(cur)
            # Short words stay when they belong to a longer name ("raspberry pi" needs both words, so
            # "raspberry-red" does not match); single short words stand alone only if ≥3 letters.
            kept = [[w for w in g if not w.isdigit() and (len(w) > 2 or (len(w) == 2 and len(g) > 1))]
                    for g in groups]
            kept = [g for g in kept if g]
            st["concepts"] = [sorted(set(g)) for g in kept]
            st["concept_order"] = kept
        return [set(g) for g in st["concepts"]]

    def concept_score(self, st: dict, text: str) -> float:
        """Per concept: 1.0 when all its words occur (≥2/3 for 4+ words), 0.5 when only its
        head word (last word, as in "thermal *throttling*") occurs, else 0."""
        words = _stems(text)
        total = 0.0
        for c, order in zip(self.concepts(st), st.get("concept_order", []), strict=False):
            sc = {stem(w) for w in c}
            need = len(sc) if len(sc) <= 3 else max(2, round(len(sc) * 2 / 3))
            if len(sc & words) >= need:
                total += 1.0
            elif order and stem(order[-1]) in words:
                total += 0.5
        return total

    def concepts_covered(self, st: dict, text: str) -> int:
        words = _stems(text)
        return sum(1 for c in self.concepts(st) if {stem(w) for w in c} & words)

    def _thresholds(self, st: dict) -> tuple[float, float]:
        n = len(self.concepts(st))
        core = float(n if n <= 2 else n - 1)
        return core, max(0.5, core - 0.5)

    def is_core(self, st: dict, text: str) -> bool:
        return not self.concepts(st) or self.concept_score(st, text) >= self._thresholds(st)[0]

    def has_focus(self, st: dict, text: str) -> bool:
        """Relevant (core or background). Rejects text that shares only the entity name or
        only the aspect with the topic — e.g. Raspberry Pi model trivia for a cooling topic."""
        return not self.concepts(st) or self.concept_score(st, text) >= self._thresholds(st)[1]

    def focus_terms(self, st: dict) -> set[str]:
        return set().union(*self.concepts(st)) if self.concepts(st) else set(self._topic_terms(st))

    def coverage(self, st: dict, text: str) -> float:
        w = self._weights(st)
        total = sum(w.values())
        if not total:
            return 0.0
        words = set(tokens(text))
        return round(sum(v for t, v in w.items() if t in words) / total, 4)

    def _update_df(self, st: dict, text: str) -> None:
        words = set(tokens(text))
        st["df_docs"] = st.get("df_docs", 0) + 1
        df = st.setdefault("df", {})
        for t in self._topic_terms(st):
            if t in words:
                df[t] = df.get(t, 0) + 1

    def _terms(self, st: dict) -> set[str]:
        t = set(tokens(st["topic"]))
        for q in st["subquestions"][:12]:
            t |= set(tokens(q))
        return t

    def _score(self, st: dict, url: str, text: str, depth: int, origin: str) -> float:
        path_words = re.sub(r"[/_\-.]+", " ", urlsplit(url).path)
        rel = self.coverage(st, f"{text} {path_words}") + 0.15 * self.concepts_covered(st, f"{text} {path_words}")
        extra = self._terms(st) - set(self._topic_terms(st))
        if extra:
            rel += 0.2 * len(extra & set(tokens(f"{text} {path_words}"))) / len(extra)
        host = urlsplit(url).hostname or ""
        if domain_matches(host, self.s.settings.primary_source_domains) or host.endswith((".gov", ".edu")):
            rel += 0.15
        if origin in ("wikipedia", "searxng", "brave", "seed"):
            rel += 0.05
        return round(rel - 0.1 * depth, 4)

    # -- frontier -----------------------------------------------------------
    def _add_candidate(self, st: dict, url: str, text: str, depth: int, origin: str, title: str = "") -> bool:
        url = url.split("#", 1)[0].strip()
        if not url or url in st["visited"] or any(c["url"] == url for c in st["frontier"]):
            return False
        if _SKIP_URL.search(url):
            return False
        allowed = st["params"]["allowed_domains"] or None
        try:
            host = self.s.url_policy.check(url, allowed)
        except URLRejected:
            return False
        if st["domain_counts"].get(host, 0) >= st["params"]["max_per_domain"]:
            return False
        score = self._score(st, url, f"{title} {text}", depth, origin)
        if origin == "link" and (self.concepts_covered(st, f"{title} {text} {url}") < 1
                                 or self.coverage(st, f"{title} {text} {url}") < self.LINK_MIN * 0.7):
            return False
        st["frontier"].append({"url": url, "score": score, "depth": depth, "origin": origin, "title": title[:200]})
        return True

    def _add_query(self, st: dict, q: str, kind: str, **extra) -> bool:
        q = " ".join(q.split())[:200]
        key = normalize(q) if kind != "cites" else f"cites:{extra.get('work')}"
        if len(key) < 3 or key in st["done_queries"] or any(
                (normalize(x["q"]) if x["kind"] != "cites" else f"cites:{x.get('work')}") == key for x in st["queries"]):
            return False
        st["queries"].append({"q": q, "kind": kind, **extra})
        return True

    # -- question generation ------------------------------------------------
    QUESTION_SCHEMA = {
        "type": "object",
        "properties": {
            "subquestions": {"type": "array", "items": {"type": "string"}},
            "search_queries": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["subquestions", "search_queries"],
    }

    def _model_questions(self, o: dict, st: dict) -> bool:
        if st["model_disabled"] or not self.s.router.available:
            return False
        findings = "\n".join(f"- {c['text'][:200]}" for c in self._top_claims(st, 15))
        prompt = (f"Research topic: {st['topic']}\n"
                  f"Sub-questions so far: {st['subquestions'] or '(none)'}\n"
                  f"Queries already run: {st['done_queries'][-30:]}\n\n"
                  "Findings so far (extracted from untrusted web sources):\n"
                  + wrap_untrusted(findings or "(none yet)", "aegis-knowledge", 6000)
                  + "\n\nPropose up to 6 NEW sub-questions that a rigorous researcher would still need answered "
                  "(gaps, mechanisms, limitations, comparisons, recent developments, counter-evidence) and up to 8 "
                  "concise web/scholarly search queries (2-8 words each) to answer them. Do not repeat earlier ones.")
        try:
            r = self.s.router.generate("research_planning", prompt, json_schema=self.QUESTION_SCHEMA,
                                       max_tokens=1200, objective_id=o["id"], token_budget=o.get("budget_tokens"))
        except BudgetExceeded as exc:
            st["model_disabled"] = True
            self._log(st, f"model budget reached, continuing deterministically ({exc})")
            return False
        except ModelError as exc:
            self._log(st, f"question model unavailable ({exc.error_class}); deterministic questions used")
            return False
        data = r.data if isinstance(r.data, dict) else {}
        added = 0
        for q in [str(x) for x in data.get("subquestions", [])][:6]:
            if normalize(q) not in {normalize(x) for x in st["subquestions"]}:
                st["subquestions"].append(q[:300])
                added += 1
        for q in [str(x) for x in data.get("search_queries", [])][:8]:
            added += self._add_query(st, q, "model")
        return added > 0

    def _deterministic_questions(self, st: dict) -> bool:
        added = 0
        topic = st["topic"]
        if not st["done_queries"] and not st["queries"]:
            base = " ".join(self._base_terms(st)) or topic
            added += self._add_query(st, " ".join(tokens(topic)[:8]) or topic, "topic")
            for suffix in ("overview", "limitations", "comparison", "measurements", "latest research"):
                added += self._add_query(st, f"{base} {suffix}", "facet")
            cs = [sorted(c) for c in self.concepts(st)]
            for i in range(len(cs)):
                for j in range(i + 1, len(cs)):
                    added += self._add_query(st, " ".join(cs[i] + cs[j]), "concepts")
            if not st["subquestions"]:
                t = topic.rstrip("?. ")
                st["subquestions"] += [f"What is known about {t}?", f"What causes or drives {t}?",
                                       f"What measurements or evidence exist on {t}?",
                                       f"What are the limitations, risks or caveats of {t}?",
                                       f"How do approaches or alternatives for {t} compare?",
                                       f"What are recent developments in {t}?"]
        # New key terms that keep appearing in accepted documents but not in the topic.
        if st["doc_ids"]:
            topic_terms = set(tokens(topic))
            cnt: Counter = Counter()
            rows = self.db.query(f"SELECT extraction FROM documents WHERE id IN ({_ids(st['doc_ids'][-60:])})")
            for r in rows:
                ext = loads(r["extraction"], {})
                cnt.update(k for k in ext.get("keywords", [])[:8]
                           if k not in topic_terms and len(k) > 3 and k not in _GENERIC)
                for q in ext.get("open_questions", [])[:2]:
                    if len(set(tokens(q)) & topic_terms) and normalize(q) not in {normalize(x) for x in st["subquestions"]}:
                        st["subquestions"].append(q[:300])
            base = " ".join(self._base_terms(st))
            for term, n in cnt.most_common(6):
                if n >= 2:
                    added += self._add_query(st, f"{base} {term}", "term")
        for q in st["subquestions"][-6:]:
            added += self._add_query(st, " ".join(tokens(q)[:8]), "subquestion")
        added += self._phrase_queries(st)
        return added > 0

    def _phrase_queries(self, st: dict, limit: int = 6) -> int:
        """Mine recurring on-topic phrases from findings ("passive cooling", "thermal interface")
        and search them in combination with the rest of the topic."""
        concept_words = set().union(*self.concepts(st)) if self.concepts(st) else set()
        counts: Counter = Counter()
        for c in self._top_claims(st, 80):
            words = [w for w in tokens(c["text"]) if not w.isdigit() and len(w) > 2]
            for a, b in zip(words, words[1:], strict=False):
                if (a in concept_words) != (b in concept_words) and not ({a, b} & _GENERIC):  # topic word + new word
                    counts[(a, b)] += 1
        added = 0
        for (a, b), n in counts.most_common(30):
            if n < 2 or added >= limit:
                break
            rest = [sorted(c)[0] for c in self.concepts(st) if not ({a, b} & c)]
            added += self._add_query(st, " ".join([a, b] + rest[:2]), "phrase")
        return added

    def _base_terms(self, st: dict) -> list[str]:
        """One or two words per topic concept: the backbone of every generated query."""
        out = []
        for order in st.get("concept_order") or [sorted(c) for c in self.concepts(st)]:
            out += order[-2:]
        return out[:6] or tokens(st["topic"])[:5]

    def _verification_queries(self, st: dict) -> int:
        """Targeted corroboration searches for important single-source claims: the topic's
        concept words plus the claim's most distinctive words."""
        added = 0
        base = self._base_terms(st)
        for c in self._top_claims(st, 30):
            if c["corroboration"] <= 1 and c["id"] not in st["verified_claims"]:
                st["verified_claims"].append(c["id"])
                distinctive = [t for t in dict.fromkeys(tokens(c["text"]))
                               if t not in base and t not in _GENERIC and not t.isdigit() and len(t) > 3]
                distinctive.sort(key=len, reverse=True)
                if len(distinctive) >= 2 and self._add_query(st, " ".join(base + distinctive[:3]), "verify"):
                    added += 1
            if added >= 3:
                break
        return added

    MAX_REFRESHES = 60

    def refresh_questions(self, o: dict, st: dict) -> bool:
        """Generate new questions/queries. True only if there is now work to do."""
        if st["refreshes"] >= self.MAX_REFRESHES:
            return bool(st["queries"] or st["frontier"])
        st["refreshes"] += 1
        self._model_questions(o, st)
        self._deterministic_questions(st)
        self._verification_queries(st)
        self._log(st, f"question refresh #{st['refreshes']}: {len(st['queries'])} queries queued, "
                      f"{len(st['subquestions'])} sub-questions")
        return bool(st["queries"] or st["frontier"])

    # -- units of work --------------------------------------------------------
    def _run_query(self, o: dict, st: dict, item: dict) -> None:
        q = item["q"]
        st["done_queries"].append(normalize(q) if item["kind"] != "cites" else f"cites:{item.get('work')}")
        st["stats"]["queries_run"] += 1
        sources = st["params"]["sources"]
        fetcher = self.s.fetcher
        results = []
        if item["kind"] == "cites":
            results.append(discovery.openalex_search(fetcher, "", limit=6, contact=self.s.settings.research_contact,
                                                     cites=item.get("work")))
        elif "wikipedia" in sources:
            results.append(discovery.wikipedia_search(fetcher, q, limit=6))
        if "openalex" in sources and item["kind"] != "cites":
            results.append(discovery.openalex_search(fetcher, q, limit=4,
                                                     contact=self.s.settings.research_contact))
        if "web" in sources and self.s.settings.brave_search_api_key and item["kind"] != "cites":
            results.append(discovery.brave_search(fetcher, q, self.s.settings.brave_search_api_key, limit=10))
        if "searxng" in sources and self.s.settings.searxng_url and item["kind"] != "cites":
            results.append(discovery.searxng_search(self.s.research, q, limit=10))
        n_cand = n_items = 0
        for res in results:
            for c in res.candidates:
                n_cand += self._add_candidate(st, c.url, c.snippet, 0, c.origin, c.title)
            for item_ in res.content:
                if self._ingest_item(o, st, item_):
                    n_items += 1
            for err in res.errors:
                self._log(st, f"discovery error: {err[:160]}")
        self._log(st, f"query [{item['kind']}] '{q[:70]}': +{n_cand} candidates, +{n_items} abstracts")

    def _record(self, st: dict, out) -> None:
        stats = st["stats"]
        stats["examined"] += 1
        if out.status in ("stored", "near_duplicate"):
            stats["stored"] += 1
            if self._on_topic(st, out.document_id):
                st["doc_ids"].append(out.document_id)
            else:
                stats["off_topic"] = stats.get("off_topic", 0) + 1
                out.status = "off_topic"
        elif out.status == "duplicate":
            stats["duplicates"] += 1
        elif out.status == "rejected":
            stats["rejected"] += 1
        elif out.status == "failed":
            stats["failed"] += 1
        elif out.status == "skipped":
            # Already in the knowledge base (e.g. from an earlier campaign): reuse its evidence.
            row = self.db.one("SELECT d.id FROM documents d JOIN research_sources r ON r.id = d.source_id "
                              "WHERE r.url = ?", (out.url,))
            if row and row["id"] not in st["doc_ids"] and self._on_topic(st, row["id"]):
                st["doc_ids"].append(row["id"])
                stats["reused"] = stats.get("reused", 0) + 1
            else:
                stats["skipped"] += 1
        if out.reason.startswith("prompt-injection"):
            stats["injection_flagged"] += 1
        stats["claims_created"] += out.claims_created
        stats["claims_reinforced"] += out.claims_reinforced
        stats["contradictions"] += out.contradictions
        if out.status in ("stored", "near_duplicate", "duplicate"):
            # New knowledge *or* new corroboration of existing knowledge both count as progress.
            st["yields"].append(out.claims_created + out.claims_reinforced)

    def _on_topic(self, st: dict, doc_id: int) -> bool:
        d = self.db.one("SELECT title, summary, text FROM documents WHERE id = ?", (doc_id,))
        if not d:
            return False
        # Judge what the document is *about* (title, summary, lead), not scattered mentions deep
        # inside a very long page (a "Pacemaker" article that mentions fasting once is off-topic).
        lead = f"{d['title']} {d['summary']} {d['text'][:6000]}"
        text = f"{d['title']} {d['text'][:200_000]}"
        ok = self.coverage(st, lead) >= self.ON_TOPIC and self.has_focus(st, lead)
        if ok:
            self._update_df(st, text)
        return ok

    def _ingest_item(self, o: dict, st: dict, item: discovery.ContentItem) -> bool:
        if item.url in st["visited"] or st["domain_counts"].get(item.domain, 0) >= st["params"]["max_per_domain"]:
            return False
        # The abstract is already in hand: skip clearly off-topic items before spending budget on them.
        if self.coverage(st, f"{item.title} {item.text}") < self.ON_TOPIC or \
                not self.has_focus(st, f"{item.title} {item.text}"):
            st["visited"].append(item.url)
            st["stats"]["prefiltered"] = st["stats"].get("prefiltered", 0) + 1
            return False
        st["visited"].append(item.url)
        out = self.s.research.ingest_content(
            url=item.url, title=item.title, text=item.text, method=f"deep:{item.origin}", domain=item.domain,
            published_at=item.published_at, author=item.author, query=st["topic"], topic_name=st["topic"][:100],
            objective_id=o["id"])
        self._record(st, out)
        st["stats"]["api_items"] += 1
        if out.status in ("stored", "near_duplicate"):
            st["domain_counts"][item.domain] = st["domain_counts"].get(item.domain, 0) + 1
            if item.meta.get("work") and st["stats"].get("snowballs", 0) < 40:
                if self._add_query(st, f"works citing: {item.title[:80]}", "cites", work=item.meta["work"]):
                    st["stats"]["snowballs"] = st["stats"].get("snowballs", 0) + 1
            self._related_query(st, item.title)
            return True
        return False

    def _related_query(self, st: dict, title: str) -> None:
        """Search for related work using an on-topic document's title."""
        words = tokens(title)
        if 3 <= len(words) <= 14 and st["stats"].get("related_queries", 0) < 40:
            if self._add_query(st, " ".join(words), "related"):
                st["stats"]["related_queries"] = st["stats"].get("related_queries", 0) + 1

    def _ingest_next(self, o: dict, st: dict) -> None:
        st["frontier"].sort(key=lambda c: -c["score"])
        cand = st["frontier"].pop(0)
        st["visited"].append(cand["url"])
        host = urlsplit(cand["url"]).hostname or ""
        if st["domain_counts"].get(host, 0) >= st["params"]["max_per_domain"]:
            return
        out = self.s.research.ingest_url(
            cand["url"], method=f"deep:{cand['origin']}", query=st["topic"], objective_id=o["id"],
            topic_name=st["topic"][:100], allowed_domains=st["params"]["allowed_domains"] or None)
        self._record(st, out)
        if out.status in ("stored", "near_duplicate"):
            st["domain_counts"][host] = st["domain_counts"].get(host, 0) + 1
            self._log(st, f"stored (q {out.quality}) +{out.claims_created} claims: {cand['url'][:90]}")
            if cand.get("title"):
                self._related_query(st, cand["title"])
            if "links" in st["params"]["sources"] and cand["depth"] < st["params"]["max_depth"] \
                    and (out.quality or 0) >= 45:
                added = 0
                scored = sorted(((self._score(st, u, a, cand["depth"] + 1, "link"), u, a) for u, a in out.links),
                                reverse=True)
                for _sc, u, a in scored[:25]:
                    if added >= 8:
                        break
                    if self._add_candidate(st, u, a, cand["depth"] + 1, "link", a):
                        added += 1
                st["stats"]["links_followed"] += added
        elif out.status == "off_topic":
            self._log(st, f"off-topic, not cited: {cand['url'][:90]}")
        elif out.status not in ("skipped",):
            self._log(st, f"{out.status}: {cand['url'][:80]} ({out.reason[:80]})")

    # -- control ----------------------------------------------------------------
    def _should_stop(self, st: dict) -> str | None:
        p, stats = st["params"], st["stats"]
        if st["active_seconds"] >= p["hours"] * 3600:
            return f"time budget of {p['hours']:g} h used"
        if len(st["doc_ids"]) >= p["max_documents"]:
            return f"document limit reached ({len(st['doc_ids'])} on-topic documents)"
        if stats["examined"] >= p["max_documents"] * 4:
            return f"examination limit reached ({stats['examined']} sources examined)"
        return None

    def _saturated(self, st: dict) -> bool:
        y = st["yields"]
        return len(y) >= SATURATION_WINDOW and sum(y[-SATURATION_WINDOW:]) < SATURATION_MIN_CLAIMS

    def initialize(self, o: dict) -> dict:
        cp = self.load(o)
        st = cp["state"]
        for u in st["params"]["seed_urls"]:
            self._add_candidate(st, u, st["topic"], 0, "seed")
        self.refresh_questions(o, st)
        self._log(st, f"campaign started: {st['params']['hours']:g} h budget, up to "
                      f"{st['params']['max_documents']} documents, sources {st['params']['sources']}")
        self.save(o["id"], cp)
        return cp

    def run_slice(self, o: dict, keep_going) -> str:
        """Work for up to ``slice_seconds``. Returns 'finished', 'yield' or 'stopped'."""
        cp = self.load(o)
        st = cp["state"]
        slice_end = time.monotonic() + st["params"]["slice_seconds"]
        last = time.monotonic()
        units = 0  # every slice does at least one unit of work, so a campaign always progresses
        try:
            while True:
                now = time.monotonic()
                st["active_seconds"] += now - last
                last = now
                reason = self._should_stop(st)
                if reason:
                    st["stop_reason"] = reason
                    return "finished"
                if not keep_going():
                    return "stopped"
                if now >= slice_end and units:
                    return "yield"
                units += 1
                docs_before = st["stats"]["stored"]
                if st["queries"] and (not st["frontier"] or st["stats"]["examined"] % 6 == 0):
                    self._run_query(o, st, st["queries"].pop(0))
                elif st["frontier"]:
                    self._ingest_next(o, st)
                else:
                    if not self.refresh_questions(o, st) and not st["frontier"]:
                        st["stop_reason"] = "sources exhausted: no new questions or candidates"
                        return "finished"
                if st["stats"]["stored"] != docs_before and len(st["doc_ids"]) and len(st["doc_ids"]) % 15 == 0:
                    self._verification_queries(st)
                    if len(st["doc_ids"]) % 30 == 0:
                        self.refresh_questions(o, st)
                if self._saturated(st):
                    st["saturation_strikes"] += 1
                    st["yields"] = []
                    self._log(st, f"saturation detected (strike {st['saturation_strikes']}); generating new questions")
                    refreshed = self.refresh_questions(o, st)
                    if st["params"]["stop_on_saturation"] and (st["saturation_strikes"] >= 3 or not refreshed):
                        st["stop_reason"] = "diminishing returns: new sources no longer add new knowledge"
                        return "finished"
                self.save(o["id"], cp)
        finally:
            self.save(o["id"], cp)

    # -- reporting ----------------------------------------------------------
    def _similar_support(self, st: dict, findings: list[dict]) -> dict[int, int]:
        """Consensus signal: how many *other* documents contain a similarly worded statement
        (token Jaccard ≥ 0.4). Reported as "similar statements", never merged into corroboration."""
        if not st["doc_ids"]:
            return {}
        rows = self.db.query(
            f"SELECT c.id, c.text, e.document_id FROM claims c JOIN claim_evidence e ON e.claim_id = c.id "
            f"WHERE e.document_id IN ({_ids(st['doc_ids'])}) AND c.origin = 'source'")
        toks = [(r["id"], r["document_id"], set(tokens(r["text"]))) for r in rows]
        out = {}
        for f in findings:
            ft = set(tokens(f["text"]))
            own = {d for cid, d, _t in toks if cid == f["id"]}
            docs = {d for cid, d, t in toks if cid != f["id"] and d not in own and t and ft
                    and len(ft & t) / len(ft | t) >= 0.4}
            out[f["id"]] = len(docs)
        return out

    def _answer(self, st: dict, question: str) -> list[dict]:
        """Claims answering a sub-question: facet terms AND focus terms, ranked by BM25."""
        facets = next((v for k, v in _FACETS.items() if question.startswith(k)), None)
        if facets is None:
            topic = set(self._topic_terms(st))
            facets = [t for t in tokens(question) if t not in topic and len(t) > 2][:8]
        focus = sorted(self.focus_terms(st))
        if not focus:
            return []
        fq = "(" + " OR ".join(f'"{t}"' for t in focus) + ")"
        if facets:
            fq += " AND (" + " OR ".join(f'"{t}"*' for t in facets) + ")"
        try:
            rows = self.db.query("SELECT c.* FROM claims_fts f JOIN claims c ON c.id = f.rowid WHERE claims_fts "
                                 "MATCH ? ORDER BY bm25(claims_fts) LIMIT 80", (fq,))
        except Exception:  # malformed FTS expression from unusual tokens
            return []
        return [c for c in rows if c["status"] != "retracted" and self.has_focus(st, c["text"])
                and not _FIRST_PERSON.search(c["text"])
                and self.coverage(st, c["text"]) >= self.CLAIM_MIN * 0.8]

    def _campaign_claims(self, st: dict) -> list[dict]:
        if not st["doc_ids"]:
            return []
        return self.db.query(
            f"SELECT DISTINCT c.* FROM claims c JOIN claim_evidence e ON e.claim_id = c.id "
            f"WHERE e.document_id IN ({_ids(st['doc_ids'])}) AND c.origin = 'source' AND c.status != 'retracted'")

    def _top_claims(self, st: dict, n: int, tier: str = "any") -> list[dict]:
        rank = {"high": 3, "medium": 2, "low": 1}
        extra = self._terms(st) - set(self._topic_terms(st))
        scored = []
        for c in self._campaign_claims(st):
            cov = self.coverage(st, c["text"])
            sub = len(extra & set(tokens(c["text"]))) / len(extra) if extra else 0
            if (cov < self.CLAIM_MIN and sub < 0.25) or not self.has_focus(st, c["text"]):
                continue
            if _FIRST_PERSON.search(c["text"]):
                continue  # anecdotes are kept in the knowledge base but are not findings
            if tier == "core" and not self.is_core(st, c["text"]):
                continue
            if tier == "background" and self.is_core(st, c["text"]):
                continue
            score = (cov * 6 + sub * 2 + self.concept_score(st, c["text"]) * 1.5
                     + rank.get(c["confidence"], 0) + min(c["corroboration"], 5)
                     - (2 if c["status"] in ("contested", "outdated") else 0)
                     - (1.5 if _FIRST_PERSON.search(c["text"]) else 0) - (0.5 if c["kind"] == "procedure" else 0))
            scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _s, c in scored[:n]]

    def report(self, o: dict, final: bool = True) -> str:
        cp = self.load(o)
        st = cp["state"]
        docs = {d["id"]: d for d in self.db.query(
            f"SELECT id, url, final_url, domain, title, quality_score, published_at, retrieved_at, extraction "
            f"FROM documents WHERE id IN ({_ids(st['doc_ids'])})")} if st["doc_ids"] else {}
        numbering: dict[int, int] = {}

        def cite(claim_id: int) -> str:
            ev = self.db.query("SELECT document_id FROM claim_evidence WHERE claim_id = ? AND document_id IS NOT NULL",
                               (claim_id,))
            refs = []
            for e in ev:
                if e["document_id"] in docs:
                    numbering.setdefault(e["document_id"], len(numbering) + 1)
                    refs.append(numbering[e["document_id"]])
            return "".join(f"[{r}]" for r in sorted(set(refs))[:6])

        s, p = st["stats"], st["params"]
        hrs = st["active_seconds"] / 3600
        lines = [f"# Research report: {st['topic']}", "",
                 f"*{'Final' if final else 'Interim'} report · {now_iso()} · {hrs:.2f} h of active research"
                 f" (budget {p['hours']:g} h) · stop reason: {st.get('stop_reason') or 'in progress'}*", ""]
        claims = self._top_claims(st, 25, tier="core")
        background = self._top_claims(st, 12, tier="background")
        claim_ids = {c["id"] for c in self._campaign_claims(st)}

        gaps: list[str] = []
        if st["subquestions"]:
            lines += ["## Answers by sub-question", ""]
            used: set[int] = set()
            for q in st["subquestions"][:12]:
                pool = [c for c in self._answer(st, q) if c["id"] in claim_ids and c["id"] not in used]
                hits = ([c for c in pool if self.is_core(st, c["text"])]
                        + [c for c in pool if not self.is_core(st, c["text"])])[:3]
                used.update(c["id"] for c in hits)
                lines.append(f"**{q}**")
                if hits:
                    lines += [f"- {c['text']} {cite(c['id'])} *(confidence {c['confidence']}, "
                              f"{c['corroboration']} source{'s' if c['corroboration'] != 1 else ''})*" for c in hits]
                else:
                    gaps.append(q)
                    lines.append("- *No source-backed answer found — research gap.*")
                lines.append("")

        lines += ["## Key findings", "", "*Evidence that addresses the topic as a whole.*", ""]
        if claims:
            similar = self._similar_support(st, claims[:20])
            for c in claims[:20]:
                flag = " ⚠ contested" if c["status"] == "contested" else ""
                sim = similar.get(c["id"], 0)
                also = f"; similar statements in {sim} other source{'s' if sim != 1 else ''}" if sim else ""
                lines.append(f"- {c['text']} {cite(c['id'])} — *{c['confidence']} confidence, "
                             f"{c['corroboration']} independent source(s){also}{flag}*")
        else:
            lines.append("- *No source-backed findings that address the whole topic — see background.*")
        lines.append("")
        if background:
            lines += ["## Background and context", "",
                      "*Related evidence that addresses part of the topic (e.g. the mechanism in general).*", ""]
            lines += [f"- {c['text']} {cite(c['id'])} — *{c['confidence']} confidence*" for c in background]
            lines.append("")

        contested = self.db.query(
            "SELECT r.note, a.id AS a_id, a.text AS a_text, b.id AS b_id, b.text AS b_text FROM relationships r "
            "JOIN claims a ON a.id = r.from_id JOIN claims b ON b.id = r.to_id WHERE r.kind = 'contradicts' "
            "AND r.resolved = 0") if claim_ids else []
        contested = [c for c in contested if c["a_id"] in claim_ids or c["b_id"] in claim_ids][:10]
        if contested:
            lines += ["## Contested points (sources disagree)", ""]
            for c in contested:
                lines.append(f"- {c['a_text']} {cite(c['a_id'])}  \n  **vs.** {c['b_text']} {cite(c['b_id'])}  \n"
                             f"  *({c['note']})*")
            lines.append("")

        defs, procs, lims, questions = {}, [], [], []
        for d in sorted(docs.values(), key=lambda d: -d["quality_score"]):
            ext = loads(d["extraction"], {})
            for x in ext.get("definitions", []):
                if self.has_focus(st, x["definition"]):
                    defs.setdefault(x["term"].lower(), (x["definition"], d["id"]))
            procs += [(x, d["id"]) for x in ext.get("procedures", []) if self.has_focus(st, x)][:3]
            lims += [(x, d["id"]) for x in ext.get("limitations", []) if self.has_focus(st, x)][:3]
            questions += [q for q in ext.get("open_questions", []) if self.has_focus(st, q)][:2]

        def ref(doc_id):
            numbering.setdefault(doc_id, len(numbering) + 1)
            return f"[{numbering[doc_id]}]"

        procs = list({normalize(t): (t, i) for t, i in procs}.values())
        lims = list({normalize(t): (t, i) for t, i in lims}.values())
        if defs:
            lines += ["## Definitions", ""] + [f"- {v[0]} {ref(v[1])}" for v in list(defs.values())[:10]] + [""]
        if procs:
            lines += ["## Procedures found", ""] + [f"- {t} {ref(i)}" for t, i in procs[:10]] + [""]
        if lims:
            lines += ["## Limitations and caveats", ""] + [f"- {t} {ref(i)}" for t, i in lims[:10]] + [""]
        oq = list(dict.fromkeys(questions))[:8]
        if oq or gaps:
            lines += ["## Open questions and gaps", ""] + [f"- {q}" for q in oq] + \
                [f"- (gap) {q}" for q in gaps] + [""]

        synthesis = self._synthesis(o, st, claims[:20], numbering) if final else None
        if synthesis:
            lines[4:4] = ["## Executive summary", "",
                          "*Model-written synthesis of the cited findings below — verify against the sources.*", "",
                          synthesis, ""]

        domains = Counter(d["domain"] for d in docs.values())
        lines += ["## Method", "",
                  f"- Sources examined: {s['examined']} · stored: {s['stored']} · rejected: {s['rejected']} · "
                  f"failed: {s['failed']} · off-topic (kept, not cited): {s.get('off_topic', 0)} · "
                  f"duplicates: {s['duplicates']} · reused from earlier research: "
                  f"{s.get('reused', 0)} · injection-flagged: {s['injection_flagged']}",
                  f"- Distinct domains/publishers: {len(domains)} · scholarly abstracts: {s['api_items']} · "
                  f"links followed: {s['links_followed']}",
                  f"- Search queries run: {s['queries_run']} · question refreshes: {st['refreshes']} · "
                  f"sub-questions: {len(st['subquestions'])}",
                  f"- Claims created: {s['claims_created']} · reinforced by other sources: {s['claims_reinforced']} · "
                  f"contradictions flagged: {s['contradictions']}",
                  "- Confidence is a rubric (independent corroboration, primary sources), not a probability. "
                  "Duplicates and same-publisher pages never count as independent corroboration.", ""]

        lines += ["## Sources", ""]
        cited_docs = sorted((d for d in docs.values() if d["id"] in numbering), key=lambda d: numbering[d["id"]])
        for d in cited_docs:
            lines.append(f"{numbering[d['id']]}. {d['title'] or d['final_url']} — {d['domain']} · "
                         f"quality {d['quality_score']:.0f} · <{d['final_url']}>")
        uncited = len(docs) - len(cited_docs)
        if uncited:
            lines += ["", f"*Also consulted: {uncited} further on-topic document(s) whose claims were not among the "
                          "findings above; they are searchable in the knowledge base.*"]
        return "\n".join(lines) + "\n"

    SYNTH_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}

    def _synthesis(self, o: dict, st: dict, claims: list[dict], numbering: dict[int, int]) -> str | None:
        if st["model_disabled"] or not self.s.router.available or not claims:
            return None
        cited = []
        for c in claims:
            refs = sorted({numbering[e["document_id"]] for e in self.db.query(
                "SELECT document_id FROM claim_evidence WHERE claim_id = ?", (c["id"],)) if e["document_id"] in numbering})
            if refs:
                cited.append(f"- {c['text']} {''.join(f'[{r}]' for r in refs)} ({c['confidence']})")
        if not cited:
            return None
        prompt = (f"Topic: {st['topic']}\nWrite an executive summary (max 300 words) of what the evidence says, "
                  "using ONLY the findings below, citing them with their [n] markers. State uncertainty and "
                  "disagreements explicitly. Do not add facts that are not in the findings.\n\n"
                  + wrap_untrusted("\n".join(cited), "aegis-findings", 8000))
        try:
            r = self.s.router.generate("summary", prompt, json_schema=self.SYNTH_SCHEMA, max_tokens=900,
                                       objective_id=o["id"])
        except ModelError:
            return None
        text = str((r.data or {}).get("summary", "")).strip()
        valid = set(numbering.values())
        # Drop citations that point at nothing; never let the model invent sources.
        text = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if int(m.group(1)) in valid else "", text)
        return text[:4000] or None

    def progress(self, o: dict) -> dict:
        st = self.load(o)["state"]
        p = st["params"]
        return {
            "topic": st["topic"], "params": p, "stats": st["stats"], "subquestions": st["subquestions"],
            "queued_queries": [q["q"] for q in st["queries"][:10]], "frontier_size": len(st["frontier"]),
            "top_frontier": [c["url"] for c in sorted(st["frontier"], key=lambda c: -c["score"])[:5]],
            "active_hours": round(st["active_seconds"] / 3600, 3), "budget_hours": p["hours"],
            "percent_time": round(100 * st["active_seconds"] / (p["hours"] * 3600), 1),
            "domains": len(st["domain_counts"]), "log": st["log"][-15:], "stop_reason": st.get("stop_reason"),
            "model_disabled": st["model_disabled"],
        }


def _ids(ids: list[int]) -> str:
    return ",".join(str(int(i)) for i in ids) or "NULL"
