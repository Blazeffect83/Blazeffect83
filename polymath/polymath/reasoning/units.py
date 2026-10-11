"""Units: quantities from Wikidata (unit Q-ids) and infoboxes (free text) on one scale, so they can be compared.

``to_base(amount, unit)`` returns ``(value, dimension)`` in a base unit per dimension (metre, square metre,
kilogram, second, kelvin, cubic metre, metre per second), or None for a unit it does not know (currencies,
counts). Two quantities compare only within one dimension.
"""

from __future__ import annotations

import math
import re

# Wikidata unit → (factor to the base unit, dimension); temperatures are converted separately (they have an offset)
WIKIDATA: dict[str, tuple[float, str]] = {
    "Q11573": (1.0, "length"),  # metre
    "Q828224": (1000.0, "length"),  # kilometre
    "Q174728": (0.01, "length"),  # centimetre
    "Q174789": (0.001, "length"),  # millimetre
    "Q3710": (0.3048, "length"),  # foot
    "Q218593": (0.0254, "length"),  # inch
    "Q253276": (1609.344, "length"),  # mile
    "Q482798": (0.9144, "length"),  # yard
    "Q93318": (1852.0, "length"),  # nautical mile
    "Q1811": (1.495978707e11, "length"),  # astronomical unit
    "Q531": (9.4607e15, "length"),  # light-year
    "Q12129": (3.0857e16, "length"),  # parsec
    "Q25343": (1.0, "area"),  # square metre
    "Q712226": (1e6, "area"),  # square kilometre
    "Q232291": (2.589988e6, "area"),  # square mile
    "Q35852": (1e4, "area"),  # hectare
    "Q81292": (4046.8564, "area"),  # acre
    "Q857027": (0.09290304, "area"),  # square foot
    "Q11570": (1.0, "mass"),  # kilogram
    "Q41803": (0.001, "mass"),  # gram
    "Q191118": (1000.0, "mass"),  # tonne
    "Q100995": (0.45359237, "mass"),  # pound
    "Q48013": (0.028349523, "mass"),  # ounce
    "Q11574": (1.0, "time"),  # second
    "Q7727": (60.0, "time"),  # minute
    "Q25235": (3600.0, "time"),  # hour
    "Q573": (86400.0, "time"),  # day
    "Q577": (31_557_600.0, "time"),  # year
    "Q11582": (0.001, "volume"),  # litre
    "Q25517": (1.0, "volume"),  # cubic metre
    "Q4243638": (1e9, "volume"),  # cubic kilometre
    "Q180154": (1 / 3.6, "speed"),  # kilometre per hour
    "Q182429": (1.0, "speed"),  # metre per second
    "Q211256": (0.44704, "speed"),  # mile per hour
    "Q128822": (0.514444, "speed"),  # knot
}
TEMPERATURE = {"Q25267": "C", "Q42289": "F", "Q11579": "K"}
# free-text units written in infoboxes
TEXT: dict[str, tuple[float, str]] = {
    "m": (1.0, "length"), "metre": (1.0, "length"), "meter": (1.0, "length"), "metres": (1.0, "length"),
    "meters": (1.0, "length"), "km": (1000.0, "length"), "kilometres": (1000.0, "length"),
    "kilometers": (1000.0, "length"), "cm": (0.01, "length"), "mm": (0.001, "length"), "ft": (0.3048, "length"),
    "feet": (0.3048, "length"), "foot": (0.3048, "length"), "in": (0.0254, "length"), "inches": (0.0254, "length"),
    "mi": (1609.344, "length"), "miles": (1609.344, "length"), "km2": (1e6, "area"), "km²": (1e6, "area"),
    "sq km": (1e6, "area"), "square kilometres": (1e6, "area"), "square kilometers": (1e6, "area"),
    "sq mi": (2.589988e6, "area"), "square miles": (2.589988e6, "area"), "mi2": (2.589988e6, "area"),
    "mi²": (2.589988e6, "area"), "ha": (1e4, "area"), "hectares": (1e4, "area"), "acres": (4046.8564, "area"),
    "m2": (1.0, "area"), "m²": (1.0, "area"), "kg": (1.0, "mass"), "g": (0.001, "mass"), "t": (1000.0, "mass"),
    "tonnes": (1000.0, "mass"), "lb": (0.45359237, "mass"), "lbs": (0.45359237, "mass"),
    "pounds": (0.45359237, "mass"), "km/h": (1 / 3.6, "speed"), "mph": (0.44704, "speed"),
    "m/s": (1.0, "speed"), "l": (0.001, "volume"), "litres": (0.001, "volume"), "liters": (0.001, "volume"),
}  # fmt: skip
TEXT_TEMPERATURE = {"°c": "C", "c": "C", "°f": "F", "f": "F", "k": "K"}
# how a value of each dimension is shown back: (unit name, size of one in the base unit)
SHOW = {
    "length": [("km", 1000.0), ("m", 1.0)],
    "area": [("km²", 1e6), ("m²", 1.0)],
    "mass": [("t", 1000.0), ("kg", 1.0), ("g", 0.001)],
    "time": [("years", 31_557_600.0), ("days", 86400.0), ("hours", 3600.0), ("s", 1.0)],
    "volume": [("km³", 1e9), ("m³", 1.0), ("litres", 0.001)],
    "speed": [("km/h", 1 / 3.6)],
    "temperature": [("°C", 1.0)],
}


def to_base(amount: float, unit: str | None) -> tuple[float, str] | None:
    """(value in the base unit, dimension); ``unit`` None is a plain count, returned as dimension "count"."""
    if unit is None or unit == "":
        return float(amount), "count"
    u = str(unit).strip()
    if u in WIKIDATA:
        f, dim = WIKIDATA[u]
        return float(amount) * f, dim
    t = TEMPERATURE.get(u) or TEXT_TEMPERATURE.get(u.lower())
    if t:
        return _celsius(float(amount), t), "temperature"
    key = re.sub(r"\s+", " ", u.lower().strip(" .")).replace("sq.", "sq")
    if key in TEXT:
        f, dim = TEXT[key]
        return float(amount) * f, dim
    return None


def _celsius(v: float, scale: str) -> float:
    return {"C": v, "F": (v - 32) * 5 / 9, "K": v - 273.15}[scale]


def show(value: float, dim: str) -> str:
    """A base-unit value written in a readable unit: 8,849 m, 643,801 km², 1.5 t."""
    if dim == "count":
        return _num(value)
    units = SHOW.get(dim) or [("", 1.0)]
    for name, size in units:
        if abs(value) >= size or (name, size) == units[-1]:
            return f"{_num(value / size)} {name}".strip()
    return _num(value)


def _num(v: float) -> str:
    if v == 0:
        return "0"
    if abs(v) >= 100 or float(v).is_integer():
        return f"{v:,.0f}"
    digits = max(0, 2 - math.floor(math.log10(abs(v))))
    return f"{v:,.{digits}f}"
