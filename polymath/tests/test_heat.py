"""Heat-aware scheduling (body.heat): it learns what warms the Pi, and holds that back while it is warm."""

from __future__ import annotations

import time
from typing import Any

from polymath.body.heat import HeatModel
from polymath.body.sensors import Vitals
from polymath.core.app import build_agent
from polymath.core.loop import BodyState, Decision


def learn(m: HeatModel, kind: str, rate: float, n: int = 6, start: float = 60.0) -> None:
    t, temp = 0.0, start
    m.step("", temp, t)
    for _ in range(n):
        m.started(kind)
        t += 60.0
        temp += rate
        m.step("", temp, t)


def test_it_learns_which_kinds_heat_and_holds_the_hottest_back_only_while_warm(db):
    m = HeatModel(db)
    learn(m, "wikipedia.part", 1.5)
    learn(m, "reason.infer", 0.8)
    learn(m, "feeds.ingest", -0.2)
    learn(m, "body.backup", 3.0)  # maintenance is never held back
    assert abs(m.kinds["wikipedia.part"][0] - 1.5) < 1e-9 and m.kinds["wikipedia.part"][1] == 6
    assert m.hot_kinds(60.0, 75.0) == set()  # cool: everything runs
    assert m.hot_kinds(70.0, 75.0) == {"wikipedia.part"}  # within 6 °C of the throttle point: the hottest third
    assert m.hot_kinds(None, 75.0) == set()
    m.save(force=True)
    again = HeatModel(db)
    assert again.kinds["wikipedia.part"][1] == 6 and again.hours  # kept in the brain
    m.step("x", None, 0.0)  # no sensor: nothing learned, nothing held back
    assert m._last is None


def test_the_warmest_hours_widen_the_margin(db):
    m = HeatModel(db, clock=lambda: time.mktime((2026, 10, 26, 15, 0, 0, 0, 0, -1)))
    m.hours = {str(h): 50.0 + (10.0 if h in {14, 15, 16, 17} else 0.0) for h in range(24)}
    learn(m, "wikipedia.part", 1.0)
    assert m.warm_hours() == {14, 15, 16, 17} and m.hot_kinds(66.0, 75.0) == {"wikipedia.part"}
    m.clock = lambda: time.mktime((2026, 10, 26, 3, 0, 0, 0, 0, -1))
    assert m.hot_kinds(66.0, 75.0) == set()


def test_the_loop_learns_and_filters(config, monkeypatch):
    agent = build_agent(config, planners=False)
    agent.start()
    agent.heat.kinds = {"t.hot": [2.0, 9.0], "t.cool": [-0.5, 9.0], "t.mild": [0.1, 9.0]}
    d = agent._heat_filter(Decision(["t.hot", "t.cool"], reason="r"), 72.0)
    assert d.kinds == ["t.cool"] and "the hottest work waits" in d.reason
    assert agent._heat_filter(Decision([]), 72.0).kinds == []
    assert agent._heat_filter(Decision(["t.hot"]), 40.0).kinds == ["t.hot"]
    warm = BodyState(vitals=Vitals(72.0, 0.5, 4, 1000.0, 500.0, 10.0))
    monkeypatch.setattr(agent.body, "observe", lambda: warm)
    agent.cycle()
    assert agent.heat._last is not None and agent.heat._last[1] == 72.0

    def boom(*_a: Any) -> None:
        raise RuntimeError("broken")

    monkeypatch.setattr(agent.heat, "step", boom)
    agent.cycle()  # a broken heat model never stops the loop
    agent.shutdown()
    agent.db.close()
