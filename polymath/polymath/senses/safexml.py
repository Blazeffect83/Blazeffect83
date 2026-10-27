"""XML parsing for untrusted input (feeds, sitemaps, crawled XML).

Entity declarations are rejected outright (billion-laughs / quadratic blowup /
external entities), and external DTDs are never fetched. Parsing is done by
expat through ElementTree's TreeBuilder.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET
from xml.parsers import expat

MAX_XML_BYTES = 20_000_000


class UnsafeXML(ValueError):
    pass


def parse_untrusted_xml(data: bytes) -> ET.Element:
    if len(data) > MAX_XML_BYTES:
        raise UnsafeXML("XML document too large")
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")

    def reject_entity(*_: Any) -> None:
        raise UnsafeXML("entity declarations are not allowed")

    def start(tag: str, attrs: dict[str, str]) -> None:
        builder.start(_ns(tag), {_ns(k): v for k, v in attrs.items()})

    def end(tag: str) -> None:
        builder.end(_ns(tag))

    parser.EntityDeclHandler = reject_entity
    parser.UnparsedEntityDeclHandler = reject_entity
    parser.ExternalEntityRefHandler = lambda *a: 0
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.buffer_text = True
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise UnsafeXML(f"malformed XML: {exc}") from exc
    root = builder.close()
    return root


def _ns(name: str) -> str:
    # expat yields "uri}local"; ElementTree convention is "{uri}local".
    return "{" + name if "}" in name else name
