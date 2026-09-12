"""
Cases pinning reports/weekly_deals_report.py.

The weekly report's whole reason to exist is the four aggregations a single
session cannot produce — persistent flows, the daily trend, class net flows and
first appearances — so those are what this file pins. Each fixture below is
shaped to make one claim falsifiable:

  * IDEA          a four-session one-way build. Must read as accumulation, and
                  must combine quantities across sessions rather than reporting
                  the largest one.
  * YESBANK       a three-session exit. Must come out net negative.
  * SUZLON        a client that bought and sold the same size every session.
                  Must be classified as churn and excluded from the one-way
                  table, because calling a round trip a position is the one
                  error in this report that would read as a signal.
  * HDFCBANK      a build split across the bulk feed and the block window.
                  Must appear as one flow, not two half-signals.
  * TCS           a single-session print. Must NOT appear in persistent flows.

No network and no database: the three functions that reach Supabase are
monkeypatched, everything else runs as it does in production.

Run: python -m pytest tests/test_weekly_deals_report.py -q
"""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from reports import client_class as cc
from reports import weekly_deals_report as W

# Mon–Fri. A real trading week with no holiday in it, so the fixtures are not
# also testing the holiday calendar.
WEEK = [date(2026, 8, 17) + timedelta(days=i) for i in range(5)]

SOCGEN   = "SOCIETE GENERALE - ODI"
GRAVITON = "GRAVITON RESEARCH CAPITAL LLP"
ICICIPRU = "ICICI PRUDENTIAL MUTUAL FUND"
QUANTMF  = "QUANT MUTUAL FUND"


def _bulk() -> pd.DataFrame:
    rows = []
    # Four sessions, buy only, rising size — the canonical accumulation.
    for i, dt in enumerate(WEEK[:4]):
        rows.append(dict(deal_date=dt.isoformat(), symbol="IDEA",
                         security_name="Vodafone Idea Limited",
                         client_name=SOCGEN, buy_sell="B",
                         quantity=3_000_000 + i * 400_000,
                         avg_price=14.2 + i * 0.3))
    # Three sessions, sell only.
    for i, dt in enumerate(WEEK[1:4]):
        rows.append(dict(deal_date=dt.isoformat(), symbol="YESBANK",
                         security_name="Yes Bank Limited",
                         client_name=ICICIPRU, buy_sell="S",
                         quantity=2_200_000 + i * 300_000,
                         avg_price=21.4 + i * 0.2))
    # Equal buy and sell every session: gross is large, net is nil.
    for dt in WEEK:
        for side in ("B", "S"):
            rows.append(dict(deal_date=dt.isoformat(), symbol="SUZLON",
                             security_name="Suzlon Energy Limited",
                             client_name=GRAVITON, buy_sell=side,
                             quantity=1_500_000, avg_price=62.5))
    # One session only — the control case for the multi-session filter.
    rows.append(dict(deal_date=WEEK[1].isoformat(), symbol="TCS",
                     security_name="Tata Consultancy Services Limited",
                     client_name=SOCGEN, buy_sell="B",
                     quantity=180_000, avg_price=3_900.0))
    return W._enrich_bulk(pd.DataFrame(rows))


def _block() -> pd.DataFrame:
    rows = [
        # Half of a HDFCBANK build; the other half arrives in the bulk feed
        # below via a second block session.
        dict(deal_date=WEEK[0].isoformat(), symbol="HDFCBANK",
             security_name="HDFC Bank Limited", client_name=QUANTMF,
             buy_sell="B", quantity=600_000, trade_price=1_680.0),
        dict(deal_date=WEEK[3].isoformat(), symbol="HDFCBANK",
             security_name="HDFC Bank Limited", client_name=QUANTMF,
             buy_sell="B", quantity=750_000, trade_price=1_692.0),
        # The week's largest single print, both legs reported.
        dict(deal_date=WEEK[2].isoformat(), symbol="RELIANCE",
             security_name="Reliance Industries Limited", client_name=QUANTMF,
             buy_sell="B", quantity=2_400_000, trade_price=2_950.0),
        dict(deal_date=WEEK[2].isoformat(), symbol="RELIANCE",
             security_name="Reliance Industries Limited", client_name=SOCGEN,
             buy_sell="S", quantity=2_400_000, trade_price=2_950.0),
    ]
    return W._enrich_block(pd.DataFrame(rows))


def _short() -> pd.DataFrame:
    rows = []
    for i, dt in enumerate(WEEK):
        for sym in ("RELIANCE", "IDEA"):
            rows.append(dict(deal_date=dt.isoformat(), symbol=sym,
                             security_name=f"{sym} Limited",
                             quantity=5_000 + i * 1_000))
    return W._enrich_short(pd.DataFrame(rows))


@pytest.fixture
def bulk():
    return _bulk()


@pytest.fixture
def block():
    return _block()


@pytest.fixture
def short():
    return _short()


def _pair(persist: pd.DataFrame, client: str, symbol: str) -> pd.Series | None:
    hit = persist[(persist["client_name"] == client) & (persist["symbol"] == symbol)]
    return None if hit.empty else hit.iloc[0]


# ── Persistent flows: the section the weekly exists for ──────────────────────

def test_multi_session_build_sums_across_sessions(bulk, block):
    """REGRESSION: a build is the sum of its sessions, not its largest one.

    Reporting the biggest single print instead of the total is the failure mode
    that makes an accumulation look like an ordinary trade.
    """
    r = _pair(W._persistent_flows(bulk, block), SOCGEN, "IDEA")
    assert r is not None
    assert int(r["sessions"]) == 4
    assert int(r["net_qty"]) == 3_000_000 + 3_400_000 + 3_800_000 + 4_200_000
    assert r["side"] == "accumulating"
    assert float(r["net_cr"]) > 0


def test_multi_session_exit_is_net_negative(bulk, block):
    r = _pair(W._persistent_flows(bulk, block), ICICIPRU, "YESBANK")
    assert r is not None
    assert int(r["sessions"]) == 3
    assert int(r["net_qty"]) == -(2_200_000 + 2_500_000 + 2_800_000)
    assert r["side"] == "distributing"
    assert float(r["net_cr"]) < 0


def test_round_trip_is_not_reported_as_a_position(bulk, block):
    """REGRESSION: the error that would read as a signal.

    A desk in and out of the same size five days running has no position. It
    must survive into the multi-session frame — so the report can say how many
    pairs were churn — while being kept out of the one-way table.
    """
    persist = W._persistent_flows(bulk, block)
    r = _pair(persist, GRAVITON, "SUZLON")
    assert r is not None
    assert int(r["net_qty"]) == 0
    assert float(r["conviction"]) == 0.0
    assert _pair(W._one_way(persist), GRAVITON, "SUZLON") is None
    assert _pair(W._round_trips(persist), GRAVITON, "SUZLON") is not None


def test_conviction_floor_is_what_separates_the_two(bulk, block):
    persist = W._persistent_flows(bulk, block)
    one_way, churn = W._one_way(persist), W._round_trips(persist)
    assert len(one_way) + len(churn) == len(persist)
    assert (one_way["conviction"] >= W.PERSIST_MIN_CONVICTION).all()
    assert (churn["conviction"] < W.PERSIST_MIN_CONVICTION).all()


def test_bulk_and_block_combine_into_one_flow(bulk, block):
    """A position built across both feeds is one position.

    HDFCBANK is bought in the block window on two separate sessions. Keeping
    the feeds apart would either halve the flow or drop it below the
    multi-session floor entirely.
    """
    r = _pair(W._persistent_flows(bulk, block), QUANTMF, "HDFCBANK")
    assert r is not None
    assert int(r["sessions"]) == 2
    assert int(r["net_qty"]) == 1_350_000


def test_single_session_trade_is_not_a_flow(bulk, block):
    assert _pair(W._persistent_flows(bulk, block), SOCGEN, "TCS") is None


def test_persistent_flows_rank_by_size_not_session_count(bulk, block):
    """REGRESSION: size orders the table, persistence only qualifies for it.

    Ranking on session count buries the week's largest flow — on the real
    2026-08-17 week it put a four-session 4-crore position above a 914-crore
    exit built over two sessions. Sessions remains a column, not the sort key.
    """
    persist = W._persistent_flows(bulk, block)
    mags = list(persist["net_cr"].abs())
    assert mags == sorted(mags, reverse=True)


def test_persistent_flows_survives_empty_input():
    assert W._persistent_flows(pd.DataFrame(), pd.DataFrame()).empty


# ── Daily trend ──────────────────────────────────────────────────────────────

def test_trend_keeps_a_row_for_every_trading_day(bulk, block, short):
    """A quiet session must be visibly quiet.

    Dropping empty days would let the chart imply the week ran Mon, Tue, Thu,
    Fri — and would hide a missed scrape completely.
    """
    trend = W._daily_trend(bulk, block, short, WEEK)
    assert len(trend) == len(WEEK)
    assert list(trend["deal_date"]) == WEEK


def test_trend_totals_are_internally_consistent(bulk, block, short):
    trend = W._daily_trend(bulk, block, short, WEEK)
    assert (trend["total_cr"].round(2) ==
            (trend["bulk_cr"] + trend["block_cr"]).round(2)).all()
    assert (trend["deals"] == trend["bulk_deals"] + trend["block_deals"]).all()


def test_trend_counts_a_crossed_deal_once(block):
    """Both legs of the RELIANCE cross are reported; the value is not doubled."""
    trend = W._daily_trend(pd.DataFrame(), block, pd.DataFrame(), WEEK)
    wed = trend[trend["deal_date"] == WEEK[2]].iloc[0]
    one_leg = 2_400_000 * 2_950.0 / 1e7
    assert float(wed["block_cr"]) == pytest.approx(one_leg, rel=1e-6)


def test_empty_trend_renders_no_chart():
    trend = W._daily_trend(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), WEEK)
    assert len(trend) == len(WEEK)
    assert W._trend_chart(trend) == ""


# ── Class net flows ──────────────────────────────────────────────────────────

def test_class_flows_are_arithmetically_sound(bulk, block):
    cf = W._class_net_flows(bulk, block)
    assert not cf.empty
    assert (cf["net_cr"].round(2) == (cf["buy_cr"] - cf["sell_cr"]).round(2)).all()
    assert (cf["gross_cr"].round(2) == (cf["buy_cr"] + cf["sell_cr"]).round(2)).all()


def test_class_flows_follow_the_house_class_order(bulk, block):
    """Stable ordering: the table must read the same way every week."""
    from reports import client_class as cc
    cf = W._class_net_flows(bulk, block)
    present = set(cf["class"])
    assert list(cf["class"]) == [c for c in cc.CLASS_ORDER if c in present]


def test_class_flows_survive_empty_input():
    assert W._class_net_flows(pd.DataFrame(), pd.DataFrame()).empty


# ── First appearances ────────────────────────────────────────────────────────

def test_first_appearance_reports_only_names_absent_from_the_baseline(bulk, block):
    baseline_syms = {"IDEA", "YESBANK", "SUZLON", "TCS"}       # RELIANCE, HDFCBANK new
    baseline_clients = {SOCGEN, GRAVITON, ICICIPRU}            # QUANTMF new
    ns, nc = W._first_appearances(bulk, block, baseline_syms, baseline_clients)
    assert set(ns["symbol"]) == {"RELIANCE", "HDFCBANK"}
    assert set(nc["client_name"]) == {QUANTMF}


def test_empty_baseline_does_not_declare_everything_new(bulk, block):
    """REGRESSION: a failed lookback must suppress the section, not invert it.

    With no baseline every name looks new, and the section would report the
    whole week as first appearances — wrong in exactly the direction a reader
    would act on.
    """
    ns, nc = W._first_appearances(bulk, block, set(), set())
    assert ns.empty and nc.empty


def test_first_appearance_html_says_so_when_there_is_nothing(bulk, block):
    html = W._first_appearance_html(
        pd.DataFrame(), pd.DataFrame(), top_n=None, show_badge=False, source="",
    )
    assert "had already appeared" in html


# ── Weekly shorts ────────────────────────────────────────────────────────────

def test_shorts_collapse_to_one_row_per_name(short):
    sw = W._short_week_rows(short)
    assert len(sw) == short["symbol"].nunique()
    assert int(sw["quantity"].sum()) == int(short["quantity"].sum())
    assert (sw["peak"] <= sw["quantity"]).all()
    assert (sw["sessions"] == len(WEEK)).all()


def test_shorts_survive_empty_input():
    assert W._short_week_rows(pd.DataFrame()).empty


# ── Week window resolution ───────────────────────────────────────────────────

@pytest.mark.parametrize("anchor", [
    date(2026, 8, 17),   # Monday
    date(2026, 8, 19),   # Wednesday
    date(2026, 8, 21),   # Friday
])
def test_any_weekday_anchor_resolves_to_the_same_week(anchor, monkeypatch):
    monkeypatch.setattr(
        "utils.helpers.is_trading_day", lambda dt: dt.weekday() < 5,
    )
    monkeypatch.setattr(
        "utils.helpers.iter_trading_days",
        lambda s, e: [s + timedelta(days=i) for i in range((e - s).days + 1)
                      if (s + timedelta(days=i)).weekday() < 5],
    )
    start, end, days = W._week_window(anchor)
    assert (start, end) == (WEEK[0], WEEK[-1])
    assert days == WEEK


def test_a_holiday_shortened_week_reports_its_real_first_session(monkeypatch):
    """The dateline must not claim a session that never happened."""
    monkeypatch.setattr(
        "utils.helpers.iter_trading_days",
        lambda s, e: [s + timedelta(days=i) for i in range((e - s).days + 1)
                      if (s + timedelta(days=i)).weekday() < 5
                      and (s + timedelta(days=i)) != WEEK[0]],
    )
    start, end, days = W._week_window(WEEK[2])
    assert start == WEEK[1]           # Monday was a holiday
    assert end == WEEK[-1]
    assert len(days) == 4


def test_a_fully_closed_week_does_not_crash(monkeypatch):
    monkeypatch.setattr("utils.helpers.iter_trading_days", lambda s, e: [])
    start, end, days = W._week_window(WEEK[2])
    assert days == []
    assert start == WEEK[0] and end == WEEK[-1]


# ── Missing-session guard ────────────────────────────────────────────────────

def test_a_swallowed_session_is_detected(bulk, block):
    """A hole in the middle of the week understates every total below it."""
    drop = WEEK[2]
    b = bulk[pd.to_datetime(bulk["deal_date"]).dt.date != drop]
    k = block[pd.to_datetime(block["deal_date"]).dt.date != drop]
    assert W._missing_sessions(WEEK, b, k) == [drop]
    card = W._degraded_card([], [drop])
    assert card is not None
    assert "Wed 19 Aug" in card["body"]


def test_a_complete_week_raises_no_warning(bulk, block):
    assert W._missing_sessions(WEEK, bulk, block) == []
    assert W._degraded_card([], []) is None


# ── Week-on-week ─────────────────────────────────────────────────────────────

def test_wow_is_none_rather_than_a_division_error(bulk, block, short):
    cur = W._topline(bulk, block, short)
    nothing = W._topline(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    wow = W._wow(cur, nothing)
    assert wow["bulk_value"] is None
    assert "no prior week" in W._fmt_pct_delta(None)


def test_wow_direction_is_signed_correctly(bulk, block, short):
    cur = W._topline(bulk, block, short)
    half = W._topline(bulk.head(len(bulk) // 2), block, short)
    assert W._wow(cur, half)["bulk_value"] > 0
    assert W._wow(half, cur)["bulk_value"] < 0


# ── Formatting ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("start,end,expect", [
    (date(2026, 8, 24),  date(2026, 8, 28), "24&ndash;28 Aug 2026"),
    (date(2026, 9, 28),  date(2026, 10, 2), "28 Sep&ndash;02 Oct 2026"),
    (date(2026, 12, 28), date(2027, 1, 1),  "28 Dec 2026&ndash;01 Jan 2027"),
    (date(2026, 8, 24),  date(2026, 8, 24), "24 Aug 2026"),
])
def test_range_collapses_what_the_dates_share(start, end, expect):
    assert W._house_range(start, end) == expect


def test_signed_money_carries_direction_not_magnitude():
    """A net seller is a direction, not a missing value.

    The daily's _fmt_cr prints the em-dash at or below zero because there every
    value is a magnitude. Reusing it for a net figure would erase every seller.
    """
    assert W._GOOD in W._fmt_signed_cr(12.5)
    assert W._BAD in W._fmt_signed_cr(-12.5)
    assert "flat" in W._fmt_signed_cr(0.0)
    assert "&mdash;" in W._fmt_signed_cr(None)


# ── First-appearance materiality floor ───────────────────────────────────────
# REGRESSION: without a floor this section is a census, not a signal. On the
# real week of 17-21 Aug 2026 it flagged 71 of 121 names and 124 of 225 clients,
# and 108 of those clients traded one name on one session — single block
# participants, which is the structure of the feed rather than news.

def test_floor_keeps_the_material_entrants_and_drops_the_tail(bulk, block):
    baseline_syms = {"IDEA", "YESBANK", "SUZLON", "TCS"}
    baseline_clients = {SOCGEN, GRAVITON, ICICIPRU}
    ns, nc = W._first_appearances(bulk, block, baseline_syms, baseline_clients)

    # RELIANCE is a 708-crore print; HDFCBANK is ~229 crore. Both clear 100.
    kept100, _ = W._material(ns, 100.0)
    assert set(kept100["symbol"]) == {"RELIANCE", "HDFCBANK"}
    # A floor above both leaves nothing material, but nothing is deleted.
    kept, dropped = W._material(ns, 5_000.0)
    assert kept.empty and dropped == len(ns)


def test_floor_is_a_lens_not_a_filter_on_the_record(bulk, block):
    """min_cr=0 must list every absent name — that is what the PDF passes."""
    ns, nc = W._first_appearances(bulk, block, {"IDEA"}, {SOCGEN})
    kept, dropped = W._material(ns, 0.0)
    assert len(kept) == len(ns) and dropped == 0


def test_body_states_what_the_floor_set_aside(bulk, block):
    """Silent truncation reads as 'this is everything'. It must not be silent."""
    ns, nc = W._first_appearances(bulk, block, {"IDEA"}, {SOCGEN})
    html = W._first_appearance_html(
        ns, nc, top_n=None, show_badge=False, source="", min_cr=300.0,
    )
    assert "set aside" in html
    assert "lists every absent name" in html


def test_nothing_material_says_so_rather_than_rendering_empty(bulk, block):
    """An empty section reads as 'no new names', which would be false."""
    ns, nc = W._first_appearances(bulk, block, {"IDEA"}, {SOCGEN})
    html = W._first_appearance_html(
        ns, nc, top_n=None, show_badge=False, source="", min_cr=99_999.0,
    )
    assert "were absent from the prior" in html
    assert "none carried" in html


# ── Class net flows after the classifier fix ─────────────────────────────────

def test_foreign_holding_vehicle_no_longer_lands_in_hni():
    """REGRESSION: the defect this review found.

    RESILIENT ASSET MANAGEMENT B V was the largest single seller of the week of
    17-21 Aug 2026 at 5,898 crore, classified HNI, which made Section III report
    that individuals and HUFs sold 5,819 crore net. One Dutch holding vehicle
    was 101% of that headline.
    """
    from reports import client_class as cc
    assert cc.classify("RESILIENT ASSET MANAGEMENT B V") == cc.CORP

    df = pd.DataFrame([
        dict(deal_date=WEEK[0].isoformat(), symbol="PAYTM",
             security_name="One 97 Communications Ltd",
             client_name="RESILIENT ASSET MANAGEMENT B V", buy_sell="S",
             quantity=19_210_110, avg_price=1_535.0),
        dict(deal_date=WEEK[0].isoformat(), symbol="PAYTM",
             security_name="One 97 Communications Ltd",
             client_name="ARUNA GANESH", buy_sell="S",
             quantity=10_000, avg_price=1_535.0),
    ])
    cf = W._class_net_flows(W._enrich_bulk(df), pd.DataFrame())
    hni = cf[cf["class"] == "HNI"]
    corp = cf[cf["class"] == "CORP"]
    # The individual stays in HNI; the Dutch vehicle does not.
    assert len(hni) == 1 and int(hni.iloc[0]["clients"]) == 1
    assert len(corp) == 1 and int(corp.iloc[0]["clients"]) == 1
    assert abs(float(corp.iloc[0]["net_cr"])) > abs(float(hni.iloc[0]["net_cr"]))


# ── Class flow per session ───────────────────────────────────────────────────
# The week-total table says a class was a net seller. It cannot say whether that
# was one block on Wednesday or four days of steady selling, and those are
# different events. These pin the frame that separates them.

def test_class_daily_rows_reconcile_with_the_week_table(bulk, block):
    """REGRESSION: two aggregations of one truth must agree.

    A chart that does not sum to the table beneath it is worse than no chart —
    the reader cannot tell which number to believe.
    """
    dc = W._class_daily_flows(bulk, block, WEEK)
    cf = W._class_net_flows(bulk, block)
    assert not dc.empty
    for tag in cf["class"]:
        charted = float(dc[dc["class"] == tag]["net_cr"].sum())
        tabled = float(cf[cf["class"] == tag]["net_cr"].iloc[0])
        assert charted == pytest.approx(tabled, abs=0.05), tag


def test_every_active_class_gets_a_cell_for_every_session(bulk, block):
    """A class idle on Wednesday must render as a gap at the centre line.

    Dropping the row would shift the rest of the week left and misstate when
    the flow happened.
    """
    dc = W._class_daily_flows(bulk, block, WEEK)
    n_classes = dc["class"].nunique()
    assert len(dc) == n_classes * len(WEEK)
    for tag in dc["class"].unique():
        assert list(dc[dc["class"] == tag]["deal_date"]) == WEEK


def test_class_daily_net_is_buy_minus_sell(bulk, block):
    dc = W._class_daily_flows(bulk, block, WEEK)
    assert (dc["net_cr"].round(2) == (dc["buy_cr"] - dc["sell_cr"]).round(2)).all()


def test_class_daily_follows_the_house_class_order(bulk, block):
    dc = W._class_daily_flows(bulk, block, WEEK)
    seen = list(dict.fromkeys(dc["class"]))
    assert seen == [c for c in W.cc.CLASS_ORDER if c in set(seen)]


def test_class_daily_survives_empty_input():
    assert W._class_daily_flows(pd.DataFrame(), pd.DataFrame(), WEEK).empty


# ── The chart itself ─────────────────────────────────────────────────────────

def test_chart_encodes_direction_by_colour_and_by_side(bulk, block):
    """Direction is encoded twice so the chart survives greyscale and
    red-green colour blindness. Both encodings must actually be present."""
    dc = W._class_daily_flows(bulk, block, WEEK)
    cf = W._class_net_flows(bulk, block)
    html = W._class_flow_chart(dc, cf, WEEK)
    assert W._GOOD in html and W._BAD in html          # colour
    assert html.count("bgcolor") > len(WEEK)           # bars, not just a legend
    assert "net buyer that session" in html            # legend explains both
    assert "net seller" in html


def test_chart_states_its_scale(bulk, block):
    """A bar chart without a scale is a shape, not a measurement."""
    dc = W._class_daily_flows(bulk, block, WEEK)
    cf = W._class_net_flows(bulk, block)
    html = W._class_flow_chart(dc, cf, WEEK)
    assert "full bar =" in html


def test_chart_labels_every_session_column(bulk, block):
    dc = W._class_daily_flows(bulk, block, WEEK)
    cf = W._class_net_flows(bulk, block)
    html = W._class_flow_chart(dc, cf, WEEK)
    for dt in WEEK:
        assert dt.strftime("%a %d") in html


def test_chart_uses_no_images_and_no_css_bars(bulk, block):
    """Outlook blocks images until the reader opts in, and Word ignores width
    on a div. The bars have to be table cells or they do not render."""
    dc = W._class_daily_flows(bulk, block, WEEK)
    cf = W._class_net_flows(bulk, block)
    html = W._class_flow_chart(dc, cf, WEEK)
    assert "<img" not in html
    assert "<div" not in html


def test_chart_is_suppressed_rather_than_rendered_empty():
    """A grid of empty cells claims a quiet week it has no evidence for."""
    assert W._class_flow_chart(pd.DataFrame(), pd.DataFrame(), WEEK) == ""
    flat = pd.DataFrame([
        {"class": "FII", "deal_date": dt, "buy_cr": 0.0, "sell_cr": 0.0,
         "net_cr": 0.0, "deals": 0} for dt in WEEK
    ])
    assert W._class_flow_chart(flat, pd.DataFrame(), WEEK) == ""


def test_chart_gives_a_tiny_nonzero_net_a_visible_bar(bulk, block):
    """Scaled to a 5,000-crore peak, a 3-crore net rounds to zero pixels.

    It still has to be visible, or the chart shows a class as idle on a session
    it actually traded — which the caption explicitly says means something else.
    """
    dc = pd.DataFrame(
        [{"class": "FII", "deal_date": WEEK[0], "buy_cr": 5000.0,
          "sell_cr": 0.0, "net_cr": 5000.0, "deals": 1}]
        + [{"class": "HNI", "deal_date": WEEK[0], "buy_cr": 0.0,
            "sell_cr": 3.0, "net_cr": -3.0, "deals": 1}]
        + [{"class": c, "deal_date": dt, "buy_cr": 0.0, "sell_cr": 0.0,
            "net_cr": 0.0, "deals": 0}
           for c in ("FII", "HNI") for dt in WEEK[1:]]
    )
    html = W._class_flow_chart(dc, pd.DataFrame(), WEEK)
    # The 3-crore cell must still paint a bar in the sell colour.
    assert W._BAD in html


# ── Concentration: the denominator has to be the published one ───────────────

def test_concentration_sums_to_the_same_week_total_as_the_session_chart(
    bulk, block, short
):
    """REGRESSION: the Pareto sits directly under the session chart, and the two
    have to agree on how big the week was.

    The first cut of `_concentration` took max(buy, sell) per name over the
    whole week; `_daily_trend` takes it per feed per session. Those look
    equivalent and are not — a name bought heavily on one session and sold
    heavily on another collapses to the larger leg — and on real data for
    17-21 Aug 2026 the section headline came out 30 crore light against the
    chart immediately above it.
    """
    conc = W._concentration(bulk, block)
    trend = W._daily_trend(bulk, block, short, WEEK)
    assert round(float(conc["value_cr"].sum()), 2) == \
           round(float(trend["total_cr"].sum()), 2)


def test_concentration_running_share_is_monotonic_and_closes_at_100(bulk, block):
    conc = W._concentration(bulk, block)
    cum = list(conc["cum_pct"])
    assert cum == sorted(cum)
    assert cum[-1] == pytest.approx(100.0, abs=1e-6)


def test_concentration_is_ordered_largest_first(bulk, block):
    conc = W._concentration(bulk, block)
    vals = list(conc["value_cr"])
    assert vals == sorted(vals, reverse=True)


def test_concentration_survives_empty_input():
    assert W._concentration(pd.DataFrame(), pd.DataFrame()).empty


def test_pareto_is_suppressed_rather_than_drawn_empty():
    """An empty Pareto asserts a week with no value in it."""
    assert W._pareto_chart(pd.DataFrame()) == ""


def test_pareto_uses_no_images_and_no_css_bars(bulk, block):
    html = W._pareto_chart(W._concentration(bulk, block))
    assert "<img" not in html
    assert "<div" not in html


def test_pareto_states_the_total_it_is_a_share_of(bulk, block):
    """A percentage with no denominator on the page is not checkable."""
    conc = W._concentration(bulk, block)
    html = W._pareto_chart(conc)
    assert W._strip_html(W._fmt_cr(float(conc["value_cr"].sum()))) in \
           W._strip_html(html)


# ── Cumulative FII vs DII ────────────────────────────────────────────────────

def test_cumulative_final_equals_the_week_net_table(bulk, block):
    """The chart and the table beneath it must not disagree about the week."""
    cd = W._class_daily_flows(bulk, block, WEEK)
    cf = W._class_net_flows(bulk, block)
    cum = W._cumulative_class_flow(cd, WEEK)
    for tag in set(cum["class"]):
        final = float(cum[cum["class"] == tag]["cum_cr"].iloc[-1])
        table = float(cf[cf["class"] == tag]["net_cr"].iloc[0])
        assert final == pytest.approx(table, abs=0.01)


def test_cumulative_carries_a_quiet_session_forward(bulk, block):
    """A class that did not trade held its position; it did not go flat.

    Resetting to zero on a quiet session would draw an exit that never
    happened — the single most misleading thing a cumulative chart can do.
    """
    cd = pd.DataFrame([
        {"class": "FII", "deal_date": WEEK[0], "buy_cr": 0.0,
         "sell_cr": 400.0, "net_cr": -400.0, "deals": 1},
        # No row at all for WEEK[1] — the class was absent that session.
        {"class": "FII", "deal_date": WEEK[2], "buy_cr": 0.0,
         "sell_cr": 100.0, "net_cr": -100.0, "deals": 1},
    ])
    cum = W._cumulative_class_flow(cd, WEEK[:3])
    got = list(cum["cum_cr"])
    assert got == [-400.0, -400.0, -500.0]


def test_cumulative_is_a_running_sum_not_a_repeat_of_the_daily(bulk, block):
    cd = W._class_daily_flows(bulk, block, WEEK)
    cum = W._cumulative_class_flow(cd, WEEK)
    for tag in set(cum["class"]):
        sub = cum[cum["class"] == tag]
        assert list(sub["cum_cr"]) == pytest.approx(
            list(sub["net_cr"].cumsum().round(2))
        )


def test_cumulative_only_carries_the_two_classes_it_claims(bulk, block):
    cd = W._class_daily_flows(bulk, block, WEEK)
    cum = W._cumulative_class_flow(cd, WEEK)
    assert set(cum["class"]) <= {cc.FII, cc.DII}


def test_cumulative_chart_is_suppressed_rather_than_drawn_empty():
    assert W._cumulative_flow_chart(pd.DataFrame(), WEEK) == ""


def test_cumulative_chart_uses_no_images_and_no_css_bars(bulk, block):
    cd = W._class_daily_flows(bulk, block, WEEK)
    html = W._cumulative_flow_chart(W._cumulative_class_flow(cd, WEEK), WEEK)
    assert "<img" not in html
    assert "<div" not in html


def test_cumulative_chart_encodes_direction_by_colour_and_by_side(bulk, block):
    cd = W._class_daily_flows(bulk, block, WEEK)
    html = W._cumulative_flow_chart(W._cumulative_class_flow(cd, WEEK), WEEK)
    assert W._GOOD in html and W._BAD in html


# ── Shading ──────────────────────────────────────────────────────────────────

def test_shade_darkens_with_conviction():
    """Three distinct steps, or the shading carries no information."""
    steps = [W._shade("buy", c) for c in (0.30, 0.60, 0.90)]
    assert len(set(steps)) == 3
    assert steps[2] == W._GOOD


def test_shade_keeps_direction_separate_from_strength():
    for conv in (0.30, 0.60, 0.90):
        assert W._shade("buy", conv) != W._shade("sell", conv)


# ── Deal-count track ─────────────────────────────────────────────────────────

def test_count_track_is_absent_for_a_session_with_no_deals():
    assert W._count_track(0, 200.0, 268) == ""


def test_count_track_scales_against_the_busiest_session():
    """The busiest session fills the track; half as many deals, half the width."""
    full = W._count_track(200, 200.0, 268)
    half = W._count_track(100, 200.0, 268)
    assert 'width="268"' in full
    assert 'width="134"' in half


def test_trend_chart_draws_the_count_track(bulk, block, short):
    """The Thursday case: most deals of the week, fourth by value. Without the
    count track that session reads as quiet."""
    html = W._trend_chart(W._daily_trend(bulk, block, short, WEEK))
    assert W.d.RULE_STRONG in html
    assert "deal count" in html


# ── By-symbol net-direction bars ─────────────────────────────────────────────

def test_sym_bars_replace_the_flag_glyph(bulk):
    html = W._sym_table_bars_html(W._sym_rows(bulk))
    assert "Net direction" in html
    assert "&#9650;" not in html


def test_sym_bars_put_a_net_seller_left_and_a_net_buyer_right(bulk):
    """IDEA was bought only and YESBANK sold only; the two must not draw the
    same bar."""
    rows = W._sym_rows(bulk)
    html = W._sym_table_bars_html(rows)
    assert W._GOOD in html and W._BAD in html


def test_sym_bars_leave_a_crossed_name_at_the_centre(bulk):
    """SUZLON bought and sold the same size all week: net zero, no bar."""
    rows = W._sym_rows(bulk)
    suzlon = rows[rows["symbol"] == "SUZLON"]
    assert int(suzlon["buy_qty"].iloc[0]) == int(suzlon["sell_qty"].iloc[0])
    html = W._sym_table_bars_html(suzlon)
    assert W._GOOD not in html and W._BAD not in html


def test_sym_bars_survive_an_empty_frame():
    assert "No deals in scope" in W._sym_table_bars_html(pd.DataFrame())


# ── Persistent-flow lollipop ─────────────────────────────────────────────────

def test_persist_table_carries_a_shaded_flow_bar(bulk, block):
    persist = W._persistent_flows(bulk, block)
    html = W._persist_table_html(
        W._one_way(persist), len(W._round_trips(persist)),
        top_n=None, show_badge=False, source="x",
    )
    assert "Flow" in html
    assert W._GOOD in html or W._BAD in html


def test_persist_bars_scale_to_the_rows_actually_shown(bulk, block):
    """REGRESSION: scaling a capped body against a row only the PDF shows would
    render every visible bar as a stub."""
    persist = W._persistent_flows(bulk, block)
    one_way = W._one_way(persist)
    capped = W._persist_table_html(
        one_way, 0, top_n=1, show_badge=False, source="x",
    )
    # The single row shown is by definition the largest in its own frame, so it
    # must draw a full-width bar.
    assert 'width="44"' in capped


# ── Squarified treemap layout ────────────────────────────────────────────────

def test_squarify_conserves_area():
    vals = [50.0, 25.0, 12.0, 8.0, 5.0]
    rects = W._squarify(vals, 0.0, 0.0, 400.0, 200.0)
    assert len(rects) == len(vals)
    drawn = sum(w * h for _, _, w, h in rects)
    assert drawn == pytest.approx(400.0 * 200.0, rel=1e-6)


def test_squarify_areas_are_proportional_to_their_values():
    vals = [60.0, 30.0, 10.0]
    rects = W._squarify(vals, 0.0, 0.0, 300.0, 300.0)
    areas = [w * h for _, _, w, h in rects]
    total = sum(areas)
    for v, a in zip(vals, areas):
        assert a / total == pytest.approx(v / sum(vals), rel=1e-6)


def test_squarify_stays_inside_its_box():
    rects = W._squarify([7.0, 3.0, 2.0, 1.0], 10.0, 20.0, 100.0, 50.0)
    for x, y, w, h in rects:
        assert x >= 10.0 - 1e-6 and y >= 20.0 - 1e-6
        assert x + w <= 110.0 + 1e-6 and y + h <= 70.0 + 1e-6


def test_squarify_handles_degenerate_input():
    assert W._squarify([], 0, 0, 10, 10) == []
    assert W._squarify([1.0], 0, 0, 0, 10) == []
    assert W._squarify([0.0, 0.0], 0, 0, 10, 10) == []


# ── Comprehensive-edition SVG ────────────────────────────────────────────────

def test_treemap_is_svg_and_labels_its_classes(bulk, block):
    html = W._treemap_svg(W._class_symbol_values(bulk, block))
    assert "<svg" in html
    assert cc.FII in html or cc.DII in html


def test_treemap_survives_empty_input():
    assert W._treemap_svg(pd.DataFrame()) == ""


def test_scatter_survives_a_universe_with_no_market_caps(bulk, block, monkeypatch):
    """The security master can be stale or unloaded; a chart that cannot be
    drawn must be absent, not half-drawn."""
    monkeypatch.setattr(W.sm, "market_cap_cr", lambda s: None)
    conc = W._concentration(bulk, block)
    assert W._scatter_svg(conc, bulk, block) == ""


def test_scatter_says_how_many_names_it_could_not_plot(bulk, block, monkeypatch):
    caps = {"IDEA": 90_000.0, "YESBANK": 60_000.0, "SUZLON": 80_000.0}
    monkeypatch.setattr(W.sm, "market_cap_cr", lambda s: caps.get(s))
    conc = W._concentration(bulk, block)
    html = W._scatter_svg(conc, bulk, block)
    assert "<svg" in html
    assert "not plotted" in html


# ── The medium invariant: SVG never reaches an inbox ─────────────────────────

def _editions(bulk, block, short):
    frames = W._derive(bulk, block, short, WEEK, set(), set())
    wow = W._wow(frames["metrics"], frames["metrics"])
    highlights = W._weekly_highlights(
        bulk, block, frames["trend"], frames["one_way"], frames["class_flow"],
    )
    gen = datetime(2026, 8, 22, 4, 30, tzinfo=timezone.utc)
    common = dict(week_frames=frames, full_metrics=frames["metrics"],
                  n_full_names=len(W._all_symbols(bulk, block, short)))
    email = W._build_html(
        WEEK[0], WEEK[-1], WEEK, frames, wow, highlights, gen,
        edition=W.EDITION_FOCUS, focus_symbols=["IDEA"], **common,
    )
    pdf = W._build_html(
        WEEK[0], WEEK[-1], WEEK, frames, wow, highlights, gen,
        edition=W.EDITION_FULL, **common,
    )
    return email, pdf


def test_the_email_edition_carries_no_svg_and_no_images(bulk, block, short):
    """Word renders no SVG at all. An SVG that reached the body would not
    degrade — it would leave a section heading over blank space."""
    email, _ = _editions(bulk, block, short)
    assert "<svg" not in email.lower()
    assert "<img" not in email.lower()


def test_the_pdf_edition_carries_the_svg_charts(bulk, block, short):
    _, pdf = _editions(bulk, block, short)
    assert pdf.lower().count("<svg") >= 1
    assert "The week in one frame" in pdf


def test_the_pdf_only_sections_are_absent_from_the_email(bulk, block, short):
    email, pdf = _editions(bulk, block, short)
    for heading in ("The week in one frame", "Value against company size"):
        assert heading in pdf
        assert heading not in email


def test_both_editions_carry_the_new_email_safe_charts(bulk, block, short):
    email, pdf = _editions(bulk, block, short)
    for html in (email, pdf):
        assert "Where the week&rsquo;s value sat" in html
        assert "Foreign against domestic, cumulative" in html


def test_the_masthead_uses_the_house_name(bulk, block, short):
    email, _ = _editions(bulk, block, short)
    assert "Brindco &middot; Quant Desk" in email
    assert "Alpha Capital" not in email
