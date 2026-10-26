"""Drive health from SMART (body.health): what counts as failing, through a USB bridge too."""

from __future__ import annotations

import json
from typing import Any

from polymath.body import health as hl


def ata(passed: bool = True, **raw: int) -> dict[str, Any]:
    ids = {"reallocated": 5, "reported_uncorrect": 187, "pending": 197, "uncorrectable": 198}
    table = [{"id": ids[k], "value": 100, "raw": {"value": v}} for k, v in raw.items() if k in ids]
    if "life_left" in raw:
        table.append({"id": 231, "value": raw["life_left"], "raw": {"value": 0}})
    return {"device": {"name": "/dev/sda"}, "smart_status": {"passed": passed},
            "ata_smart_attributes": {"table": table}, "temperature": {"current": raw.get("temp", 35)}}  # fmt: skip


def runner(answers: list[tuple[int, dict[str, Any] | str]]) -> Any:
    calls: list[list[str]] = []

    def run(args: list[str]) -> tuple[int, str]:
        calls.append(args)
        code, out = answers.pop(0)
        return code, out if isinstance(out, str) else json.dumps(out)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_a_usb_bridge_is_asked_through_and_none_is_unknown():
    run = runner([(2, {"smartctl": {"messages": [{"string": "Unknown USB bridge"}]}}), (0, ata())])
    report = hl.read_report("sda", run)
    assert report is not None and report["device"]["name"] == "/dev/sda"
    assert run.calls[1] == ["smartctl", "-j", "-a", "-d", "sat", "/dev/sda"]
    assert hl.read_report("sda", runner([(2, ""), (1, "not json")])) is None
    out = hl.check("sda", None, 1000.0, runner([(2, ""), (2, "")]))
    assert out["status"] == "unknown" and "smartctl missing" in out["why"]
    assert hl.run_smartctl(["/nonexistent-smartctl-binary"])[0] == 255


def test_what_failing_means():
    assert hl.verdict(hl.counts_of(ata(passed=False)), None)[0] == "failing"
    base = hl.counts_of(ata(reallocated=2, pending=0))
    assert hl.verdict(base, None) == ("watch", "2 remapped and 0 waiting sectors (not growing)")
    status, why = hl.verdict(hl.counts_of(ata(reallocated=2, pending=8)), base)
    assert status == "failing" and why == "sectors waiting to be remapped went from 0 to 8"
    assert hl.verdict(hl.counts_of(ata(reallocated=14)), base, days=3)[0] == "failing"  # 12 more in 3 days
    assert hl.verdict(hl.counts_of(ata(reallocated=14)), base, days=30)[0] == "watch"
    assert hl.verdict(hl.counts_of(ata(life_left=5)), None) == ("watch", "95 % of its write life used")
    assert hl.verdict(hl.counts_of(ata(temp=63)), None) == ("watch", "it runs at 63 °C")
    assert hl.verdict(hl.counts_of(ata()), None) == ("ok", "")


def test_nvme_reports():
    log: dict[str, Any] = {"critical_warning": 0, "media_errors": 0, "percentage_used": 3, "available_spare": 100,
                           "available_spare_threshold": 10}  # fmt: skip
    nvme = {
        "device": {"name": "/dev/nvme0"},
        "smart_status": {"passed": True},
        "nvme_smart_health_information_log": log,
    }
    c = hl.counts_of(nvme)
    assert c["wear_pct"] == 3 and not c["spare_low"] and hl.verdict(c, None)[0] == "ok"
    log["critical_warning"] = 4
    assert hl.verdict(hl.counts_of(nvme), None)[0] == "failing"
    log.update(critical_warning=0, available_spare=5)
    assert "spare blocks" in hl.verdict(hl.counts_of(nvme), None)[1]


def test_failing_stays_failing_until_the_drive_is_replaced():
    first = hl.check("sda", None, 0.0, runner([(0, ata(pending=0))]))
    assert first["status"] == "ok"
    second = hl.check("sda", first, 86400.0, runner([(0, ata(pending=8))]))
    assert second["status"] == "failing" and second["counts"]["pending"] == 0  # measured against before
    third = hl.check("sda", second, 2 * 86400.0, runner([(0, ata(pending=8))]))
    assert third["status"] == "failing"  # not growing any more, but still worse than when it was healthy
