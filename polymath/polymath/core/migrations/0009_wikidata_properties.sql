-- Where each Wikidata property page lives in the XML multistream dump (see senses/wikidata_props.py).
CREATE TABLE wd_property_pages (
    pid      TEXT PRIMARY KEY,
    dump_url TEXT NOT NULL,
    stream   INTEGER NOT NULL,   -- byte offset of the bz2 stream holding the page
    next     INTEGER NOT NULL,   -- byte offset where that stream ends
    fetched  REAL                -- when the stream was last read (NULL: not yet)
);
CREATE INDEX wd_property_pages_due ON wd_property_pages (fetched);
