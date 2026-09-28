"""End-to-end morning for the daily deals report with report_log, NSE data and SMTP faked."""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from reports import daily_deals_report as r

IST = timezone(timedelta(hours=5, minutes=30))


class Clock:
    now = None


class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return Clock.now.astimezone(tz) if tz else Clock.now.replace(tzinfo=None)


@pytest.fixture
def world(monkeypatch):
    log: dict[tuple[str, str], dict] = {}
    data = {
        "bulk_deals": [{"symbol": "ABC", "security_name": "ABC Ltd", "client_name": "Fund A",
                        "buy_sell": "B", "quantity": 100000, "avg_price": 12.5,
                        "exchange": "NSE", "remarks": None, "deal_date": "2026-09-28"}],
        "block_deals": [],
        "short_deals": [{"symbol": "XYZ", "security_name": "XYZ Ltd", "quantity": 5000,
                         "exchange": "NSE", "deal_date": "2026-09-28"}],
    }
    sent, ops = [], []

    def insert(rt, d, rec):
        k = (rt, d.isoformat())
        if k in log:
            return False
        log[k] = {"status": "pending", "claimed_at": Clock.now.isoformat()}
        return True

    monkeypatch.setattr(r, "datetime", FakeDatetime)
    monkeypatch.setattr(r, "_insert_slot", insert)
    monkeypatch.setattr(r, "_slot_row", lambda rt, d: log.get((rt, d.isoformat())))
    monkeypatch.setattr(r, "_set_slot", lambda rt, d, **f: log[(rt, d.isoformat())].update(f))
    monkeypatch.setattr(r, "_fetch", lambda t, d: pd.DataFrame(data[t]))
    monkeypatch.setattr(r, "_scrape_health", lambda d: [])
    monkeypatch.setattr(r, "_latest_scrape_failures", lambda since: [])
    monkeypatch.setattr(r, "render_pdf", lambda html: b"%PDF")
    monkeypatch.setattr(r, "_send_email", lambda **kw: sent.append(kw))
    monkeypatch.setattr(r, "send_ops_log", lambda *a: ops.append(a))
    monkeypatch.setattr("utils.helpers.today_ist", lambda: Clock.now.astimezone(IST).date().isoformat())
    monkeypatch.setattr("utils.helpers.is_trading_day", lambda d: d.weekday() < 5)
    for k, v in {"SMTP_USER": "bac@x", "SMTP_PASSWORD": "p",
                 "REPORT_RECIPIENTS": "bac-reports@brindco.com"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    return log, data, sent, ops


def run(when: datetime) -> int:
    Clock.now = when
    return r.main()


def test_full_morning_then_monday(world):
    log, data, sent, ops = world
    tue = lambda h, m: datetime(2026, 9, 29, h, m, tzinfo=IST)  # noqa: E731

    assert run(tue(8, 2)) == 0
    assert len(sent) == 1 and sent[0]["recipients"] == ["bac-reports@brindco.com"]
    assert not sent[0]["subject"].startswith("[UPDATED")
    assert ops[-1][1] == "SENT"

    assert run(tue(8, 15)) == 0            # duplicate 08:00 trigger: nothing at all
    assert len(sent) == 1 and len(ops) == 1

    assert run(tue(9, 33)) == 0            # nothing new
    assert len(sent) == 1 and ops[-1][1] == "NO CHANGE"
    assert run(tue(9, 50)) == 0            # duplicate 09:30 trigger: silent
    assert len(ops) == 2

    data["short_deals"].append({"symbol": "LATE", "security_name": "Late Ltd", "quantity": 900,
                               "exchange": "NSE", "deal_date": "2026-09-28"})
    assert run(tue(10, 32)) == 0           # late short deal → updated edition
    assert len(sent) == 2 and sent[1]["subject"].startswith("[UPDATED 10:32 IST]")
    assert "LATE" in sent[1]["html"]
    assert ops[-1][1] == "UPDATED"
    assert run(tue(11, 5)) == 0            # 11:00 stray trigger: silent
    assert len(sent) == 2 and len(ops) == 3

    # Next Monday: Friday already went out on Saturday → one "not sent" log, no report.
    log[("daily_deals_email", "2026-10-02")] = {
        "status": "sent", "sent_at": datetime(2026, 10, 3, 2, 33, tzinfo=timezone.utc).isoformat()}
    mon = lambda h, m: datetime(2026, 10, 5, h, m, tzinfo=IST)  # noqa: E731
    for h, m in ((8, 1), (9, 31), (10, 31)):
        assert run(mon(h, m)) == 0
    assert len(sent) == 2
    assert [o[1] for o in ops[3:]] == ["NOT SENT"]


def test_failed_edition_is_logged_and_retried(world, monkeypatch):
    log, data, sent, ops = world
    boom = {"on": True}

    def send(**kw):
        if boom["on"]:
            raise OSError("smtp down")
        sent.append(kw)

    monkeypatch.setattr(r, "_send_email", send)
    assert run(datetime(2026, 9, 29, 8, 2, tzinfo=IST)) == 1
    assert ops[-1][1] == "FAILED" and "smtp down" in ops[-1][3][0]
    assert log[("daily_deals_email", "2026-09-28")]["status"] == "failed"

    boom["on"] = False
    assert run(datetime(2026, 9, 29, 9, 31, tzinfo=IST)) == 0   # 09:30 trigger sends the edition
    assert len(sent) == 1 and not sent[0]["subject"].startswith("[UPDATED")
    assert ops[-1][1] == "SENT"
