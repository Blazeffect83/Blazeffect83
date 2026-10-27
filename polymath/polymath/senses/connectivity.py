"""Finding out when the internet is back (job ``net.probe``).

While ``core.netstate`` says offline, the planner queues a probe: one HEAD request to a server Polymath reads
from anyway (never anywhere new). The first answer brings the agent back online, and the held-back network jobs
run again. Probes are spaced out the longer the outage lasts (every minute at first, then up to every 10
minutes), so an unplugged Pi does not spend its time knocking.
"""

from __future__ import annotations

import time
from typing import Any

from polymath.core import netstate
from polymath.core.jobs import JobContext, JobOutcome
from polymath.senses.net import HttpClient, reachable

# servers Polymath already reads from; the first that answers is enough
PROBE_URLS = ("https://dumps.wikimedia.org/", "https://www.wikidata.org/", "https://en.wikipedia.org/")
FIRST_PROBE_S = 60.0
MAX_PROBE_S = 600.0


def probe_interval(probes: int) -> float:
    """Seconds before probe number ``probes``: 1, 2, 4, 8, then every 10 minutes."""
    return float(min(MAX_PROBE_S, FIRST_PROBE_S * 2 ** max(0, probes - 1)))


def probe_job(ctx: JobContext) -> JobOutcome:
    """Job ``net.probe``: ask one known server whether the internet is back."""
    client: HttpClient = ctx.services["http"]
    now = time.time()
    if netstate.get(ctx.db).online:
        return JobOutcome(done=True, result={"online": True, "skipped": "already online"})
    why = ""
    for url in PROBE_URLS:
        ok, why = reachable(client, url)
        if ok:
            netstate.succeeded(ctx.db, now)
            return JobOutcome(done=True, value=0.1, result={"online": True, "via": url})
        ctx.tick()
    netstate.probed(ctx.db, now)
    return JobOutcome(done=True, result={"online": False, "reason": why[:160]})


def next_probe_at(st: netstate.NetState) -> float:
    """When the next probe is due: one interval after going offline or after the last probe."""
    return max(st.since, st.last_probe) + probe_interval(st.probes + 1)


def planner(agent: Any) -> None:
    """While offline: queue the next probe when it is due."""
    st = netstate.get(agent.db)
    now = time.time()
    if not st.online and now >= next_probe_at(st):
        agent.scheduler.enqueue("net.probe", {}, key=f"net.probe@{int(now // FIRST_PROBE_S)}", priority=3.5)
