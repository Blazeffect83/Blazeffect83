"""A small, fully offline knowledge base for evaluation / interface / drive tests.

Countries with capitals (Wikidata-sourced, entity-valued), a few literal facts,
Wikipedia-style articles indexed in the full-text index, topics with documents,
and a property entity whose alias lets the answerer map "capital city" → capital.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polymath.core.db import Database
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.graph import KnowledgeGraph
from polymath.memory.text_index import TextIndex


@dataclass
class KB:
    graph: KnowledgeGraph
    store: DocumentStore
    countries: list[int] = field(default_factory=list)
    cities: list[int] = field(default_factory=list)
    preds: dict[str, int] = field(default_factory=dict)
    docs: dict[str, int] = field(default_factory=dict)
    topics: dict[str, int] = field(default_factory=dict)
    people: list[int] = field(default_factory=list)


def build(db: Database, n: int = 40) -> KB:
    g = KnowledgeGraph(db)
    store = DocumentStore(db)
    text = TextIndex(db, store)
    kb = KB(g, store)
    kb.preds = {
        "capital": g.predicate("P36", "capital"),
        "p31": g.predicate("P31", "instance of"),
        "birth": g.predicate("P569", "date of birth", datatype="time"),
        "population": g.predicate("P1082", "population", datatype="quantity"),
        "country": g.predicate("P17", "country"),
    }
    prop = g.upsert_entity("P36", "capital", kind="property")
    g.add_alias("capital city", prop, "wikidata", 1)
    country_cls = g.upsert_entity("Q6256", "country")
    city_cls = g.upsert_entity("Q515", "city")
    topic_geo = int(
        db.execute(
            "INSERT INTO topics(name, kind, n_docs, n_total) VALUES('Geography', 'category', ?, ?)", (n, n)
        ).lastrowid
        or 0
    )
    topic_hist = int(
        db.execute("INSERT INTO topics(name, kind, n_docs, n_total) VALUES('History', 'category', 5, 5)").lastrowid or 0
    )
    kb.topics = {"Geography": topic_geo, "History": topic_hist}
    for i in range(n):
        country, city = f"Country{i:02d}", f"Capitol{i:02d}"
        body = (
            f"{country} is a sovereign state with a long coastline and mountains. {city} is the capital of {country} "
            f"and its largest city, home to the parliament. The economy of {country} relies on farming and trade. "
            f"Visitors to {city} see old markets, museums and the river port."
        )
        did, _ = store.add(
            Document(
                "wikipedia",
                f"en:{country}",
                country,
                body,
                "CC BY-SA 4.0",
                url=f"https://en.wikipedia.org/wiki/{country}",
            )
        )
        text.index(did, country, body)
        kb.docs[country] = did
        c = g.upsert_entity(f"Q{100 + i}", country, description=f"country number {i}", wiki_title=country, doc_id=did)
        k = g.upsert_entity(f"Q{1000 + i}", city, wiki_title=city)
        g.add_alias(country, c, "title", 5)
        g.add_alias(city, k, "title", 5)
        g.add_triple(c, kb.preds["capital"], o=k, kind="wikidata", source="wikidata", confidence=0.9)
        g.add_triple(c, kb.preds["p31"], o=country_cls, kind="wikidata", source="wikidata", confidence=0.9)
        g.add_triple(k, kb.preds["p31"], o=city_cls, kind="wikidata", source="wikidata", confidence=0.9)
        g.add_triple(k, kb.preds["country"], o=c, kind="wikidata", source="wikidata", confidence=0.9)
        g.add_triple(
            c,
            kb.preds["population"],
            value={"amount": 1_000_000 + i, "unit": None},
            kind="wikidata",
            source="wikidata",
            confidence=0.9,
        )
        db.execute("INSERT INTO doc_topics(doc_id, topic_id, weight) VALUES(?,?,1)", (did, topic_geo))
        db.execute("INSERT INTO doc_entities(doc_id, entity_id, count, score) VALUES(?,?,2,1)", (did, k))
        kb.countries.append(c)
        kb.cities.append(k)
    for j in range(5):
        name = f"Person{j}"
        body = f"{name} was a historian who wrote about the old kingdoms and their wars, born long ago."
        did, _ = store.add(Document("wikipedia", f"en:{name}", name, body, "CC BY-SA 4.0"))
        text.index(did, name, body)
        p = g.upsert_entity(f"Q{5000 + j}", name, wiki_title=name, doc_id=did)
        g.add_alias(name, p, "title", 3)
        g.add_triple(
            p,
            kb.preds["birth"],
            value={"time": f"19{10 + j}-05-0{j + 1}"},
            kind="infobox",
            source="wikipedia",
            doc_id=did,
            confidence=0.8,
        )
        db.execute("INSERT INTO doc_topics(doc_id, topic_id, weight) VALUES(?,?,1)", (did, topic_hist))
        kb.people.append(p)
    return kb
