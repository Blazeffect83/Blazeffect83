# Sources

Polymath learns only from **downloadable public dump files** and a **polite crawler** — no third-party web
service APIs. Every stored item carries its license; answers cite it. `polymath sources` prints this table
from the code.

| source | what | from | license recorded per item |
|---|---|---|---|
| wikipedia | articles, links, categories, infoboxes, redirects | `dumps.wikimedia.org/<lang>wiki/<date>/…pages-articles-multistream*.xml.bz2` (finished dumps only, via `dumpstatus.json`) | CC BY-SA 4.0 (and GFDL) |
| wikidata | entities: labels, aliases, descriptions, statements | `dumps.wikimedia.org/wikidatawiki/entities/latest-all.json.bz2`; property pages from the XML multistream (`wikidatawiki/<date>/…multistream*.xml.bz2` + index) | CC0 1.0 |
| openalex | scholarly works: titles, abstracts, concepts | the OpenAlex snapshot on `openalex.s3.amazonaws.com` (plain HTTPS, manifest + gzip JSONL) | metadata CC0 1.0; abstract © publisher; OA license when present |
| pubmed | biomedical citations and abstracts, MeSH | `ftp.ncbi.nlm.nih.gov/pubmed/updatefiles` (HTTPS) | NLM terms; abstracts may be © publishers |
| gutenberg | public-domain books | catalog `pg_catalog.csv.gz` and texts from the official mirror `gutenberg.pglaf.org` | public domain in the USA (PG license for the e-text) |
| stackexchange | questions and accepted/high-score answers | `archive.org/download/stackexchange/<site>.7z` | CC BY-SA (version per post) |
| feed | RSS 2.0 / RDF / Atom items | your `senses.feeds`, or the built-in list of 46 public science/tech/news feeds (no aggregators) | per publisher, recorded per item |
| web | pages from allow-listed domains | the crawler, from `senses.seeds` (or built-in reference sites) and links within the allow list | Creative Commons when the page declares it, else "unknown" |

## Politeness and safety

- **Identity.** The User-Agent is `PolymathBot/0.1 (+project URL)`. You can add a contact e-mail with
  `senses.contact`; Wikimedia recommends it.
- **Crawler:**
  - It obeys robots.txt per RFC 9309: longest match, `*` and `$` wildcards, `Crawl-delay`. robots.txt is cached
    per host. An unreachable or 5xx robots.txt means "disallow everything" until it is retried; a 404 means
    no restrictions, as the RFC specifies.
  - At most **1 request per second per host**, enforced by a persisted token bucket of capacity 1, so restarts
    cannot burst. `crawl_rate` can only lower this.
  - Errors back off exponentially, and `429`/`Retry-After` is honoured.
  - **Only approved sites are ever fetched.** The crawler checks every URL just before fetching it, including
    URLs queued earlier and redirect targets. A URL passes only if it is on one of:
    - the allow list: the seeds' own hosts, plus hosts you add with `polymath learn <url>`;
    - the site of an approved feed (an article page on, say, `nature.com` from Nature's feed; its links are not
      followed).

    A link or redirect to any other site is never fetched. There is no web search, and the crawler cannot
    wander off to sites nobody chose.
  - **Open-web learning** (`senses.open_web`, on by default) adds sites that passed a vetting gate. The details
    are below.

## Open-web learning: every new site is vetted first

New sites come only from links that **Wikipedia articles cite**. Every article read counts the sites it links
to (`site_citations`), skipping archives, identifier resolvers and social media. A site cited by many articles
has been vetted by Wikipedia's editors.

**The gate** (`web.vet`) runs before a site's first visit, in this order:

| check | how | if it fails |
|---|---|---|
| safety lists loaded | until every list is downloaded, **nothing is vetted** (fail closed) | waits |
| not on a safety list | the site and every parent domain, against about 5 million domains: StevenBlack (fake news, gambling, porn, malware), URLhaus (malware), Université Toulouse 1 (adult, phishing, malware, gambling). Matched offline as 64-bit hashes; about 90 MB | refused, never contacted |
| name looks safe | words like porn, xxx, casino, escort; whole-word sex, bet, torrent… | refused, never contacted |
| cited enough | at least `open_web_min_citations` (3) Wikipedia articles | not considered |
| HTTPS with a valid certificate | one request for robots.txt over TLS. Python verifies the certificate chain and host name. Public addresses only. A redirect to another site fails | refused |
| robots.txt allows it | on the cited pages | refused |

Sites cited by at least `open_web_trusted_citations` (50) articles are approved. The rest start on
**probation**: at most `open_web_probation_pages` (20) pages are read, then `web.trust` compares the facts read
there with Wikidata's.
- If at least 10 facts can be checked and most contradict Wikidata, the site is dropped, and its queued pages
  are skipped.
- If most agree, it is approved.

Single-valued relations are judged from Wikidata alone, so a site's own claims cannot hide its contradictions.
The lists refresh weekly. At most `open_web_sites_per_day` (200) new sites are vetted per day.

Measured with the real lists and real sites:
- nasa.gov and nih.gov approved; smithsonianmag.com on probation;
- pornhub.com refused (UT1 adult) and bet365.com refused (UT1 gambling), without being contacted;
- self-signed, expired, wrong-host and untrusted-root certificates (badssl.com test sites) refused.

`polymath sites` lists every verdict and its reason; `polymath sites --check example.org` shows how the gate sees
a site.
- **Feeds** are polled at most hourly, with conditional requests (ETag / Last-Modified). A feed is checked
  against its host's robots.txt before polling.
- **Dumps** are downloaded with resumable range requests. Once a file has been read into the database it is
  deleted. Dump reading itself is range-based (multistream streams, bz2 blocks), so the agent never needs a
  whole 100 GB file on disk.
- **Network safety.** Requests are refused to private, loopback and link-local addresses, after DNS resolution
  and pinned at connect time. Redirects are limited and re-checked, and response sizes are capped.
- **XML** is parsed with entity declarations rejected, which blocks billion-laughs and XXE.

## What is *not* used

- No external AI service: no Claude, OpenAI, Hugging Face, search engines or knowledge APIs.
- No pretrained models, embeddings or word lists, apart from the 116-word function-word list in the HTML
  boilerplate filter (see ALGORITHMS).
- No frameworks: the runtime is the standard library and numpy. `tests/test_purity.py` fails on any other
  runtime import.
