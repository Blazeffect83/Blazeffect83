"""Deep research campaigns: discovery, relevance, snowballing, link following, budgets,
pause/resume, crash recovery, fairness, reporting, model use and API/dashboard wiring."""
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.agent import state_machine as sm
from app.agent.objectives import ObjectiveManager
from app.models.base import BudgetExceeded
from app.research.campaign import Campaign, stem
from app.research.extractor import extract_deterministic
from app.research.parser import parse_html
from app.scheduler.worker import Worker, reconcile
from app.services import build_services

TOPIC = "Thermal throttling and cooling of the Raspberry Pi 5"

WIKI_PI = """<html><head><title>Raspberry Pi 5 thermal management - Wikipedia</title></head><body>
<div class="mw-body"><h1>Raspberry Pi 5 thermal management</h1>
<p>The Raspberry Pi 5 uses the BCM2712 processor, which produces more heat than earlier models under sustained load.
If the CPU temperature exceeds 85 °C, thermal throttling reduces the clock speed of the Raspberry Pi 5 automatically.
The official active cooler for the Raspberry Pi 5 combines a heatsink with a temperature controlled fan for cooling.
Passive cooling with a large heatsink delays thermal throttling on the Raspberry Pi 5 but does not always prevent it.
Benchmarks show that the Raspberry Pi 5 without cooling reaches thermal throttling within a few minutes of full load.</p>
<p>See the <a href="https://docs.example.org/pi5-thermal">official Raspberry Pi 5 thermal throttling guide</a> and
the unrelated <a href="https://en.wikipedia.org/wiki/Diesel_engine">diesel engine</a> article.</p>
</div>
<div id="comments"><p>I think my Raspberry Pi 5 cooling fan is too loud, does anyone know a fix?</p></div>
<div class="reflist"><ol class="references"><li><a href="https://docs.example.org/pi5-cooling-study">Raspberry Pi 5
cooling and thermal throttling study</a> 2025.</li></ol></div>
</body></html>"""

DOCS = """<html><head><title>Raspberry Pi 5 thermal throttling guide</title></head><body><main>
<h1>Raspberry Pi 5 thermal throttling guide</h1>
<p>The Raspberry Pi 5 begins thermal throttling at 85 °C and reduces the clock speed until the temperature drops.
Active cooling keeps the Raspberry Pi 5 below the thermal throttling threshold during sustained benchmarks.
Without cooling, the Raspberry Pi 5 reaches the thermal throttling point after roughly three minutes of load.
The case fan of the Raspberry Pi 5 is controlled by firmware based on the measured temperature of the processor.
Thermal throttling on the Raspberry Pi 5 is reported by the vcgencmd get_throttled command for monitoring.</p>
</main></body></html>"""

STUDY = """<html><head><title>Raspberry Pi 5 cooling study</title></head><body><article>
<h1>Raspberry Pi 5 cooling and thermal throttling study</h1>
<p>Our measurements show the Raspberry Pi 5 begins thermal throttling at 80 °C and reduces the clock speed until the
temperature drops. Passive cooling extended the time before thermal throttling on the Raspberry Pi 5 by four minutes.
Active cooling with the official fan eliminated thermal throttling on the Raspberry Pi 5 in every benchmark we ran.
The cooling results for the Raspberry Pi 5 were repeated three times with consistent thermal measurements overall.
Ambient temperature strongly influenced how quickly the Raspberry Pi 5 reached thermal throttling under load.</p>
</article></body></html>"""

DIESEL = """<html><head><title>Diesel engine - Wikipedia</title></head><body><p>""" + " ".join(
    ["The diesel engine controls torque by changing the fuel quantity instead of throttling the intake air."] * 8
) + """ Engine cooling systems remove heat from the cylinder block using a liquid coolant loop.</p></body></html>"""

ABSTRACT_ON = ("We study thermal throttling on the Raspberry Pi 5 under sustained neural network inference. "
               "Active cooling with a fan prevented thermal throttling in all runs, while passive cooling with a "
               "heatsink delayed thermal throttling by several minutes. The Raspberry Pi 5 reached 82 degrees "
               "Celsius without cooling. We report throughput, temperature traces and power for each cooling "
               "configuration and discuss implications for edge deployments of the Raspberry Pi 5.")
ABSTRACT_CITING = ("Building on prior measurements of thermal throttling on the Raspberry Pi 5, we evaluate "
                   "low-cost cooling options. A copper heatsink with a small fan kept the Raspberry Pi 5 below the "
                   "thermal throttling threshold, and undervolting reduced temperatures by six degrees. We compare "
                   "cooling options against throughput and noise and recommend active cooling for sustained "
                   "workloads on the Raspberry Pi 5 in enclosures with poor airflow and high ambient temperature.")
ABSTRACT_OFF = ("We investigate fuel injection timing in marine diesel engines and its effect on emissions. " * 6)


def _inverted(text):
    inv = {}
    for i, w in enumerate(text.split()):
        inv.setdefault(w, []).append(i)
    return inv


def _work(wid, doi, title, abstract):
    return {"id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/{doi}", "title": title,
            "publication_year": 2025, "publication_date": "2025-03-01", "cited_by_count": 3,
            "authorships": [{"author": {"display_name": "A. Researcher"}}], "type": "article",
            "primary_location": {"source": {"display_name": "Journal of Edge Systems"}},
            "abstract_inverted_index": _inverted(abstract)}


@pytest.fixture
def world(web):
    def wikimedia(request):
        return httpx.Response(200, json={"pages": [
            {"key": "Raspberry_Pi_5_thermal_management", "title": "Raspberry Pi 5 thermal management",
             "excerpt": "thermal throttling and cooling of the Raspberry Pi 5"},
            {"key": "Diesel_engine", "title": "Diesel engine", "excerpt": "throttling the intake air"}]})

    def openalex(request):
        q = parse_qs(urlsplit(str(request.url)).query)
        if q.get("filter") == ["cites:W1"]:
            return httpx.Response(200, json={"results": [
                _work("W2", "10.2000/citing", "Low-cost cooling options for the Raspberry Pi 5", ABSTRACT_CITING)]})
        if q.get("filter"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"results": [
            _work("W1", "10.1000/pi5thermal", "Thermal throttling of the Raspberry Pi 5 under inference", ABSTRACT_ON),
            _work("W9", "10.9000/diesel", "Injection timing in marine diesel engines", ABSTRACT_OFF)]})

    web.route("https://api.wikimedia.org/core/v1/wikipedia/en/search/page", wikimedia)
    web.route("https://api.openalex.org/works", openalex)
    web.add("https://en.wikipedia.org/wiki/Raspberry_Pi_5_thermal_management", WIKI_PI)
    web.add("https://en.wikipedia.org/wiki/Diesel_engine", DIESEL)
    web.add("https://docs.example.org/pi5-thermal", DOCS)
    web.add("https://docs.example.org/pi5-cooling-study", STUDY)
    return web


def start(services, **params):
    base = {"hours": 1.0, "max_documents": 20, "slice_seconds": 30}
    base.update(params)
    return ObjectiveManager(services).create(TOPIC, kind="deep_research", research_params=base)


def run_until_done(services, oid, limit=60):
    for _ in range(limit):
        st = services.controller.run(oid)
        if st in (sm.COMPLETED, sm.FAILED, sm.PAUSED, sm.CANCELLED):
            return st
    return services.state.objective_status(oid)


def state(services, oid):
    return Campaign(services).load(services.db.one("SELECT * FROM objectives WHERE id = ?", (oid,)))["state"]


# -- building blocks -------------------------------------------------------------
def test_concepts_and_relevance_tiers():
    c = Campaign.__new__(Campaign)
    st = {"topic": TOPIC}
    assert c.concepts(st) == [{"thermal", "throttling"}, {"cooling"}, {"raspberry", "pi"}]
    assert c.is_core(st, "A cooled Raspberry Pi 5 ran without thermal throttling")
    assert c.is_core(st, "The Raspberry Pi 5 throttles at 85C without a cooler")
    assert not c.is_core(st, "Phones use throttling with little active cooling") and \
        c.has_focus(st, "Phones use throttling with little active cooling")
    for irrelevant in ("Instead of throttling the intake air the diesel engine", "The Raspberry Pi 500 is based on the Pi 5",
                       "A raspberry-red compound turns blue on cooling, a thermal effect"):
        assert not c.has_focus(st, irrelevant), irrelevant


def test_stemmer():
    assert {stem(w) for w in ("cooling", "cooled", "cooler")} == {"cool"}
    assert stem("throttles") == stem("throttling") == "throttl"


def test_extraction_ranks_rare_query_terms_and_list_items():
    text = ("The Raspberry Pi is a computer. " * 30 + "If the CPU temperature exceeds 85 °C, performance is throttled "
            "automatically. For sustained workloads, additional cooling may be required.")
    e = extract_deterministic("Raspberry Pi", text, ["Active cooler (2023) – a heatsink and fan for thermal management "
                                                     "on Pi 5"], TOPIC)
    top = [c.text for c in e.claims[:3]]
    assert any("throttled" in t for t in top) and any("Active cooler" in t for t in top)


def test_parser_keeps_reference_links_but_drops_comments():
    d = parse_html(WIKI_PI, "https://en.wikipedia.org/wiki/X")
    assert "https://docs.example.org/pi5-cooling-study" in d.links
    assert d.anchors["https://docs.example.org/pi5-cooling-study"].startswith("Raspberry Pi 5")
    assert "fan is too loud" not in d.text and "2025." not in d.text


# -- end to end -----------------------------------------------------------------------
def test_campaign_end_to_end(services, world):
    oid = start(services)
    assert run_until_done(services, oid) == sm.COMPLETED
    o = ObjectiveManager(services).get(oid)
    report = o["completion_summary"]
    camp = o["campaign"]
    s = camp["stats"]
    assert s["queries_run"] >= 3 and s["links_followed"] >= 1 and s.get("snowballs", 0) >= 1
    assert s.get("off_topic", 0) + s.get("prefiltered", 0) >= 1
    # Snowballing actually ran and citation links were followed.
    assert any("cites%3AW1" in u or "cites:W1" in u for u in world.requests)
    assert "https://docs.example.org/pi5-cooling-study" in world.requests
    # Report structure and honest citations.
    for section in ("# Research report:", "## Answers by sub-question", "## Key findings", "## Method", "## Sources"):
        assert section in report
    sources = dict(re.findall(r"^(\d+)\. (.+)$", report, re.M))
    cited = {n for n in re.findall(r"\[(\d+)\]", report)}
    assert cited and cited <= set(sources), "every citation must point at a listed source"
    assert "doi.org/10.1000/pi5thermal" in report and "docs.example.org" in report
    assert "Diesel engine" not in report and "marine diesel" not in report  # off-topic never cited
    assert "fan is too loud" not in report  # comment section never becomes evidence
    # The 80 °C vs 85 °C disagreement between sources is surfaced, not silently resolved.
    assert "## Contested points" in report and "80" in report and "85" in report
    assert (services.settings.workspace / oid / "report.md").read_text() == report
    assert services.db.one("SELECT * FROM audit_events WHERE event = 'research.campaign_started'")
    # Every cited document is stored with evidence passages in the knowledge base.
    docs = services.db.query("SELECT id FROM documents")
    assert len(docs) >= 4


def test_document_limit_counts_on_topic_documents(services, world):
    oid = start(services, max_documents=2)
    assert run_until_done(services, oid) == sm.COMPLETED
    st = state(services, oid)
    assert len(st["doc_ids"]) == 2 and "document limit" in st["stop_reason"]


def test_time_budget_finishes_not_fails(services, world):
    oid = start(services, hours=0.5)
    services.controller.run(oid, max_iterations=1)  # initialise
    o = services.db.one("SELECT * FROM objectives WHERE id = ?", (oid,))
    cp = Campaign(services).load(o)
    cp["state"]["active_seconds"] = 0.5 * 3600
    Campaign(services).save(oid, cp)
    assert run_until_done(services, oid) == sm.COMPLETED
    assert "time budget" in state(services, oid)["stop_reason"]


def test_time_slices_yield_and_resume_without_refetching(services, world):
    oid = start(services, slice_seconds=0)
    services.controller.run(oid)  # QUEUED → initialised → one unit of work → yields
    assert services.state.objective_status(oid) == sm.READY
    assert state(services, oid)["stats"]["queries_run"] == 1
    for _ in range(3):
        services.controller.run(oid)
    m = ObjectiveManager(services)
    m.pause(oid)
    before = len(world.requests)
    assert services.controller.run(oid) == sm.PAUSED and len(world.requests) == before
    m.resume(oid)
    assert services.state.objective_status(oid) == sm.READY
    services.db.execute("UPDATE objectives SET checkpoint = json_set(checkpoint, '$.state.params.slice_seconds', 30) "
                        "WHERE id = ?", (oid,))
    assert run_until_done(services, oid) == sm.COMPLETED
    fetched = [u for u in world.requests if u == "https://docs.example.org/pi5-thermal"]
    assert len(fetched) == 1  # never fetched twice across slices / pause / resume


def test_campaign_survives_restart(services, world, settings, mock_model):
    oid = start(services, slice_seconds=0)
    for _ in range(4):
        services.controller.run(oid)
    services.db.execute("UPDATE objectives SET status = 'RUNNING' WHERE id = ?", (oid,))  # simulate crash mid-slice
    services.db.close()
    s2 = build_services(settings, providers={"mock": mock_model}, http_transport=world.transport,
                        fetch_sleep=lambda x: None)
    reconcile(s2)
    assert s2.state.objective_status(oid) == sm.READY
    s2.db.execute("UPDATE objectives SET checkpoint = json_set(checkpoint, '$.state.params.slice_seconds', 30) "
                  "WHERE id = ?", (oid,))
    assert run_until_done(s2, oid) == sm.COMPLETED
    urls = [r["final_url"] for r in s2.db.query("SELECT final_url FROM documents")]
    assert len(urls) == len(set(urls))
    s2.db.close()


def test_long_campaign_does_not_starve_other_objectives(services, world, mock_model):
    deep = start(services, slice_seconds=0)

    def by_schema(messages, system, schema):  # research questions vs. an objective plan
        if schema and "subquestions" in schema.get("properties", {}):
            return {"subquestions": [], "search_queries": []}
        return {"steps": [{"title": "w", "tool": "file_write", "args": {"path": "a", "content": "1"}}]}
    mock_model.push(*[by_schema] * 50)
    services.db.execute("UPDATE objectives SET priority = 5 WHERE id = ?", (deep,))
    other = ObjectiveManager(services).create("Write a small file while research runs", priority=5)
    w = Worker(services)
    for _ in range(4):
        w.tick(wait=True)
    assert services.state.objective_status(other) == sm.COMPLETED
    st = state(services, deep)
    assert st["stats"]["queries_run"] + st["stats"]["examined"] >= 2  # research progressed too (interleaved)
    w.stop()


def test_allowed_domains_respected(services, world):
    oid = start(services, allowed_domains=["wikipedia.org"], sources=["wikipedia", "links"])
    assert run_until_done(services, oid) == sm.COMPLETED
    assert not any(u.startswith("https://docs.example.org") for u in world.requests)


def test_resource_pressure_pauses_slice(services, world):
    oid = start(services)
    services.controller.run(oid, max_iterations=1)  # initialise
    services.db.kv_set("resource_pause", ["temperature"])
    services.controller.run(oid)
    assert services.state.objective_status(oid) == sm.READY
    assert state(services, oid)["stats"]["examined"] == 0
    services.db.kv_set("resource_pause", None)


def test_saturation_stops_with_diminishing_returns(services, world):
    camp = Campaign(services)
    oid = start(services, sources=["wikipedia"])
    services.controller.run(oid, max_iterations=1)
    o = services.db.one("SELECT * FROM objectives WHERE id = ?", (oid,))
    cp = camp.load(o)
    st = cp["state"]
    st["yields"] = [0] * 20
    st["saturation_strikes"] = 2
    st["queries"], st["frontier"] = [], []
    st["refreshes"] = camp.MAX_REFRESHES  # nothing new can be generated
    camp.save(oid, cp)
    assert run_until_done(services, oid) == sm.COMPLETED
    assert "diminishing returns" in state(services, oid)["stop_reason"] or \
        "exhausted" in state(services, oid)["stop_reason"]


def test_model_generates_questions_and_cited_synthesis(services, world, mock_model):
    def questions(messages, system, schema):
        assert "<untrusted_document" in messages[0].content  # findings are passed as untrusted data
        return {"subquestions": ["How effective is active cooling on the Raspberry Pi 5?"],
                "search_queries": ["raspberry pi 5 active cooler benchmark"]}
    mock_model.push(questions)
    # Any further question rounds: nothing new. Final synthesis cites a real and a non-existent source.
    for _ in range(30):
        mock_model.push({"subquestions": [], "search_queries": []})
    oid = start(services, max_documents=4)
    services.controller.run(oid, max_iterations=1)
    st = state(services, oid)
    assert "How effective is active cooling on the Raspberry Pi 5?" in st["subquestions"]
    assert any(q["q"] == "raspberry pi 5 active cooler benchmark" for q in st["queries"])
    mock_model.script.clear()
    mock_model.push(*[{"subquestions": [], "search_queries": []}] * 30)
    original = services.router.generate

    def generate(role, *a, **k):
        if role == "summary":
            from app.models.base import ModelResponse
            return ModelResponse(text="", provider="mock", model="m",
                                 data={"summary": "Active cooling prevents throttling [1]; see also [99]."})
        return original(role, *a, **k)
    services.router.generate = generate
    assert run_until_done(services, oid) == sm.COMPLETED
    report = services.db.scalar("SELECT completion_summary FROM objectives WHERE id = ?", (oid,))
    assert "## Executive summary" in report and "[1]" in report and "[99]" not in report


def test_model_budget_exhaustion_falls_back_to_deterministic(services, world, mock_model):
    mock_model.push(BudgetExceeded("daily limit"))
    oid = start(services, max_documents=3)
    assert run_until_done(services, oid) == sm.COMPLETED
    assert state(services, oid)["model_disabled"] is True


def test_injection_page_in_discovery_is_inert(services, world):
    from tests.conftest import load_fixture
    world.add("https://en.wikipedia.org/wiki/Raspberry_Pi_5_thermal_management",
              load_fixture("injection.html").replace("Linux tips", "Raspberry Pi 5 thermal throttling cooling"))
    oid = start(services)
    assert run_until_done(services, oid) == sm.COMPLETED
    assert services.db.scalar("SELECT COUNT(*) FROM tool_runs") == 0
    assert services.db.scalar("SELECT COUNT(*) FROM approvals") == 0
    assert not any("attacker" in u for u in world.requests)


def test_brave_web_search_provider(services, world):
    services.settings.brave_search_api_key = "brave-test-key-123456"
    seen = {}

    def brave(request):
        seen["key"] = request.headers.get("x-subscription-token")
        return httpx.Response(200, json={"web": {"results": [
            {"url": "https://docs.example.org/pi5-thermal", "title": "Raspberry Pi 5 thermal throttling guide",
             "description": "cooling and thermal throttling of the Raspberry Pi 5"}]}})
    world.route("https://api.search.brave.com/res/v1/web/search", brave)
    oid = start(services, sources=["web"])
    assert run_until_done(services, oid) == sm.COMPLETED
    assert seen["key"] == "brave-test-key-123456"
    assert "https://docs.example.org/pi5-thermal" in world.requests


def test_api_and_dashboard(services, world):
    from fastapi.testclient import TestClient

    from app.api.authentication import hash_password, hash_token
    from app.main import create_app
    services.settings.admin_password_hash = hash_password("correct horse battery")
    services.settings.api_token_hash = hash_token("tok_deep_research_test")
    client = TestClient(create_app(services=services, start_worker=False))
    h = {"authorization": "Bearer tok_deep_research_test"}
    assert client.post("/api/research/deep", headers=h, json={"topic": TOPIC, "sources": ["nope"]}).status_code == 422
    r = client.post("/api/research/deep", headers=h, json={"topic": TOPIC, "hours": 0.5, "max_documents": 5})
    assert r.status_code == 201
    oid = r.json()["id"]
    interim = client.get(f"/api/objectives/{oid}/report", headers=h)
    assert interim.status_code == 200 and "Interim report" in interim.text
    detail = client.get(f"/api/objectives/{oid}", headers=h).json()
    assert detail["campaign"]["topic"] == TOPIC and "stats" in detail["campaign"]
    run_until_done(services, oid)
    assert "Final report" in client.get(f"/api/objectives/{oid}/report", headers=h).text
    # Dashboard: form submission + progress page.
    client.post("/login", data={"username": "admin", "password": "correct horse battery"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/research").text).group(1)
    r = client.post("/research/deep", data={"topic": "Effects of intermittent fasting on insulin sensitivity",
                                            "hours": "2", "max_documents": "50", "csrf": csrf},
                    follow_redirects=False)
    assert r.status_code == 303 and "/tasks/obj_" in r.headers["location"]
    page = client.get(r.headers["location"].split("?")[0]).text
    assert "Deep research" in page and "intermittent fasting" in page
    page = client.get(f"/tasks/{oid}").text
    assert "<h2" in page and "Key findings" in page  # rendered Markdown report
    assert json.loads(json.dumps(detail))  # serialisable


def test_study_aims_are_not_findings_and_labels_are_stripped():
    t = ("OBJECTIVE: This study aimed to compare intermittent fasting versus continuous restriction on insulin "
         "sensitivity in adults. RESULTS: Intermittent fasting improved insulin sensitivity by 20% compared with "
         "continuous restriction in obese adults. CONCLUSION Intermittent fasting may improve insulin sensitivity "
         "and glucose metabolism in metabolically high-risk adults. ") * 2
    texts = [c.text for c in extract_deterministic("t", t, [], "intermittent fasting insulin sensitivity").claims]
    assert texts and not any("aimed" in x or x.startswith(("OBJECTIVE", "RESULTS", "CONCLUSION")) for x in texts)
    assert any(x.startswith("Intermittent fasting improved insulin sensitivity by 20%") for x in texts)


def test_similar_statements_reported_as_consensus_not_corroboration(services, world):
    oid = start(services)
    assert run_until_done(services, oid) == sm.COMPLETED
    report = services.db.scalar("SELECT completion_summary FROM objectives WHERE id = ?", (oid,))
    # The docs page and the study both say throttling starts at a temperature and reduces the clock speed.
    assert "similar statements in" in report
    # ...but corroboration counts only exact/near-identical evidence, never paraphrases.
    assert services.db.scalar("SELECT MAX(corroboration) FROM claims") <= 3
