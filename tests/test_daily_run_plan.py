"""Run plan for the daily deals report: 08:00 edition, 09:30 / 10:30 re-checks."""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from reports import daily_deals_report as r

IST = timezone(timedelta(hours=5, minutes=30))
TUE = date(2026, 9, 29)
MON_REPORT = date(2026, 9, 25)  # Friday's session


def at(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


@pytest.mark.parametrize("hh,mm,slot", [
    (8, 0, None), (9, 19, None), (9, 20, "0930"), (9, 34, "0930"),
    (10, 19, "0930"), (10, 20, "1030"), (10, 33, "1030"), (12, 29, "1030"),
    (12, 30, None), (17, 0, None),
])
def test_check_windows(hh, mm, slot):
    assert r._check_slot_for(at(TUE, hh, mm)) == slot


def _patch(monkeypatch, *, claim: bool, row: dict | None):
    monkeypatch.setattr(r, "_claim_slot", lambda *_: claim)
    monkeypatch.setattr(r, "_slot_row", lambda *_: row)


def test_first_run_sends(monkeypatch):
    _patch(monkeypatch, claim=True, row=None)
    assert r._plan_run(date(2026, 9, 28), TUE, at(TUE, 8, 1), ["x"])[0] == "send"


def test_sent_this_morning_then_checks(monkeypatch):
    sent = at(TUE, 8, 4).astimezone(timezone.utc).isoformat()
    _patch(monkeypatch, claim=False, row={"status": "sent", "sent_at": sent})
    assert r._plan_run(date(2026, 9, 28), TUE, at(TUE, 9, 32), ["x"])[0] == "check:0930"
    assert r._plan_run(date(2026, 9, 28), TUE, at(TUE, 10, 31), ["x"])[0] == "check:1030"
    # duplicate 08:00 trigger after the send, and a GHA schedule delayed to the afternoon
    assert r._plan_run(date(2026, 9, 28), TUE, at(TUE, 8, 20), ["x"])[0] == "silent"
    assert r._plan_run(date(2026, 9, 28), TUE, at(TUE, 15, 0), ["x"])[0] == "silent"


def test_monday_is_idle(monkeypatch):
    mon = date(2026, 9, 28)
    sat_send = at(date(2026, 9, 26), 8, 3).astimezone(timezone.utc).isoformat()
    _patch(monkeypatch, claim=False, row={"status": "sent", "sent_at": sat_send})
    for hh, mm in ((8, 1), (9, 31), (10, 31)):
        assert r._plan_run(MON_REPORT, mon, at(mon, hh, mm), ["x"])[0] == "idle"


def test_in_flight_is_silent(monkeypatch):
    _patch(monkeypatch, claim=False, row={"status": "pending"})
    assert r._plan_run(date(2026, 9, 28), TUE, at(TUE, 9, 31), ["x"])[0] == "silent"


def _frames(short_rows):
    bulk = pd.DataFrame([{"symbol": "ABC", "client_name": "Fund A", "buy_sell": "BUY",
                          "quantity": 100000, "avg_price": 12.5}])
    return {"bulk": bulk, "block": pd.DataFrame(), "short": pd.DataFrame(short_rows)}


def test_snapshot_diff_finds_only_new_rows():
    before = _frames([{"symbol": "XYZ", "quantity": 5000}])
    base = r._snapshot(before)
    after = _frames([{"symbol": "XYZ", "quantity": 5000}, {"symbol": "LATE", "quantity": 900}])
    counts, syms = r._new_summary(r._new_since(after, base))
    assert counts == {"bulk": 0, "block": 0, "short": 1}
    assert syms == ["LATE"]


def test_snapshot_ignores_json_number_formatting():
    a = _frames([{"symbol": "XYZ", "quantity": 5000}])
    b = _frames([{"symbol": "xyz ", "quantity": "5000.0"}])
    b["bulk"] = b["bulk"].astype({"avg_price": object})
    b["bulk"].loc[0, "avg_price"] = "12.50"
    assert r._snapshot(a) == r._snapshot(b)


def test_revised_quantity_counts_as_new():
    base = r._snapshot(_frames([{"symbol": "XYZ", "quantity": 5000}]))
    counts, _ = r._new_summary(r._new_since(_frames([{"symbol": "XYZ", "quantity": 7000}]), base))
    assert counts["short"] == 1


def test_update_card_mentions_counts_and_names():
    card = r._update_card({"short": 3, "bulk": 1, "block": 0}, ["LATE", "M&M"], "08:04")
    assert "3 short deals" in card["body"] and "1 bulk deal" in card["body"]
    assert "M&amp;M" in card["body"] and "08:04" in card["body"]
