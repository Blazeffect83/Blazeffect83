"""A tiny hand-made world for the v0.5 skills: real names, Wikidata keys, values with units and dates."""

from __future__ import annotations

from typing import Any

from polymath.core.db import Database
from polymath.memory.graph import KnowledgeGraph


class World:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.g = KnowledgeGraph(db)
        self.e: dict[str, int] = {}

    def pred(self, key: str, label: str, datatype: str | None = None) -> int:
        return self.g.predicate(key, label, datatype=datatype)

    def ent(self, key: str, label: str, *aliases: str, kind: str = "item") -> int:
        eid = self.g.upsert_entity(key, label, kind=kind)
        self.g.add_alias(label, eid, "label", 3)
        for a in aliases:
            self.g.add_alias(a, eid, "alias", 1)
        self.e[label] = eid
        return eid

    def fact(self, s: str | int, key: str, label: str, *, o: str | int | None = None, value: Any = None,
             conf: float = 0.9, kind: str = "wikidata", source: str = "wikidata", datatype: str | None = None) -> int:  # fmt: skip
        sid = self.e[s] if isinstance(s, str) else s
        p = self.pred(key, label, datatype)
        if o is not None:
            oid = self.e[o] if isinstance(o, str) else o
            tid, _ = self.g.add_triple(sid, p, o=oid, confidence=conf, kind=kind, source=source, detail=key)
        else:
            tid, _ = self.g.add_triple(sid, p, value=value, confidence=conf, kind=kind, source=source, detail=key)
        return tid


def q(amount: float, unit: str | None = None) -> dict[str, Any]:
    return {"amount": amount, "unit": unit}


def t(date: str) -> dict[str, str]:
    return {"time": date}


def geo_world(db: Database) -> World:
    """Places with coordinates, areas, populations; people with lifespans; a few classes."""
    w = World(db)
    for key, label in (("Q6256", "country"), ("Q515", "city"), ("Q8502", "mountain"), ("Q4022", "river"),
                       ("Q5", "human"), ("Q5107", "continent")):  # fmt: skip
        w.ent(key, label)
    w.ent("Q142", "France")
    w.ent("Q29", "Spain")
    w.ent("Q183", "Germany")
    w.ent("Q90", "Paris")
    w.ent("Q64", "Berlin")
    w.ent("Q350", "Bonn")
    w.ent("Q1490", "Tokyo")
    w.ent("Q5465", "Seattle")
    w.ent("Q243", "Eiffel Tower")
    w.ent("Q1726", "Versailles")
    w.ent("Q513", "Mount Everest", "Everest")
    w.ent("Q43512", "K2")
    w.ent("Q1471", "Seine")
    w.ent("Q2851133", "Anne Hidalgo")
    for city in ("Paris", "Berlin", "Bonn", "Tokyo", "Seattle", "Versailles"):
        w.fact(city, "P31", "instance of", o="city")
    for country in ("France", "Spain", "Germany"):
        w.fact(country, "P31", "instance of", o="country")
    w.fact("Seine", "P31", "instance of", o="river")
    w.fact("Mount Everest", "P31", "instance of", o="mountain")
    w.fact("K2", "P31", "instance of", o="mountain")
    w.fact("France", "P36", "capital", o="Paris")
    w.fact("Germany", "P36", "capital", o="Berlin")
    w.fact("Paris", "P6", "head of government", o="Anne Hidalgo")
    w.fact("Seine", "P206", "flows through", o="Paris")  # stands in for "located in or next to body of water"
    w.fact("Paris", "P17", "country", o="France")
    w.fact("Mount Everest", "P2044", "elevation above sea level", value=q(8849, "Q11573"), datatype="quantity")
    w.fact("K2", "P2044", "elevation above sea level", value=q(28251, "Q3710"), datatype="quantity")  # feet
    w.fact("France", "P2046", "area", value=q(643801, "Q712226"), datatype="quantity")
    w.fact("Spain", "P2046", "area", value=q(505990, "Q712226"), datatype="quantity")
    w.fact("France", "P1082", "population", value=q(68_000_000), datatype="quantity")
    w.fact("Spain", "P1082", "population", value=q(48_000_000), datatype="quantity")
    w.fact("France", "P571", "inception", value=t("843"), datatype="time")
    w.fact("Germany", "P571", "inception", value=t("1871-01-18"), datatype="time")
    coords = {"Paris": (48.8566, 2.3522), "Berlin": (52.52, 13.405), "Tokyo": (35.6762, 139.6503),
              "Seattle": (47.6062, -122.3321), "Eiffel Tower": (48.8584, 2.2945), "Versailles": (48.8049, 2.1204),
              "Bonn": (50.7374, 7.0982)}  # fmt: skip
    for name, (lat, lon) in coords.items():
        w.fact(name, "P625", "coordinate location", value={"lat": lat, "lon": lon})
    people = {"Wolfgang Amadeus Mozart": ("Q254", "1756-01-27", "1791-12-05"),
              "Joseph Haydn": ("Q7349", "1732-03-31", "1809-05-31"),
              "Ludwig van Beethoven": ("Q255", "1770-12-17", "1827-03-26"),
              "Albert Einstein": ("Q937", "1879-03-14", "1955-04-18"),
              "Isaac Newton": ("Q935", "1643-01-04", "1727-03-31"),
              "Gottfried Wilhelm Leibniz": ("Q9047", "1646-07-01", "1716-11-14")}  # fmt: skip
    for name, (key, born, died) in people.items():
        w.ent(key, name, name.split()[-1])
        w.fact(name, "P31", "instance of", o="human")
        w.fact(name, "P569", "date of birth", value=t(born), datatype="time")
        w.fact(name, "P570", "date of death", value=t(died), datatype="time")
    w.ent("Q1", "Moon landing")
    w.fact("Moon landing", "P585", "point in time", value=t("1969-07-20"), datatype="time")
    for i, name in enumerate(w.e):
        db.execute("UPDATE entities SET pagerank = ? WHERE id = ?", (1.0 / (i + 1), w.e[name]))
    return w
