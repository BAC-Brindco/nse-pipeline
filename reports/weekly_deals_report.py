"""
Weekly NSE deals email report — the Monday-to-Friday review.

Runs once per week on Saturday morning IST, reporting on the trading week that
has just finished. It is not five daily reports stapled together: the sections
that carry a week's value are the ones a single session structurally cannot
show.

  * PERSISTENT FLOWS — clients that appear across several sessions on the same
    side. One 40-crore print is a trade; the same client on the bid four days
    running is a position being built. This is the section the weekly exists
    for.
  * DAILY TREND — where in the week the money moved, so one heavy session is
    not read as a heavy week.
  * CLASS NET FLOWS — which kinds of participant were net buyers and sellers
    across five sessions, using the same taxonomy as the daily.
  * FIRST APPEARANCES — names and clients with no reported deal in the prior
    four weeks. A new entrant deserves more attention than a familiar one.

Two editions, matching the daily report's convention:

  * FOCUS — the email body. Each section scoped to its own largest companies by
    market capitalisation, for the same reason the daily is: a bulk deal needs
    0.5% of traded quantity, so an index-membership filter would empty the body
    on most weeks, while a market-cap ranking of the week's actual names keeps
    the large-cap lens and always carries the biggest names present.

  * COMPREHENSIVE — every deal in the week, unfiltered, rendered to an attached
    PDF alongside the raw CSVs. The body is a lens onto that record, never a
    replacement for it.

Almost every aggregation and table renderer here is imported from
reports.daily_deals_report rather than reimplemented. That coupling is
deliberate: two copies of "value in rupee crore, buy side or sell side
whichever is larger" is how a weekly total stops reconciling against the five
dailies it summarises. The daily module is left untouched, so the 10:00 IST
email carries no regression risk from this file existing.

Env vars required:
  SUPABASE_URL, SUPABASE_KEY
  SMTP_USER           — sending address (bac@brindco.com)
  SMTP_PASSWORD       — Google app-specific password for SMTP_USER
  REPORT_RECIPIENTS   — the same bac-reports@brindco.com Google Group the daily
                        reports use. Readers are added in Google Workspace, not
                        by editing this secret.

Optional:
  REPORT_WEEKLY_SENDER_NAME     — defaults to "BAC Weekly Deals"
  REPORT_WEEKLY_FOCUS_TOP_N     — companies the body covers (default 10)
  REPORT_WEEKLY_LOOKBACK_WEEKS  — first-appearance lookback (default 4)
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from html import escape as _e

import pandas as pd

from reports import client_class as cc
from reports import design as d
from reports.pdf_render import render_pdf
from utils import security_master as sm

# ── Shared with the daily report ─────────────────────────────────────────────
# Imported, not copied. Every name below is scope-agnostic: it takes frames of
# deals and knows nothing about whether those frames are one session or five.
from reports.daily_deals_report import (
    # formatters and glyphs
    _fmt_cr, _fmt_mcap, _fmt_qty, _index_badge, _tag_html, _strip_html, _classify,
    # enrichment and aggregation
    _enrich_bulk, _enrich_block, _enrich_short,
    _topline, _sym_rows, _client_rows,
    # focus scoping
    _all_symbols, _focus_scope, _focus_union, _filter_symbols,
    # renderers
    _section, _sym_table_html, _class_compartments_html, _fmt_notional,
    # message assembly
    _build_message, _send_email, _csv_bytes,
    # palette aliases
    _INK_SOFT, _STONE, _NAVY, _NAVY_SOFT, _GOOD, _BAD, _WARN,
    # shared caveat text
    _CLASS_CAVEAT,
)

logger = logging.getLogger("nse.report.weekly")

REPORT_TYPE = "weekly_deals_email"

EDITION_FULL = "full"
EDITION_FOCUS = "focus"

# How many companies the email body covers per section, largest first.
FOCUS_TOP_N = int(os.environ.get("REPORT_WEEKLY_FOCUS_TOP_N", "10"))

# A client-symbol pair must appear on at least this many sessions before it
# counts as a flow rather than a trade. Two is the lowest number that can tell
# the two apart at all.
PERSIST_MIN_SESSIONS = 2

# Fraction of a pair's gross weekly quantity that has to be net one-way before
# it is called accumulation or distribution. Below this it is a round trip: an
# HFT desk in and out of the same name all week nets to roughly nothing, and
# that is churn, not a position.
PERSIST_MIN_CONVICTION = 0.25

# How far back "first appearance" looks. Four weeks is long enough that a name
# trading roughly monthly is not announced as new every time it shows up.
LOOKBACK_WEEKS = int(os.environ.get("REPORT_WEEKLY_LOOKBACK_WEEKS", "4"))

# Rupee crore a first appearance must carry before the email body reports it.
#
# Without a floor the section is not a signal, it is a census of the long tail.
# On the week of 17-21 Aug 2026 it flagged 71 of 121 names (59%) and 124 of 225
# clients (55%), and 108 of those 124 clients traded exactly one name on exactly
# one session — a single block participant that will never be seen again. That is
# the structure of the feed, not news. At 100 crore the same week reports 21
# names, and every name that moved real money survives.
#
# The floor is a lens, not a filter on the record: the comprehensive PDF passes
# zero and lists every absent name, and the body says how many it set aside.
FIRST_APPEARANCE_MIN_CR = float(os.environ.get("REPORT_WEEKLY_FIRST_MIN_CR", "100"))

# Rows per weekly-specific section in the email body. The PDF is uncapped.
BODY_ROW_CAP = 15

_DEAL_DATASETS = ("bulk_deals", "block_deals", "short_deals")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _fmt_signed_cr(v: float | None) -> str:
    """Signed rupee crore for a net figure, coloured by direction.

    Distinct from the daily's _fmt_cr, which prints the em-dash for anything at
    or below zero because there a value is a magnitude. Here zero and negative
    are both real answers — a net seller is not a missing value.
    """
    if v is None:
        return f'<span style="color:{_STONE};">{d.EM_DASH}</span>'
    v = float(v)
    if abs(v) < 0.05:
        return f'<span style="color:{_STONE};">flat</span>'
    color = _GOOD if v > 0 else _BAD
    sign = "+" if v > 0 else "&minus;"
    mag = abs(v)
    body = f"{mag:,.0f}" if mag >= 100 else f"{mag:.1f}"
    return (
        f'<span style="color:{color};font-weight:bold;'
        f'font-variant-numeric:tabular-nums;">{sign}&#8377;&nbsp;{body}</span>'
    )


def _fmt_signed_qty(n: float | None) -> str:
    if not n:
        return f'<span style="color:{_STONE};">flat</span>'
    color = _GOOD if n > 0 else _BAD
    sign = "+" if n > 0 else "&minus;"
    return (
        f'<span style="color:{color};font-variant-numeric:tabular-nums;">'
        f'{sign}{abs(int(n)):,}</span>'
    )


def _fmt_pct_delta(pct: float | None) -> str:
    """Week-on-week change.

    Deliberately not coloured good or bad: a heavier week of bulk deals is more
    turnover, not better news, and the semantic palette is reserved for things
    that actually carry a direction.
    """
    if pct is None:
        return f'<span style="color:{_STONE};">no prior week</span>'
    if abs(pct) < 0.5:
        return f'<span style="color:{_STONE};">level on the week before</span>'
    arrow = d.UP if pct > 0 else d.DOWN
    return (
        f'<span style="color:{_INK_SOFT};">{arrow}&nbsp;{abs(pct):.0f}% '
        f'on the week before</span>'
    )


def _pct_delta(cur: float, prev: float) -> float | None:
    """Percentage change, or None when the prior week has no base to divide by."""
    if not prev:
        return None
    return (float(cur) - float(prev)) / float(prev) * 100.0


def _house_date(dt: date) -> str:
    return dt.strftime("%a %d-%b-%Y")


def _house_range(start: date, end: date) -> str:
    """'24–28 Aug 2026', collapsing whatever the two dates share.

    A weekly masthead reading '24 Aug 2026 – 28 Aug 2026' spends its width on
    repetition; the reader needs the span.
    """
    if start == end:
        return start.strftime("%d %b %Y")
    if start.year != end.year:
        return f'{start.strftime("%d %b %Y")}&ndash;{end.strftime("%d %b %Y")}'
    if start.month != end.month:
        return f'{start.strftime("%d %b")}&ndash;{end.strftime("%d %b %Y")}'
    return f'{start.strftime("%d")}&ndash;{end.strftime("%d %b %Y")}'


def _plain_range(start: date, end: date) -> str:
    """The same span with an ASCII dash, for a subject line."""
    return _strip_html(_house_range(start, end)).replace("–", "-")


def _slug_range(start: date, end: date) -> str:
    return f"{start.isoformat()}_to_{end.isoformat()}"


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


# ─── Week window ──────────────────────────────────────────────────────────────

def _previous_trading_day(today: date) -> date:
    from utils.helpers import is_trading_day
    dt = today - timedelta(days=1)
    while not is_trading_day(dt):
        dt -= timedelta(days=1)
    return dt


def _week_window(anchor: date) -> tuple[date, date, list[date]]:
    """The Mon–Fri week containing `anchor`, and the trading days inside it.

    Returns (start, end, trading_days) where start and end are the first and
    last *trading* days of that week, not Monday and Friday flat. A week whose
    Monday was a holiday must report itself as Tue–Fri, or the dateline claims
    a session that never happened.
    """
    from utils.helpers import iter_trading_days
    monday = anchor - timedelta(days=anchor.weekday())
    friday = monday + timedelta(days=4)
    days = list(iter_trading_days(monday, friday))
    if not days:
        # A fully-closed week. Fall back to the calendar span so the report can
        # still state that nothing traded.
        return monday, friday, []
    return days[0], days[-1], days


def _resolve_window(today: date, week_of: date | None) -> tuple[date, date, list[date]]:
    """Which week this run reports on.

    Anchored on the previous trading day rather than on `today`, so the Saturday
    and Sunday runs both resolve to the week that has just finished, and a
    Monday re-run resolves to that same week rather than to an empty current
    one.
    """
    anchor = week_of or _previous_trading_day(today)
    start, end, days = _week_window(anchor)
    logger.info(
        "Week window: %s to %s (%d trading day%s: %s)",
        start, end, len(days), "" if len(days) == 1 else "s",
        ", ".join(dt.strftime("%a %d") for dt in days) or "none",
    )
    return start, end, days


def _prior_window(start: date) -> tuple[date, date, list[date]]:
    """The trading week before the reported one, for the week-on-week base."""
    return _week_window(start - timedelta(days=7))


# ─── Data fetch ───────────────────────────────────────────────────────────────

def _fetch_range(table: str, start: date, end: date,
                 columns: str = "*") -> pd.DataFrame:
    """Every row in [start, end], paginated.

    Range-scanned rather than looped per trading day: five single-day queries
    per table is fifteen round trips to say the same thing, and idx_bulk_date
    already covers the range predicate.
    """
    from database.client import get_client
    client = get_client()
    page, page_size, out = 0, 1000, []
    while True:
        resp = (
            client.table(table).select(columns)
            .gte("deal_date", start.isoformat())
            .lte("deal_date", end.isoformat())
            .order("deal_date")
            .range(page * page_size, (page + 1) * page_size - 1)
            .execute()
        )
        chunk = resp.data or []
        out.extend(chunk)
        if len(chunk) < page_size:
            break
        page += 1
    return pd.DataFrame(out)


def _fetch_lookback_keys(start: date, weeks: int) -> tuple[set[str], set[str]]:
    """Distinct symbols and client names in the `weeks` before `start`.

    Two columns only: this window is four times the size of the reported week
    and nothing else about those rows is used. Fails soft — without the
    baseline the first-appearance section is suppressed rather than allowed to
    declare every name in the week a new entrant.
    """
    lb_end = start - timedelta(days=1)
    lb_start = start - timedelta(weeks=weeks)
    symbols: set[str] = set()
    clients: set[str] = set()
    for table in ("bulk_deals", "block_deals"):
        try:
            df = _fetch_range(table, lb_start, lb_end, columns="symbol,client_name")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lookback fetch failed for %s: %s", table, exc)
            return set(), set()
        if df.empty:
            continue
        if "symbol" in df.columns:
            symbols |= set(df["symbol"].dropna().astype(str))
        if "client_name" in df.columns:
            clients |= set(df["client_name"].dropna().astype(str).str.strip())
    logger.info(
        "Lookback baseline %s to %s: %d symbols, %d clients",
        lb_start, lb_end, len(symbols), len(clients),
    )
    return symbols, clients


# ─── Data-integrity guard ─────────────────────────────────────────────────────

def _scrape_health(start: date, end: date) -> list[str]:
    """Datasets empty across the whole week *because collection failed*.

    Same rule as the daily's guard, widened to the range: a dataset is degraded
    when it has no rows anywhere in the week AND its most recent scrape did not
    succeed. A genuinely quiet week and a broken feed render identically, and
    only the scrape log can separate them.

    Never raises. A broken guard must not be able to stop the report.
    """
    from database.client import get_client
    degraded: list[str] = []
    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Scrape-health check unavailable: %s", exc)
        return degraded

    for ds in _DEAL_DATASETS:
        try:
            got = (
                client.table(ds).select("id", count="exact")
                .gte("deal_date", start.isoformat())
                .lte("deal_date", end.isoformat())
                .limit(1).execute()
            )
            if (got.count or 0) > 0:
                continue
            last = client.table("scrape_run_log").select("status").eq(
                "dataset", ds).order("start_time", desc=True).limit(1).execute()
            status = last.data[0]["status"] if last.data else None
            if status != "success":
                degraded.append(ds)
                logger.error(
                    "%s: no rows for %s..%s and last scrape status=%s",
                    ds, start, end, status,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Scrape-health check failed for %s: %s", ds, exc)
    return degraded


def _missing_sessions(
    trading_days: list[date], bulk: pd.DataFrame, block: pd.DataFrame,
) -> list[date]:
    """Trading days inside the week with no bulk *and* no block row.

    A weekly-only integrity signal. A single missing session is invisible in a
    daily report — that day's email simply said "none" — but across five
    sessions a hole in the middle of the week is obvious and worth stating,
    because every weekly total is understated by whatever it swallowed.
    """
    if not trading_days:
        return []
    seen: set[date] = set()
    for df in (bulk, block):
        if df is not None and not df.empty and "deal_date" in df.columns:
            seen |= {
                pd.to_datetime(v).date()
                for v in df["deal_date"].dropna().unique()
            }
    return [dt for dt in trading_days if dt not in seen]


def _degraded_card(degraded: list[str], missing: list[date]) -> dict | None:
    """The lead callout when the week's record is known to be incomplete."""
    if not degraded and not missing:
        return None
    parts: list[str] = []
    if degraded:
        names = ", ".join(x.replace("_", " ") for x in degraded)
        parts.append(
            f"The {names} feed could not be collected for any session this "
            f"week, so those sections are blank because the data is missing, "
            f"not because there were no deals."
        )
    if missing:
        days = ", ".join(dt.strftime("%a %d %b") for dt in missing)
        parts.append(
            f"No bulk or block rows landed for {days} — {_n(len(missing), 'trading session')} "
            f"of the week. Every weekly total below is understated by whatever "
            f"those sessions carried."
        )
    return {
        "title": "Week incomplete &mdash; do not read empty sections as zero",
        "body": " ".join(parts) + " Treat the affected figures as provisional pending a re-run.",
    }


# ─── Idempotency ──────────────────────────────────────────────────────────────
# Keyed on the week's last trading day, so a Saturday and a Sunday retry claim
# the same slot. report_log's unique constraint is on (report_type,
# report_date); the distinct report_type keeps the weekly from colliding with
# the daily that also ran on that Friday.

def _claim_slot(report_date: date, recipients: list[str]) -> bool:
    from database.client import get_client
    try:
        get_client().table("report_log").insert({
            "report_type": REPORT_TYPE,
            "report_date": report_date.isoformat(),
            "status": "pending",
            "recipients": ",".join(recipients),
        }).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "Weekly slot for %s already claimed (%s) — exiting.",
            report_date, type(exc).__name__,
        )
        return False


def _mark_sent(report_date: date) -> None:
    from database.client import get_client
    get_client().table("report_log").update({
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).eq("report_type", REPORT_TYPE).eq("report_date", report_date.isoformat()).execute()


def _mark_failed(report_date: date, err: str) -> None:
    from database.client import get_client
    try:
        get_client().table("report_log").update({
            "status": "failed",
            "error_message": err[:2000],
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }).eq("report_type", REPORT_TYPE).eq("report_date", report_date.isoformat()).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not mark failed: %s", exc)


# ─── Weekly aggregations ──────────────────────────────────────────────────────

def _side_value_cr(df: pd.DataFrame) -> float:
    """Rupee crore for a frame, counting a crossed deal once.

    Mirrors the per-symbol max(buy, sell) convention inside the daily's
    _topline. It is restated here rather than imported because _topline's
    version is a closure over its own frames; the rule is the thing that must
    not diverge, and any change to it belongs in both places.
    """
    if df is None or df.empty or "value_cr" not in df.columns:
        return 0.0
    if "buy_sell" not in df.columns:
        return round(float(df["value_cr"].sum()), 2)
    sym_val = df.groupby(["symbol", "buy_sell"])["value_cr"].sum().reset_index()
    return round(float(sym_val.groupby("symbol")["value_cr"].max().sum()), 2)


def _daily_trend(
    bulk: pd.DataFrame, block: pd.DataFrame, short: pd.DataFrame,
    trading_days: list[date],
) -> pd.DataFrame:
    """One row per trading day: bulk value, block value, deal and name counts.

    Every trading day in the window gets a row, including the ones with no
    deals. A quiet Wednesday has to be visibly quiet — dropping the row would
    let the chart imply the week ran Mon, Tue, Thu, Fri.
    """
    rows = []
    for dt in trading_days:
        def _slice(df: pd.DataFrame) -> pd.DataFrame:
            if df is None or df.empty or "deal_date" not in df.columns:
                return pd.DataFrame()
            mask = pd.to_datetime(df["deal_date"]).dt.date == dt
            return df[mask]

        b, k, s = _slice(bulk), _slice(block), _slice(short)
        rows.append({
            "deal_date": dt,
            "bulk_cr":   _side_value_cr(b),
            "block_cr":  _side_value_cr(k),
            "bulk_deals":  0 if b.empty else len(b),
            "block_deals": 0 if k.empty else len(k),
            "short_pos":   0 if s.empty else len(s),
            "names": len(_all_symbols(b, k)),
        })
    trend = pd.DataFrame(rows)
    if not trend.empty:
        trend["total_cr"] = trend["bulk_cr"] + trend["block_cr"]
        trend["deals"] = trend["bulk_deals"] + trend["block_deals"]
    return trend


def _combined(bulk: pd.DataFrame, block: pd.DataFrame) -> pd.DataFrame:
    """Bulk and block in one frame, tagged by kind.

    Both feeds describe the same act — a large negotiated transfer — and a week
    of position building shows up across both. Keeping them apart here would
    split a client that used the block window on Monday and bulk prints on
    Thursday into two half-signals.
    """
    parts = []
    for df, kind in ((bulk, "bulk"), (block, "block")):
        if df is not None and not df.empty:
            tmp = df.copy()
            tmp["_kind"] = kind
            parts.append(tmp)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    if "deal_date" in out.columns:
        out["deal_date"] = pd.to_datetime(out["deal_date"]).dt.date
    return out


def _persistent_flows(bulk: pd.DataFrame, block: pd.DataFrame) -> pd.DataFrame:
    """Client-symbol pairs traded on more than one session in the week.

    Columns: client_name, symbol, security_name, class, sessions, buy_qty,
    sell_qty, net_qty, buy_cr, sell_cr, net_cr, gross_cr, conviction, side.

    `conviction` is |net| / gross quantity — the share of the week's activity
    that did not cancel itself out. It is the column that separates a position
    from a round trip, and it is carried on the frame rather than filtered here
    so the renderer can report how many pairs it set aside as churn instead of
    silently dropping them.
    """
    all_ = _combined(bulk, block)
    if all_.empty or "client_name" not in all_.columns:
        return pd.DataFrame()

    all_ = all_.copy()
    all_["client_name"] = all_["client_name"].astype(str).str.strip()

    grp = all_.groupby(["client_name", "symbol"], dropna=False)
    rows = []
    for (client, symbol), g in grp:
        sessions = int(g["deal_date"].nunique()) if "deal_date" in g.columns else 1
        if sessions < PERSIST_MIN_SESSIONS:
            continue
        buys, sells = g[g["buy_sell"] == "B"], g[g["buy_sell"] == "S"]
        bq, sq = int(buys["quantity"].sum()), int(sells["quantity"].sum())
        bv, sv = float(buys["value_cr"].sum()), float(sells["value_cr"].sum())
        gross_qty = bq + sq
        if gross_qty <= 0:
            continue
        net_qty = bq - sq
        sec = g["security_name"].dropna()
        rows.append({
            "client_name": client,
            "symbol": str(symbol),
            "security_name": str(sec.iloc[0]) if not sec.empty else str(symbol),
            "class": _classify(client),
            "sessions": sessions,
            "buy_qty": bq, "sell_qty": sq, "net_qty": net_qty,
            "buy_cr": round(bv, 2), "sell_cr": round(sv, 2),
            "net_cr": round(bv - sv, 2), "gross_cr": round(bv + sv, 2),
            "conviction": abs(net_qty) / gross_qty,
            "side": "accumulating" if net_qty > 0 else ("distributing" if net_qty < 0 else "round trip"),
            "mcap_cr": sm.market_cap_cr(str(symbol)) or 0.0,
        })

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    # Net rupee size first, session count as the tiebreaker — NOT the other way
    # round. Ranking on sessions reads well in the abstract, but on real weeks
    # the spread in size is two orders of magnitude wider than the spread in
    # session count, so it buries the week's largest flow: the 2026-08-17 week
    # led with a four-session, 4-crore FII position while a 914-crore
    # infrastructure exit built over two sessions sat fourth. Persistence is
    # what qualifies a row for this table at all; size is what orders it, as it
    # does in every other table in both reports. The session count stays a
    # column, so a reader can still see which flows were the most patient.
    out["_mag"] = out["net_cr"].abs()
    return (
        out.sort_values(["_mag", "sessions"], ascending=[False, False])
        .drop(columns="_mag")
        .reset_index(drop=True)
    )


def _one_way(persist: pd.DataFrame) -> pd.DataFrame:
    if persist is None or persist.empty:
        return pd.DataFrame()
    return persist[persist["conviction"] >= PERSIST_MIN_CONVICTION].copy()


def _round_trips(persist: pd.DataFrame) -> pd.DataFrame:
    if persist is None or persist.empty:
        return pd.DataFrame()
    return persist[persist["conviction"] < PERSIST_MIN_CONVICTION].copy()


def _class_net_flows(bulk: pd.DataFrame, block: pd.DataFrame) -> pd.DataFrame:
    """Buy, sell and net rupee crore per client class for the week.

    Columns: class, buy_cr, sell_cr, net_cr, gross_cr, clients, names, deals.
    Ordered by cc.CLASS_ORDER so the table reads the same way every week
    regardless of which classes happened to be active.
    """
    all_ = _combined(bulk, block)
    if all_.empty or "client_name" not in all_.columns:
        return pd.DataFrame()

    all_ = all_.copy()
    all_["client_name"] = all_["client_name"].astype(str).str.strip()
    all_["class"] = all_["client_name"].apply(_classify)

    rows = []
    for tag in cc.CLASS_ORDER:
        g = all_[all_["class"] == tag]
        if g.empty:
            continue
        bv = float(g.loc[g["buy_sell"] == "B", "value_cr"].sum())
        sv = float(g.loc[g["buy_sell"] == "S", "value_cr"].sum())
        rows.append({
            "class": tag,
            "buy_cr": round(bv, 2), "sell_cr": round(sv, 2),
            "net_cr": round(bv - sv, 2), "gross_cr": round(bv + sv, 2),
            "clients": int(g["client_name"].nunique()),
            "names": int(g["symbol"].nunique()),
            "deals": int(len(g)),
        })
    return pd.DataFrame(rows)


def _class_daily_flows(
    bulk: pd.DataFrame, block: pd.DataFrame, trading_days: list[date],
) -> pd.DataFrame:
    """One row per (client class, session): buy, sell and net rupee crore.

    The week-total table says FIIs sold 5,476 crore net. It cannot say whether
    that was one Wednesday block or four days of steady selling, and those are
    different events: the first is a single holder exiting, the second is a
    position being unwound into the market. This is the frame that separates
    them.

    Every class that traded at all gets a row for every trading day, including
    the days it did nothing. A class absent on Wednesday must render as a gap at
    the centre line, not as a missing column that shifts the week's shape left.
    """
    all_ = _combined(bulk, block)
    if all_.empty or "client_name" not in all_.columns:
        return pd.DataFrame()

    all_ = all_.copy()
    all_["client_name"] = all_["client_name"].astype(str).str.strip()
    all_["class"] = all_["client_name"].apply(_classify)

    rows = []
    for tag in cc.CLASS_ORDER:
        in_class = all_[all_["class"] == tag]
        if in_class.empty:
            continue
        for dt in trading_days:
            g = in_class[in_class["deal_date"] == dt]
            bv = float(g.loc[g["buy_sell"] == "B", "value_cr"].sum()) if not g.empty else 0.0
            sv = float(g.loc[g["buy_sell"] == "S", "value_cr"].sum()) if not g.empty else 0.0
            rows.append({
                "class": tag, "deal_date": dt,
                "buy_cr": round(bv, 2), "sell_cr": round(sv, 2),
                "net_cr": round(bv - sv, 2),
                "deals": int(len(g)),
            })
    return pd.DataFrame(rows)


def _first_appearances(
    bulk: pd.DataFrame, block: pd.DataFrame,
    prior_symbols: set[str], prior_clients: set[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Symbols and clients in this week with no deal in the lookback window.

    Both frames come back empty when the baseline is empty. An absent baseline
    is not evidence that everything is new, and rendering it as such would make
    the section wrong in exactly the direction that reads as a signal.
    """
    if not prior_symbols and not prior_clients:
        return pd.DataFrame(), pd.DataFrame()

    all_ = _combined(bulk, block)
    if all_.empty:
        return pd.DataFrame(), pd.DataFrame()
    all_ = all_.copy()
    all_["client_name"] = all_["client_name"].astype(str).str.strip()

    new_syms = pd.DataFrame()
    if prior_symbols:
        agg = (
            all_.groupby(["symbol", "security_name"], dropna=False)
            .agg(value_cr=("value_cr", "sum"),
                 deals=("value_cr", "size"),
                 sessions=("deal_date", "nunique"))
            .reset_index()
        )
        agg = agg[~agg["symbol"].astype(str).isin(prior_symbols)]
        if not agg.empty:
            agg["mcap_cr"] = agg["symbol"].apply(lambda s: sm.market_cap_cr(str(s)) or 0.0)
            new_syms = agg.sort_values("value_cr", ascending=False).reset_index(drop=True)

    new_clients = pd.DataFrame()
    if prior_clients:
        agg = (
            all_.groupby("client_name", dropna=False)
            .agg(value_cr=("value_cr", "sum"),
                 deals=("value_cr", "size"),
                 names=("symbol", "nunique"),
                 sessions=("deal_date", "nunique"))
            .reset_index()
        )
        agg = agg[~agg["client_name"].isin(prior_clients)]
        if not agg.empty:
            agg["class"] = agg["client_name"].apply(_classify)
            new_clients = agg.sort_values("value_cr", ascending=False).reset_index(drop=True)

    return new_syms, new_clients


def _short_week_rows(short: pd.DataFrame) -> pd.DataFrame:
    """Short positions aggregated per symbol across the week.

    short_deals is unique on (deal_date, symbol), so a week carries up to five
    rows per name. Listing them raw would rank a name that was shorted lightly
    every day below one shorted heavily once, which is the wrong way round for
    a weekly read.
    """
    if short is None or short.empty:
        return pd.DataFrame()
    df = short.copy()
    df["quantity"] = df["quantity"].fillna(0).astype("int64")
    agg = (
        df.groupby(["symbol", "security_name"], dropna=False)
        .agg(quantity=("quantity", "sum"),
             sessions=("deal_date", "nunique"),
             peak=("quantity", "max"))
        .reset_index()
    )
    agg["notional"] = [
        (sm.last_price(str(s)) or 0) * float(q or 0)
        for s, q in zip(agg["symbol"], agg["quantity"])
    ]
    return agg.sort_values("quantity", ascending=False).reset_index(drop=True)


def _wow(metrics: dict, prior: dict) -> dict:
    """Week-on-week percentage change for the three headline figures."""
    return {
        "bulk_value":  _pct_delta(metrics["bulk"]["value_cr"],  prior["bulk"]["value_cr"]),
        "block_value": _pct_delta(metrics["block"]["value_cr"], prior["block"]["value_cr"]),
        "short_pos":   _pct_delta(metrics["short"]["deals"],    prior["short"]["deals"]),
    }


# ─── Derived-frame bundle ─────────────────────────────────────────────────────

def _derive(
    bulk: pd.DataFrame, block: pd.DataFrame, short: pd.DataFrame,
    trading_days: list[date],
    prior_symbols: set[str], prior_clients: set[str],
) -> dict:
    """Runs every aggregation over one scope of the week's deals.

    Called twice per run — once for the comprehensive edition and once for the
    focused email body — so the two cannot drift apart in how they compute a
    total. Mirrors the daily's _derive, extended with the weekly-only frames.
    """
    persist = _persistent_flows(bulk, block)
    class_daily = _class_daily_flows(bulk, block, trading_days)
    first_syms, first_clients = _first_appearances(
        bulk, block, prior_symbols, prior_clients
    )
    return {
        "bulk": bulk, "block": block, "short": short,
        "metrics":     _topline(bulk, block, short),
        "bulk_sym":    _sym_rows(bulk),
        "block_sym":   _sym_rows(block),
        "short_week":  _short_week_rows(short),
        "bulk_client": _client_rows(bulk),
        "block_client": _client_rows(block),
        "trend":       _daily_trend(bulk, block, short, trading_days),
        "persist":     persist,
        "one_way":     _one_way(persist),
        "round_trips": _round_trips(persist),
        "class_flow":  _class_net_flows(bulk, block),
        "class_daily": class_daily,
        "cum_class":   _cumulative_class_flow(class_daily, trading_days),
        "concentration": _concentration(bulk, block),
        "class_sym":   _class_symbol_values(bulk, block),
        "first_syms":    first_syms,
        "first_clients": first_clients,
    }


# ─── Weekly highlights ────────────────────────────────────────────────────────

def _weekly_highlights(
    bulk: pd.DataFrame, block: pd.DataFrame,
    trend: pd.DataFrame, one_way: pd.DataFrame, class_flow: pd.DataFrame,
) -> list[dict]:
    """Up to five cards, each answering a question a daily report cannot.

    Card one is the week's largest print, which the daily would also have found.
    Everything after it is week-shaped: the largest position built, the largest
    unwound, where in the week the money actually moved, and which class ended
    five sessions furthest from flat.
    """
    cards: list[dict] = []
    all_ = _combined(bulk, block)
    if not all_.empty and "value_cr" in all_.columns:
        all_ = all_[all_["value_cr"] > 0]

    # Four cards that all name the same company are one card. The daily tracks
    # this the same way, and the weekly needs it more: the largest print of the
    # week and the largest position of the week are frequently the same name,
    # because a block cross and the flow around it come from the same event.
    used_syms: set[str] = set()

    # ── Card 1 — largest single print of the week (lead) ──────────────────────
    if not all_.empty:
        top = all_.nlargest(1, "value_cr").iloc[0]
        sym = str(top["symbol"])
        used_syms.add(sym)
        client = str(top.get("client_name", "")).strip() or "Unknown"
        side = "bought" if top["buy_sell"] == "B" else "sold"
        qty_m = int(top["quantity"]) / 1e6
        kind = str(top.get("_kind", "bulk"))
        sec = str(top.get("security_name", sym) or sym)
        when = top.get("deal_date")
        day = when.strftime("%A") if isinstance(when, date) else "the week"
        cards.append({
            "lead": True,
            "tag": "Largest print of the week",
            "tag_right": "by value",
            "title": (
                f'{_e(sym)} &nbsp;·&nbsp; '
                f'<span style="font-variant-numeric:tabular-nums;">'
                f'{_fmt_cr(float(top["value_cr"]))}</span>'
                f'<span style="font-size:18px; color:{_STONE}; font-style:italic; '
                f'font-weight:400;"> cr</span>'
            ),
            "body": (
                f'{_e(client)} {side} {qty_m:.2f}&nbsp;million shares of '
                f'<em>{_e(sec)}</em> in a {kind} deal on {day} &mdash; '
                f'the largest rupee print of the week.'
            ),
        })

    # ── Cards 2 and 3 — the week's largest position built and unwound ─────────
    if one_way is not None and not one_way.empty:
        for direction, label, verb in (
            (1,  "the week&rsquo;s largest build &mdash;", "added"),
            (-1, "the week&rsquo;s largest exit &mdash;",  "reduced"),
        ):
            side_rows = one_way[
                ((one_way["net_cr"] > 0) if direction > 0 else (one_way["net_cr"] < 0))
                & ~one_way["symbol"].astype(str).isin(used_syms)
            ]
            if side_rows.empty:
                continue
            r = side_rows.iloc[side_rows["net_cr"].abs().argmax()]
            used_syms.add(str(r["symbol"]))
            net_m = abs(int(r["net_qty"])) / 1e6
            cards.append({
                "tag": label,
                "tag_color": _NAVY if direction > 0 else _WARN,
                "title": (
                    f'{_e(str(r["symbol"]))} &nbsp;'
                    f'<span style="color:{_STONE}; font-style:italic;">·</span>&nbsp; '
                    f'{_fmt_signed_cr(float(r["net_cr"]))}'
                    f'<span style="font-size:13px; color:{_STONE}; font-style:italic; '
                    f'font-weight:400;"> cr net</span>'
                ),
                "body": (
                    f'{_e(str(r["client_name"]))} {verb} '
                    f'{net_m:.2f}&nbsp;million shares net across '
                    f'{_n(int(r["sessions"]), "session")} &mdash; '
                    f'{r["conviction"] * 100:.0f}% of its traded quantity in the name '
                    f'was one-way, so this is a position, not a round trip.'
                ),
            })

    # ── Card 4 — where in the week the money moved ────────────────────────────
    if trend is not None and not trend.empty and float(trend["total_cr"].sum()) > 0:
        peak = trend.loc[trend["total_cr"].idxmax()]
        total = float(trend["total_cr"].sum())
        share = float(peak["total_cr"]) / total * 100 if total else 0.0
        quiet = trend.loc[trend["total_cr"].idxmin()]
        cards.append({
            "tag": "the week was not evenly spread &mdash;",
            "tag_color": _NAVY_SOFT,
            "title": (
                f'{peak["deal_date"].strftime("%A")} &nbsp;'
                f'<span style="color:{_STONE}; font-style:italic;">·</span>&nbsp; '
                f'<span style="font-variant-numeric:tabular-nums;">{share:.0f}%</span>'
                f'<span style="font-size:13px; color:{_STONE}; font-style:italic; '
                f'font-weight:400;"> of the week</span>'
            ),
            "body": (
                f'{_fmt_cr(float(peak["total_cr"]))}&thinsp;cr of the week&rsquo;s '
                f'{_fmt_cr(total)}&thinsp;cr printed on '
                f'{peak["deal_date"].strftime("%a %d %b")} across '
                f'{_n(int(peak["deals"]), "deal")}; the quietest session, '
                f'{quiet["deal_date"].strftime("%a %d %b")}, carried '
                f'{_fmt_cr(float(quiet["total_cr"]))}&thinsp;cr.'
            ),
        })

    # ── Card 5 — the class that ended furthest from flat ──────────────────────
    if class_flow is not None and not class_flow.empty:
        r = class_flow.iloc[class_flow["net_cr"].abs().argmax()]
        if abs(float(r["net_cr"])) > 1:
            direction = "net buyer" if float(r["net_cr"]) > 0 else "net seller"
            cards.append({
                "tag": "who was on which side &mdash;",
                "tag_color": _NAVY,
                "title": (
                    f'{_e(str(r["class"]))} &nbsp;'
                    f'<span style="color:{_STONE}; font-style:italic;">·</span>&nbsp; '
                    f'{_fmt_signed_cr(float(r["net_cr"]))}'
                    f'<span style="font-size:13px; color:{_STONE}; font-style:italic; '
                    f'font-weight:400;"> cr net</span>'
                ),
                "body": (
                    f'{cc.CLASS_LABEL.get(str(r["class"]), str(r["class"]))} finished the '
                    f'week the largest {direction} of the reported classes &mdash; '
                    f'{_n(int(r["clients"]), "client")} across '
                    f'{_n(int(r["names"]), "name")}.'
                ),
            })

    return cards[:5]


def _highlights_html(cards: list[dict]) -> str:
    """The 'things that matter' gold callout, in the week's voice."""
    if not cards:
        return ""
    items = [
        (_strip_html(c.get("title", "")), _strip_html(c.get("body", "")))
        for c in cards[:4]
    ]
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background-color:{d.GOLD_PALE};border-left:4px solid {d.GOLD};">'
        f'<tr><td style="padding:12px 14px;">'
        f'{d.caption_title("Things that mattered this week")}'
        f'{d.numbered_list(items)}'
        f'</td></tr></table>'
    )


# ─── Weekly section renderers ─────────────────────────────────────────────────

def _count_track(deals: int, max_deals: float, bar_px: int) -> str:
    """The thin second track under a session's value bar: how many deals.

    Value and count answer different questions and the week they disagree is
    the week worth noticing — 20 Aug 2026 carried the most deals of the week
    (185) but ranked fourth by value, which is a session of small-ticket churn
    and reads as a quiet day if only the value bar is drawn.

    Drawn as a bar rather than a line: a polyline needs SVG, and Outlook's Word
    engine renders none. Deliberately 4px and grey so it stays subordinate to
    the value bars above it — this is a cross-check, not a co-headline.
    """
    if not deals or max_deals <= 0:
        return ""
    w = max(2, int(round(deals / max_deals * bar_px)))
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;"><tr>'
        f'<td width="{w}" bgcolor="{d.RULE_STRONG}" style="width:{w}px;height:4px;'
        f'font-size:0;line-height:0;">&nbsp;</td></tr></table>'
    )


def _trend_chart(trend: pd.DataFrame) -> str:
    """Session-by-session bars, bulk and block stacked.

    Built from nested table cells with bgcolor, like the daily's chart and for
    the same reasons: an <img> is blocked by Outlook until the reader opts in,
    and a CSS-width bar on a div is ignored by Word. Stacked rather than
    totalled because the split is the interesting part — a week that was all
    block window is a different week from one that was all bulk prints.
    """
    if trend is None or trend.empty:
        return ""
    max_val = float(trend["total_cr"].max())
    if max_val <= 0:
        return ""
    # Deal count rides a scale of its own. Sharing the value scale would make
    # the count track meaningless — the two quantities have no common unit —
    # and the whole point of drawing it is that the two shapes disagree.
    max_deals = float(trend["deals"].max()) if "deals" in trend.columns else 0.0

    BAR_PX = 268
    rows = []
    for r in trend.itertuples():
        total = float(r.total_cr)
        bulk_w = int(float(r.bulk_cr) / max_val * BAR_PX)
        block_w = int(float(r.block_cr) / max_val * BAR_PX)
        # A session with deals must show something, or a small day reads as a
        # closed one. A session with nothing must show nothing.
        if total > 0 and bulk_w + block_w == 0:
            bulk_w = 2 if float(r.bulk_cr) >= float(r.block_cr) else 0
            block_w = 0 if bulk_w else 2

        segs = ""
        if bulk_w:
            segs += (
                f'<td width="{bulk_w}" bgcolor="{_NAVY}" style="width:{bulk_w}px;'
                f'height:13px;font-size:0;line-height:0;">&nbsp;</td>'
            )
        if block_w:
            segs += (
                f'<td width="{block_w}" bgcolor="{d.GOLD}" style="width:{block_w}px;'
                f'height:13px;font-size:0;line-height:0;">&nbsp;</td>'
            )
        if not segs:
            segs = (
                f'<td style="{d.font(10.5, color=_STONE, italic=True)}'
                f'height:13px;padding-left:2px;">no reported deals</td>'
            )

        rows.append(
            f'<tr>'
            f'<td width="86" align="right" style="width:86px;'
            f'{d.font(11.5, weight="bold")}padding:3px 9px 3px 0;white-space:nowrap;">'
            f'{r.deal_date.strftime("%a %d")}</td>'
            f'<td style="padding:3px 0;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="border-collapse:collapse;"><tr>{segs}</tr></table>'
            f'{_count_track(int(r.deals), max_deals, BAR_PX)}</td>'
            f'<td width="86" style="width:86px;{d.font(11.5, color=_INK_SOFT)}'
            f'padding:3px 0 3px 9px;white-space:nowrap;">'
            f'{_fmt_cr(total)}&nbsp;cr</td>'
            f'<td width="74" align="right" style="width:74px;'
            f'{d.font(10.5, color=_STONE)}padding:3px 0;white-space:nowrap;">'
            f'{int(r.deals)}&nbsp;deals</td>'
            f'</tr>'
        )

    legend = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
        f'<tr>'
        f'<td width="10" bgcolor="{_NAVY}" style="width:10px;height:10px;'
        f'font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10.5, color=_INK_SOFT)}padding:0 14px 0 5px;">bulk</td>'
        f'<td width="10" bgcolor="{d.GOLD}" style="width:10px;height:10px;'
        f'font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10.5, color=_INK_SOFT)}padding:0 14px 0 5px;">block</td>'
        f'<td width="10" bgcolor="{d.RULE_STRONG}" style="width:10px;height:5px;'
        f'font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10.5, color=_INK_SOFT)}padding:0 0 0 5px;">'
        f'deal count, own scale</td>'
        f'</tr></table>'
    )

    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="border-collapse:collapse;">' + "".join(rows) + '</table>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0"><tr><td style="padding:12px 0 0 0;">{legend}</td></tr></table>'
    )


def _class_flow_chart(
    daily_class: pd.DataFrame, class_flow: pd.DataFrame,
    trading_days: list[date],
) -> str:
    """Net flow per client class per session, as a grid of diverging bars.

    Rows are classes in CLASS_ORDER, columns are the week's sessions. Each cell
    is a bar growing left from a centre rule for a net sell and right for a net
    buy, scaled against the largest absolute net anywhere in the grid, so cells
    are comparable both across a row and down a column.

    Direction is encoded twice — by side of the centre line and by colour — so
    the chart still reads for a red-green colour-blind reader and in greyscale.
    design.py reserves GOOD and BAD for exactly this kind of semantic pair, and
    the daily's buy/sell labels already use them, so a green bar here means the
    same thing as a green "buy" there.

    Built from nested table cells rather than a CSS bar or an image, for the
    same reasons as the session trend chart: Word ignores width on a div and
    Outlook blocks images until the reader opts in. Each half of a cell is a
    two-cell table (spacer + bar) instead of an aligned nested table, because
    align on a nested table is the one part of this construction Word gets
    wrong.
    """
    if daily_class is None or daily_class.empty or not trading_days:
        return ""

    peak = float(daily_class["net_cr"].abs().max())
    if peak <= 0:
        return ""

    HALF = 30          # px per side of the centre rule
    COL = 2 * HALF + 1 + 8      # bar pair + centre rule + cell padding
    # The axis tick is drawn taller than the bars so that the ticks in adjacent
    # rows read as one vertical baseline rather than as loose marks.
    TICK_H = 13
    BAR_H = 9
    net_by_class = {}
    if class_flow is not None and not class_flow.empty:
        net_by_class = dict(zip(class_flow["class"], class_flow["net_cr"]))

    def _cell(net: float) -> str:
        w = min(HALF, max(2, int(round(abs(net) / peak * HALF)))) if net else 0
        if not w:
            left = f'<td width="{HALF}" style="width:{HALF}px;"></td>'
            right = f'<td width="{HALF}" style="width:{HALF}px;"></td>'
        elif net < 0:
            left = (
                f'<td width="{HALF - w}" style="width:{HALF - w}px;"></td>'
                f'<td width="{w}" bgcolor="{_BAD}" style="width:{w}px;height:{BAR_H}px;'
                f'font-size:0;line-height:0;">&nbsp;</td>'
            )
            right = f'<td width="{HALF}" style="width:{HALF}px;"></td>'
        else:
            left = f'<td width="{HALF}" style="width:{HALF}px;"></td>'
            right = (
                f'<td width="{w}" bgcolor="{_GOOD}" style="width:{w}px;height:{BAR_H}px;'
                f'font-size:0;line-height:0;">&nbsp;</td>'
                f'<td width="{HALF - w}" style="width:{HALF - w}px;"></td>'
            )
        return (
            f'<td width="{COL}" style="width:{COL}px;padding:1px 4px;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="border-collapse:collapse;"><tr>{left}'
            f'<td width="1" bgcolor="{d.INK_FAINT}" style="width:1px;'
            f'height:{TICK_H}px;font-size:0;line-height:0;">&nbsp;</td>'
            f'{right}</tr></table></td>'
        )

    # ── Header: session labels over each column ──────────────────────────────
    head = (
        f'<tr><td width="52" style="width:52px;"></td>'
        + "".join(
            f'<td width="{COL}" align="center" style="width:{COL}px;'
            f'{d.font(9.5, color=d.INK_FAINT, weight="bold", ls=0.6, upper=True)}'
            f'padding:0 4px 5px 4px;white-space:nowrap;">{dt.strftime("%a %d")}</td>'
            for dt in trading_days
        )
        + f'<td width="84" align="right" style="width:84px;'
          f'{d.font(9.5, color=d.INK_FAINT, weight="bold", ls=0.6, upper=True)}'
          f'padding:0 0 5px 10px;white-space:nowrap;">Week net</td></tr>'
    )

    body_rows = ""
    for tag in cc.CLASS_ORDER:
        rows = daily_class[daily_class["class"] == tag]
        if rows.empty:
            continue
        by_day = dict(zip(rows["deal_date"], rows["net_cr"]))
        body_rows += (
            f'<tr>'
            f'<td width="52" style="width:52px;padding:1px 6px 1px 0;'
            f'white-space:nowrap;">{_tag_html(tag)}</td>'
            + "".join(_cell(float(by_day.get(dt, 0.0))) for dt in trading_days)
            + f'<td width="84" align="right" style="width:84px;{d.font(11.5)}'
              f'padding:1px 0 1px 10px;white-space:nowrap;">'
              f'{_fmt_signed_cr(float(net_by_class.get(tag, 0.0)))}</td>'
            f'</tr>'
        )

    legend = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td width="10" bgcolor="{_GOOD}" style="width:10px;height:10px;'
        f'font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10.5, color=_INK_SOFT)}padding:0 14px 0 5px;">'
        f'net buyer that session</td>'
        f'<td width="10" bgcolor="{_BAD}" style="width:10px;height:10px;'
        f'font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10.5, color=_INK_SOFT)}padding:0 14px 0 5px;">'
        f'net seller</td>'
        f'<td style="{d.font(10.5, color=_STONE, italic=True)}white-space:nowrap;">'
        f'full bar = {_fmt_cr(peak)}&nbsp;cr</td>'
        f'</tr></table>'
    )

    # Fixed total width, not 100%: with a percentage the table redistributes its
    # slack across the columns and the session labels stop sitting over the bars
    # they label, which is the one thing this grid has to get right.
    total_w = 52 + len(trading_days) * COL + 84
    return (
        f'<table role="presentation" width="{total_w}" cellpadding="0" '
        f'cellspacing="0" border="0" style="width:{total_w}px;'
        f'table-layout:fixed;border-collapse:collapse;">{head}{body_rows}</table>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0"><tr><td style="padding:12px 0 0 0;">{legend}</td></tr></table>'
    )


# ─── Shading tints ────────────────────────────────────────────────────────────
# Three steps per semantic colour. Email cannot be trusted with rgba or opacity
# — Word drops both — so a "lighter" bar has to be a different hex, not the same
# hex at lower alpha. Each step is the base colour mixed toward the paper, so a
# pale bar still reads as the same hue in greyscale.
_GOOD_MID  = "#4A8B74"
_GOOD_PALE = "#8FB9AA"
_BAD_MID   = "#C0675F"
_BAD_PALE  = "#D9A49F"


def _shade(side: str, conviction: float) -> str:
    """Bar colour for a flow: hue says direction, step says how one-way it was.

    Conviction is already floored at PERSIST_MIN_CONVICTION before a row reaches
    a table, so the pale step is never "no signal" — it is the weakest flow that
    still qualified. Three steps rather than a continuous ramp because a reader
    comparing two bars can rank three shades reliably and cannot rank ten.
    """
    buy = side == "buy"
    if conviction >= 0.75:
        return _GOOD if buy else _BAD
    if conviction >= 0.50:
        return _GOOD_MID if buy else _BAD_MID
    return _GOOD_PALE if buy else _BAD_PALE


def _bar_cell(width: int, color: str, height: int = 9) -> str:
    """One coloured segment. Zero width emits nothing, not a hairline."""
    if width <= 0:
        return ""
    return (
        f'<td width="{width}" bgcolor="{color}" style="width:{width}px;'
        f'height:{height}px;font-size:0;line-height:0;">&nbsp;</td>'
    )


def _spacer(width: int) -> str:
    if width <= 0:
        return ""
    return f'<td width="{width}" style="width:{width}px;"></td>'


def _diverging_bar(net: float, peak: float, half: int, *,
                   color_buy: str, color_sell: str,
                   tick_h: int = 13, bar_h: int = 9) -> str:
    """A bar growing from a centre rule: left for negative, right for positive.

    Returns the inner table only, so a caller can size the surrounding cell. The
    same construction as the class-flow grid — spacer and bar as siblings rather
    than an aligned nested table, because align on a nested table is the one
    part of this construction Word gets wrong.
    """
    w = min(half, max(2, int(round(abs(net) / peak * half)))) if (net and peak > 0) else 0
    if not w:
        left, right = _spacer(half), _spacer(half)
    elif net < 0:
        left = _spacer(half - w) + _bar_cell(w, color_sell, bar_h)
        right = _spacer(half)
    else:
        left = _spacer(half)
        right = _bar_cell(w, color_buy, bar_h) + _spacer(half - w)
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;"><tr>{left}'
        f'<td width="1" bgcolor="{d.INK_FAINT}" style="width:1px;height:{tick_h}px;'
        f'font-size:0;line-height:0;">&nbsp;</td>{right}</tr></table>'
    )


# ─── Cumulative FII vs DII ────────────────────────────────────────────────────

def _cumulative_class_flow(
    class_daily: pd.DataFrame, trading_days: list[date],
    tags: tuple[str, ...] = (cc.FII, cc.DII),
) -> pd.DataFrame:
    """Running net rupee crore per class across the week's sessions.

    One row per (class, session) carrying that session's net and the cumulative
    net up to and including it. Only the classes in `tags` — this is the
    foreign-versus-domestic question, and adding six more series to it turns the
    one chart on the desk that everybody reads into a chart nobody reads.

    A class with no row for a session carries its previous cumulative forward
    rather than resetting: a day a class did not trade is a day its position did
    not change, which is not the same as a day its position went to zero.
    """
    if class_daily is None or class_daily.empty or not trading_days:
        return pd.DataFrame()

    present = [t for t in tags if t in set(class_daily["class"])]
    if not present:
        return pd.DataFrame()

    rows = []
    for tag in present:
        sub = class_daily[class_daily["class"] == tag]
        by_day = dict(zip(sub["deal_date"], sub["net_cr"]))
        run = 0.0
        for dt in trading_days:
            net = float(by_day.get(dt, 0.0))
            run += net
            rows.append({
                "class": tag, "deal_date": dt,
                "net_cr": round(net, 2), "cum_cr": round(run, 2),
            })
    return pd.DataFrame(rows)


def _cumulative_flow_chart(cum: pd.DataFrame, trading_days: list[date]) -> str:
    """Foreign against domestic, as two cumulative tracks down the week.

    The class-flow grid in section III answers "who was on which side each day".
    It cannot answer "was this one exit or a build", because a reader cannot add
    five bars in their head. This chart is that sum, drawn: each row is a
    session, each track shows the running net to that point, and the two tracks
    pulling apart down the page is the week's whole story in one glance.

    Both tracks share one scale, so the gap between them reads as a quantity and
    not merely as a direction. Sign is encoded by side of the centre rule and by
    colour, the same pair used everywhere else in this report.
    """
    if cum is None or cum.empty or not trading_days:
        return ""
    peak = float(cum["cum_cr"].abs().max())
    if peak <= 0:
        return ""

    tags = [t for t in (cc.FII, cc.DII) if t in set(cum["class"])]
    if not tags:
        return ""

    HALF = 62
    COL = 2 * HALF + 1 + 10

    head = (
        f'<tr><td width="58" style="width:58px;"></td>'
        + "".join(
            f'<td width="{COL}" align="center" style="width:{COL}px;'
            f'{d.font(9.5, color=d.INK_FAINT, weight="bold", ls=0.6, upper=True)}'
            f'padding:0 5px 5px 5px;white-space:nowrap;">{tag}</td>'
            f'<td width="82" style="width:82px;"></td>'
            for tag in tags
        )
        + '</tr>'
    )

    body_rows = ""
    for dt in trading_days:
        cells = ""
        for tag in tags:
            hit = cum[(cum["class"] == tag) & (cum["deal_date"] == dt)]
            val = float(hit["cum_cr"].iloc[0]) if not hit.empty else 0.0
            cells += (
                f'<td width="{COL}" style="width:{COL}px;padding:2px 5px;">'
                + _diverging_bar(val, peak, HALF,
                                 color_buy=_GOOD, color_sell=_BAD)
                + '</td>'
                f'<td width="82" align="right" style="width:82px;{d.font(11)}'
                f'padding:2px 0 2px 8px;white-space:nowrap;">'
                f'{_fmt_signed_cr(val)}</td>'
            )
        body_rows += (
            f'<tr><td width="58" align="right" style="width:58px;'
            f'{d.font(11.5, weight="bold")}padding:2px 9px 2px 0;white-space:nowrap;">'
            f'{dt.strftime("%a %d")}</td>{cells}</tr>'
        )

    closing = {}
    for tag in tags:
        sub = cum[cum["class"] == tag]
        closing[tag] = float(sub["cum_cr"].iloc[-1]) if not sub.empty else 0.0
    gap = abs(closing.get(cc.FII, 0.0) - closing.get(cc.DII, 0.0))

    legend = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td width="10" bgcolor="{_GOOD}" style="width:10px;height:10px;'
        f'font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10.5, color=_INK_SOFT)}padding:0 14px 0 5px;">'
        f'net bought to date</td>'
        f'<td width="10" bgcolor="{_BAD}" style="width:10px;height:10px;'
        f'font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10.5, color=_INK_SOFT)}padding:0 14px 0 5px;">'
        f'net sold to date</td>'
        f'<td style="{d.font(10.5, color=_STONE, italic=True)}white-space:nowrap;">'
        f'full bar = {_fmt_cr(peak)}&nbsp;cr</td>'
        f'</tr></table>'
    )

    total_w = 58 + len(tags) * (COL + 82)
    out = (
        f'<table role="presentation" width="{total_w}" cellpadding="0" '
        f'cellspacing="0" border="0" style="width:{total_w}px;table-layout:fixed;'
        f'border-collapse:collapse;">{head}{body_rows}</table>'
    )
    if len(tags) == 2 and gap > 0:
        out += (
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0"><tr><td style="{d.font(11, leading=17)}'
            f'padding:12px 0 0 0;">The two sides finished '
            f'<strong>{_fmt_cr(gap)}&nbsp;cr</strong> apart.</td></tr></table>'
        )
    out += (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0"><tr><td style="padding:10px 0 0 0;">{legend}</td></tr></table>'
    )
    return out


# ─── Concentration ────────────────────────────────────────────────────────────

def _concentration(bulk: pd.DataFrame, block: pd.DataFrame) -> pd.DataFrame:
    """Every name in the week by rupee value, largest first, with a running share.

    The max-of-sides rule is applied per symbol **per feed per session**, which
    is exactly the decomposition `_daily_trend` uses, so this frame sums to the
    same week total the session chart and the highlights print. Taking the max
    over the whole week instead looks equivalent and is not: a name bought
    heavily on Monday and sold heavily on Thursday collapses to the larger of
    the two and the week total comes out light. On 17-21 Aug 2026 the difference
    was 30 crore — small, but it would have put a different total under this
    section from the one printed directly above it, which is the kind of
    discrepancy that costs a reader their trust in both numbers.

    Deliberately NOT reconciled against the by-symbol tables in VI and VII:
    those take the max over the whole week within one feed, a third convention
    again. The caption says which total this one belongs to.
    """
    all_ = _combined(bulk, block)
    if all_.empty or "symbol" not in all_.columns:
        return pd.DataFrame()

    per = (
        all_.groupby(["symbol", "_kind", "deal_date", "buy_sell"])["value_cr"]
        .sum()
        .reset_index()
        .groupby(["symbol", "_kind", "deal_date"])["value_cr"]
        .max()
        .reset_index()
        .groupby("symbol")["value_cr"]
        .sum()
    )

    rows = []
    for sym, g in all_.groupby("symbol", dropna=False):
        rows.append({
            "symbol": str(sym),
            "security_name": (str(g["security_name"].iloc[0])
                              if "security_name" in g.columns else str(sym)),
            "value_cr": round(float(per.get(sym, 0.0)), 2),
            "deals": int(len(g)),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = out.sort_values("value_cr", ascending=False).reset_index(drop=True)
    total = float(out["value_cr"].sum())
    if total <= 0:
        return pd.DataFrame()
    out["share_pct"] = out["value_cr"] / total * 100.0
    out["cum_pct"] = out["share_pct"].cumsum()
    return out


def _pareto_chart(conc: pd.DataFrame, *, top_n: int = 10) -> str:
    """Top names by value, with the running share of the week beside each.

    A Pareto, drawn the only way email allows: the bar is the name's own value,
    and the second, thinner track is the cumulative share of the whole week up
    to and including that name. Where the thin track fills, the tape is spoken
    for — so a reader sees at a glance whether the week was broad participation
    or three prints wearing a week's clothing.

    The cumulative track is deliberately a second bar rather than an overlaid
    line. A line needs either SVG or absolute positioning and Outlook honours
    neither; two bars on one row survive every client that matters.
    """
    if conc is None or conc.empty:
        return ""
    total = float(conc["value_cr"].sum())
    if total <= 0:
        return ""

    shown = conc.head(top_n)
    peak = float(shown["value_cr"].max())
    if peak <= 0:
        return ""

    VAL_PX = 176
    CUM_PX = 96

    rows = ""
    for r in shown.itertuples():
        vw = max(2, int(round(float(r.value_cr) / peak * VAL_PX)))
        cw = max(1, int(round(float(r.cum_pct) / 100.0 * CUM_PX)))
        rows += (
            f'<tr>'
            f'<td width="92" style="width:92px;{d.font(11.5, weight="bold")}'
            f'padding:3px 9px 3px 0;white-space:nowrap;">{_e(str(r.symbol))}</td>'
            f'<td width="{VAL_PX}" style="width:{VAL_PX}px;padding:3px 0;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="border-collapse:collapse;"><tr>'
            f'{_bar_cell(vw, _NAVY, 12)}</tr></table></td>'
            f'<td width="80" align="right" style="width:80px;'
            f'{d.font(11.5, color=_INK_SOFT)}padding:3px 14px 3px 9px;'
            f'white-space:nowrap;">{_fmt_cr(float(r.value_cr))}&nbsp;cr</td>'
            f'<td width="{CUM_PX}" style="width:{CUM_PX}px;padding:3px 0;">'
            f'<table role="presentation" width="{CUM_PX}" cellpadding="0" '
            f'cellspacing="0" border="0" style="width:{CUM_PX}px;'
            f'border-collapse:collapse;"><tr>'
            f'{_bar_cell(cw, d.GOLD, 6)}'
            f'{_spacer(CUM_PX - cw)}</tr></table></td>'
            f'<td width="52" align="right" style="width:52px;'
            f'{d.font(11, color=_STONE)}padding:3px 0 3px 8px;white-space:nowrap;">'
            f'{r.cum_pct:.0f}%</td>'
            f'</tr>'
        )

    n_all = len(conc)
    top3 = float(conc.head(3)["share_pct"].sum())
    n_half = int((conc["cum_pct"] < 50.0).sum()) + 1
    legend = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td width="10" bgcolor="{_NAVY}" style="width:10px;height:10px;'
        f'font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10.5, color=_INK_SOFT)}padding:0 14px 0 5px;">'
        f'the name&rsquo;s own value</td>'
        f'<td width="10" bgcolor="{d.GOLD}" style="width:10px;height:10px;'
        f'font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10.5, color=_INK_SOFT)}padding:0 0 0 5px;">'
        f'running share of the week&rsquo;s '
        f'{_fmt_cr(total)}&nbsp;cr across {_n(n_all, "name")}</td>'
        f'</tr></table>'
    )

    # The second clause is dropped when the two measures land on the same names,
    # which they do on any week concentrated enough for the first clause to be
    # worth printing. "The three largest carried 54%, and three names carried
    # the first half" says one thing twice.
    headline = (
        f'The three largest names carried <strong>{top3:.0f}%</strong> of the '
        f'week&rsquo;s reported value'
    )
    headline += (
        f'.' if n_half <= 3 else
        f', and <strong>{_n(n_half, "name")}</strong> of {n_all} carried the '
        f'first half.'
    )

    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="border-collapse:collapse;">{rows}</table>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0"><tr><td style="{d.font(11, leading=17)}padding:12px 0 0 0;">'
        f'{headline}</td></tr></table>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0"><tr><td style="padding:10px 0 0 0;">{legend}</td></tr></table>'
    )


# ─── Comprehensive-edition charts (SVG, PDF only) ─────────────────────────────
#
# Everything above is built from nested table cells because it has to survive
# Outlook. These two do not: they appear only in the comprehensive edition,
# which is never an email body — it is rendered to PDF by Chromium and attached.
# Chromium is a full browser, so inline SVG is exactly as safe there as it is in
# a browser preview, and no image library is needed. Nothing here is ever
# emitted into the focus edition.
#
# `_build_html` gates both on `not is_focus`. If that ever changes, these must
# be replaced, not merely restyled: Word renders no SVG at all and would drop
# them silently, leaving a section heading over blank space.

_SVG_W = 640


def _squarify(values: list[float], x: float, y: float, w: float, h: float) -> list[tuple]:
    """Squarified treemap layout: rectangles whose aspect ratios stay near 1.

    Plain slice-and-dice produces slivers as soon as one value dominates, and
    this week's tape always has a dominant name. Returns one (x, y, w, h) per
    value, in the order given; values must already be sorted descending.
    """
    rects: list[tuple] = []
    if not values or w <= 0 or h <= 0:
        return rects
    total = float(sum(values))
    if total <= 0:
        return rects

    vals = [v * (w * h) / total for v in values]   # scaled to area
    i = 0
    cx, cy, cw, ch = x, y, w, h

    def _worst(row: list[float], side: float) -> float:
        if not row or side <= 0:
            return float("inf")
        s = sum(row)
        if s <= 0:
            return float("inf")
        side2 = side * side
        s2 = s * s
        return max((side2 * max(row)) / s2, s2 / (side2 * min(row)))

    while i < len(vals):
        side = min(cw, ch)
        row = [vals[i]]
        j = i + 1
        while j < len(vals) and _worst(row + [vals[j]], side) <= _worst(row, side):
            row.append(vals[j])
            j += 1

        s = sum(row)
        if cw >= ch:                     # lay the row down a left-hand column
            band = s / ch if ch > 0 else 0.0
            oy = cy
            for v in row:
                rh = (v / s * ch) if s > 0 else 0.0
                rects.append((cx, oy, band, rh))
                oy += rh
            cx += band
            cw -= band
        else:                            # lay the row across the top
            band = s / cw if cw > 0 else 0.0
            ox = cx
            for v in row:
                rw = (v / s * cw) if s > 0 else 0.0
                rects.append((ox, cy, rw, band))
                ox += rw
            cy += band
            ch -= band
        i = j

    return rects


def _class_symbol_values(bulk: pd.DataFrame, block: pd.DataFrame) -> pd.DataFrame:
    """Week value per (client class, symbol), same max-of-sides convention.

    A deal is attributed to the class of the client on the reported side, so a
    name where an FII sold to a corporate appears under both. That is the honest
    reading of a feed that reports each side independently, and it is why the
    class totals in section III do not sum to zero either.
    """
    all_ = _combined(bulk, block)
    if all_.empty or "client_name" not in all_.columns:
        return pd.DataFrame()
    all_ = all_.copy()
    all_["class"] = all_["client_name"].astype(str).str.strip().apply(_classify)

    rows = []
    for (tag, sym), g in all_.groupby(["class", "symbol"], dropna=False):
        bv = float(g.loc[g["buy_sell"] == "B", "value_cr"].sum())
        sv = float(g.loc[g["buy_sell"] == "S", "value_cr"].sum())
        val = max(bv, sv)
        if val > 0:
            rows.append({"class": str(tag), "symbol": str(sym),
                         "value_cr": round(val, 2)})
    return pd.DataFrame(rows)


def _treemap_svg(cs: pd.DataFrame, *, per_class: int = 7) -> str:
    """Composition and concentration in one frame: class blocks, names inside.

    The class table says how much each kind of participant traded and the
    concentration Pareto says how few names carried the week. Neither says which
    names sat inside which class, and that is usually the question the two
    together provoke. Area is week value throughout, so a block's share of the
    picture is its share of the tape.

    Names past `per_class` within a class are pooled rather than dropped — a
    long tail rendered as forty unreadable slivers is worse than one honest
    block that says how many it stands for.
    """
    if cs is None or cs.empty:
        return ""
    total = float(cs["value_cr"].sum())
    if total <= 0:
        return ""

    by_class = (cs.groupby("class")["value_cr"].sum()
                  .sort_values(ascending=False))
    order = [t for t in cc.CLASS_ORDER if t in by_class.index]
    order += [t for t in by_class.index if t not in order]
    class_vals = [float(by_class[t]) for t in order]

    H = 300
    outer = _squarify(class_vals, 0.0, 0.0, float(_SVG_W), float(H))
    if not outer:
        return ""

    palette = d.SERIES
    parts = []
    for idx, (tag, (bx, by, bw, bh)) in enumerate(zip(order, outer)):
        if bw <= 1 or bh <= 1:
            continue
        base = palette[idx % len(palette)]
        sub = (cs[cs["class"] == tag]
               .sort_values("value_cr", ascending=False))
        head = sub.head(per_class)
        tail_val = float(sub["value_cr"].iloc[per_class:].sum())
        labels = [(str(r.symbol), float(r.value_cr)) for r in head.itertuples()]
        if tail_val > 0:
            n_tail = len(sub) - len(head)
            labels.append((f"+{n_tail}", tail_val))

        inner = _squarify([v for _, v in labels], bx, by, bw, bh)
        for (name, val), (rx, ry, rw, rh) in zip(labels, inner):
            parts.append(
                f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{max(rw - 1, 0):.1f}" '
                f'height="{max(rh - 1, 0):.1f}" fill="{base}" '
                f'fill-opacity="0.86" stroke="{d.PAPER}" stroke-width="1"/>'
            )
            # Only label a tile with room for the text. An 11px word in a 30px
            # box is a smear that makes the chart look broken.
            if rw >= 46 and rh >= 20:
                parts.append(
                    f'<text x="{rx + 4:.1f}" y="{ry + 14:.1f}" '
                    f'font-family="{d.FONT}" font-size="10.5" fill="{d.PAPER}" '
                    f'font-weight="bold">{_e(name)}</text>'
                )
            if rw >= 60 and rh >= 34:
                parts.append(
                    f'<text x="{rx + 4:.1f}" y="{ry + 27:.1f}" '
                    f'font-family="{d.FONT}" font-size="9.5" fill="{d.PAPER}" '
                    f'fill-opacity="0.85">{_strip_html(_fmt_cr(val))} cr</text>'
                )

    # Class key beneath, in the same order and colour as the blocks.
    key_cells = "".join(
        f'<td width="11" bgcolor="{palette[i % len(palette)]}" style="width:11px;'
        f'height:11px;font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10, color=_INK_SOFT)}padding:0 13px 0 5px;'
        f'white-space:nowrap;">{tag} '
        f'<span style="color:{_STONE};">'
        f'{by_class[tag] / total * 100:.0f}%</span></td>'
        for i, tag in enumerate(order)
    )

    return (
        f'<div style="width:100%;"><svg width="100%" viewBox="0 0 {_SVG_W} {H}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Week value by client class and symbol">'
        f'<rect width="{_SVG_W}" height="{H}" fill="{d.BAND_SOFT}"/>'
        + "".join(parts) +
        f'</svg></div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin-top:11px;"><tr>{key_cells}</tr></table>'
    )


def _scatter_svg(conc: pd.DataFrame, bulk: pd.DataFrame,
                 block: pd.DataFrame) -> str:
    """Week value against company size, to show where in the market it happened.

    Bulk-deal reportability is a percentage of traded quantity, so the feed is
    structurally biased toward smaller names — but "structurally biased" is a
    claim about every week, and this is a picture of one. A cluster on the left
    is the ordinary week; a point far to the right is a mega-cap that saw a
    genuine block, which is the rarer and more interesting event.

    Both axes are logarithmic, because market caps in this universe span four
    orders of magnitude and a linear axis would pile every name into one corner.
    """
    if conc is None or conc.empty:
        return ""

    all_ = _combined(bulk, block)
    if all_.empty:
        return ""
    side = {}
    for sym, g in all_.groupby("symbol", dropna=False):
        bv = float(g.loc[g["buy_sell"] == "B", "value_cr"].sum())
        sv = float(g.loc[g["buy_sell"] == "S", "value_cr"].sum())
        side[str(sym)] = bv - sv

    pts, no_mcap = [], 0
    for r in conc.itertuples():
        sym = str(r.symbol)
        mcap = sm.market_cap_cr(sym)
        val = float(r.value_cr)
        if not mcap or mcap <= 0 or val <= 0:
            no_mcap += 1
            continue
        pts.append((sym, float(mcap), val, side.get(sym, 0.0)))
    if len(pts) < 2:
        return ""

    import math
    W, H = _SVG_W, 320
    L, R, T, B = 62, 16, 16, 40
    xs = [math.log10(m) for _, m, _, _ in pts]
    ys = [math.log10(v) for _, _, v, _ in pts]
    x0, x1 = math.floor(min(xs)), math.ceil(max(xs))
    y0, y1 = math.floor(min(ys)), math.ceil(max(ys))
    x1 = max(x1, x0 + 1)
    y1 = max(y1, y0 + 1)

    def px(lv: float) -> float:
        return L + (lv - x0) / (x1 - x0) * (W - L - R)

    def py(lv: float) -> float:
        return H - B - (lv - y0) / (y1 - y0) * (H - T - B)

    def _tick(e: int) -> str:
        v = 10 ** e
        if v >= 1e5:
            return f"{v / 1e5:,.0f}L"
        if v >= 1e3:
            return f"{v / 1e3:,.0f}k"
        return f"{v:,.0f}"

    grid = []
    for e in range(x0, x1 + 1):
        gx = px(e)
        grid.append(
            f'<line x1="{gx:.1f}" y1="{T}" x2="{gx:.1f}" y2="{H - B}" '
            f'stroke="{d.RULE}" stroke-width="1"/>'
            f'<text x="{gx:.1f}" y="{H - B + 15:.1f}" text-anchor="middle" '
            f'font-family="{d.FONT}" font-size="9.5" fill="{d.INK_FAINT}">'
            f'{_tick(e)}</text>'
        )
    for e in range(y0, y1 + 1):
        gy = py(e)
        grid.append(
            f'<line x1="{L}" y1="{gy:.1f}" x2="{W - R}" y2="{gy:.1f}" '
            f'stroke="{d.RULE}" stroke-width="1"/>'
            f'<text x="{L - 7:.1f}" y="{gy + 3.5:.1f}" text-anchor="end" '
            f'font-family="{d.FONT}" font-size="9.5" fill="{d.INK_FAINT}">'
            f'{_tick(e)}</text>'
        )

    dots, labels = [], []
    biggest = sorted(pts, key=lambda p: -p[2])[:6]
    big = {p[0] for p in biggest}
    for sym, mcap, val, net in pts:
        cx, cy = px(math.log10(mcap)), py(math.log10(val))
        fill = _GOOD if net > 0 else (_BAD if net < 0 else d.INK_FAINT)
        dots.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.2" fill="{fill}" '
            f'fill-opacity="0.72" stroke="{d.PAPER}" stroke-width="0.8"/>'
        )
        if sym in big:
            labels.append(
                f'<text x="{cx + 7:.1f}" y="{cy + 3:.1f}" font-family="{d.FONT}" '
                f'font-size="9.5" fill="{d.INK}">{_e(sym)}</text>'
            )

    axis = (
        f'<text x="{(L + W - R) / 2:.1f}" y="{H - 4:.1f}" text-anchor="middle" '
        f'font-family="{d.FONT}" font-size="9.5" fill="{d.INK_FAINT}" '
        f'letter-spacing="0.6">MARKET CAPITALISATION, RUPEE CRORE</text>'
        f'<text x="12" y="{(T + H - B) / 2:.1f}" text-anchor="middle" '
        f'font-family="{d.FONT}" font-size="9.5" fill="{d.INK_FAINT}" '
        f'letter-spacing="0.6" '
        f'transform="rotate(-90 12 {(T + H - B) / 2:.1f})">'
        f'WEEK VALUE, RUPEE CRORE</text>'
    )

    key = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin-top:11px;"><tr>'
        f'<td width="11" bgcolor="{_GOOD}" style="width:11px;height:11px;'
        f'font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10, color=_INK_SOFT)}padding:0 14px 0 5px;">'
        f'net bought over the week</td>'
        f'<td width="11" bgcolor="{_BAD}" style="width:11px;height:11px;'
        f'font-size:0;line-height:0;">&nbsp;</td>'
        f'<td style="{d.font(10, color=_INK_SOFT)}padding:0 14px 0 5px;">'
        f'net sold</td>'
        f'<td style="{d.font(10, color=_STONE, italic=True)}">'
        f'{len(pts)} of {len(conc)} names carry a market cap in the security '
        f'master' + (f'; {no_mcap} without one are not plotted' if no_mcap else '')
        + f'</td></tr></table>'
    )

    return (
        f'<div style="width:100%;"><svg width="100%" viewBox="0 0 {W} {H}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Week value against market capitalisation">'
        f'<rect width="{W}" height="{H}" fill="{d.PAPER}"/>'
        + "".join(grid) + "".join(dots) + "".join(labels) + axis +
        f'</svg></div>{key}'
    )


def _sym_table_bars_html(
    sym: pd.DataFrame, *, show_mcap: bool = False,
    source: str = "", caption: str = "",
) -> str:
    """The by-symbol table with a net-direction bar in place of the flag glyph.

    A weekly-local variant rather than a flag on the daily's `_sym_table_html`.
    The daily renders one session, where the triangle-or-equals glyph is exactly
    the right amount of information; over five sessions the interesting question
    is not "was this name asymmetric" but "how asymmetric, and which way", and a
    glyph cannot answer that. Copying the renderer to answer it here is the
    trade this module makes everywhere: the daily's 10:00 email must not change
    shape because the weekly wanted a column.

    Scale is net quantity, not value, and is shared across the rows drawn. Value
    already has its own column; drawing it twice would leave the net direction —
    the thing the glyph used to carry — still unmeasured.
    """
    if sym is None or sym.empty:
        return d.datatable([], [], empty="No deals in scope.")

    work = sym.copy()
    # No leading underscore: pandas' itertuples() cannot expose a field whose
    # name starts with one and silently renames it positionally, so `net_qty`
    # read through getattr would fall back to its default and every bar in the
    # column would render blank. Pinned by a test.
    net_qty = (work["buy_qty"].astype("int64")
               - work["sell_qty"].astype("int64"))
    work["net_direction_qty"] = net_qty
    peak = float(net_qty.abs().max())

    cols = ["Symbol", "Security"] + (["Mkt cap"] if show_mcap else []) +            ["Buy qty", "Sell qty", "&#8377; cr", "Net direction"]
    align = ["l", "l"] + (["r"] if show_mcap else []) + ["r", "r", "r", "c"]

    rows = []
    for r in work.itertuples():
        sy = str(r.symbol)
        net = float(r.net_direction_qty)
        row = [
            f'<strong>{_e(sy)}</strong>{_index_badge(sy) if show_mcap else ""}',
            f'<span style="color:{_INK_SOFT};">'
            f'{_e(str(r.security_name or sy))}</span>',
        ]
        if show_mcap:
            row.append(_fmt_mcap(sy))
        row += [
            _fmt_qty(int(getattr(r, "buy_qty", 0))),
            _fmt_qty(int(getattr(r, "sell_qty", 0))),
            f'<strong>{_fmt_cr(float(r.total_vcr))}</strong>',
            _diverging_bar(net, peak, 40, color_buy=_GOOD, color_sell=_BAD,
                           tick_h=11, bar_h=8),
        ]
        rows.append(row)

    return d.datatable(cols, rows, align=align, source=source, caption=caption)


def _persist_table_html(
    one_way: pd.DataFrame, n_round_trips: int,
    *, top_n: int | None, show_badge: bool, source: str,
) -> str:
    """The week's accumulation and distribution, strongest persistence first."""
    if one_way is None or one_way.empty:
        empty = (
            "No client traded the same name on more than one session this week."
            if not n_round_trips else
            f"No one-way flows. All {n_round_trips} multi-session client-name "
            f"pairs this week were round trips."
        )
        return d.datatable([], [], empty=empty)

    n_total = len(one_way)
    rows_df = one_way.head(top_n) if top_n else one_way
    shown = len(rows_df)

    # Scaled against the rows actually drawn, not the whole frame. A capped body
    # whose bars were scaled to a row only the PDF shows would render every bar
    # it does show as a stub.
    peak = float(rows_df["net_cr"].abs().max()) if not rows_df.empty else 0.0

    rows = []
    prev = None
    for _, r in rows_df.iterrows():
        cn = str(r["client_name"])
        s = str(r["symbol"])
        first = cn != prev
        net_cr = float(r["net_cr"])
        conv = float(r["conviction"])
        side = "buy" if net_cr >= 0 else "sell"
        shade = _shade(side, conv)
        rows.append([
            (f'<strong>{_e(cn)}</strong>' if first
             else f'<span style="color:{_STONE};">&#8942;</span>'),
            _tag_html(str(r["class"])),
            f'{_e(s)}{_index_badge(s) if show_badge else ""}',
            f'<strong>{int(r["sessions"])}</strong>',
            _fmt_signed_qty(r["net_qty"]),
            _fmt_signed_cr(net_cr),
            f'{conv * 100:.0f}%',
            _diverging_bar(net_cr, peak, 44,
                           color_buy=shade, color_sell=shade,
                           tick_h=11, bar_h=8),
        ])
        prev = cn

    caption_bits = [
        "Bar length is the net position and its shade is how one-way the flow "
        "was &mdash; a long dark bar is a large position built almost without "
        "selling, a long pale one is a large position built through a lot of "
        "two-way trading. Left of the rule is distribution, right is "
        "accumulation. "
        "Largest net position first. Net is buy minus sell across the week, so a "
        "client that bought and sold the same name shows only what it kept. "
        "One-way share is the fraction of its traded quantity that did not "
        "cancel out &mdash; the column that separates a position from churn, "
        f"floored here at {PERSIST_MIN_CONVICTION * 100:.0f}%. Sessions is how "
        f"many of the week&rsquo;s trading days the client was active in the "
        f"name; more sessions is a more patient flow, not a larger one."
    ]
    if n_round_trips:
        caption_bits.append(
            f"A further {_n(n_round_trips, 'client-name pair')} traded on multiple "
            f"sessions but netted below that floor and are excluded as round trips."
        )
    if top_n and n_total > shown:
        caption_bits.append(
            f"Showing the {shown} most persistent of {n_total} qualifying pairs; "
            f"the attached PDF carries all of them."
        )

    return d.datatable(
        ["Client", "Class", "Symbol", "Sessions", "Net qty", "Net &#8377; cr",
         "One-way", "Flow"],
        rows,
        align=["l", "c", "l", "c", "r", "r", "r", "c"],
        source=source,
        caption=" ".join(caption_bits),
    )


def _class_flow_table_html(class_flow: pd.DataFrame, *, source: str) -> str:
    """Buy, sell and net rupee crore by client class for the week."""
    if class_flow is None or class_flow.empty:
        return d.datatable([], [], empty="No client data.")

    rows = []
    for _, r in class_flow.iterrows():
        tag = str(r["class"])
        rows.append([
            _tag_html(tag),
            f'<span style="color:{_INK_SOFT};">'
            f'{cc.CLASS_LABEL.get(tag, tag)}</span>',
            _fmt_cr(float(r["buy_cr"])),
            _fmt_cr(float(r["sell_cr"])),
            _fmt_signed_cr(float(r["net_cr"])),
            f'{int(r["clients"]):,}',
            f'{int(r["names"]):,}',
        ])

    return d.datatable(
        ["Class", "", "Bought &#8377; cr", "Sold &#8377; cr",
         "Net &#8377; cr", "Clients", "Names"],
        rows,
        align=["c", "l", "r", "r", "r", "r", "r"],
        source=source,
        caption=(
            "Both legs of a deal are reported only when each independently "
            "crosses the reporting threshold, so these class totals do not sum "
            "to zero the way the market as a whole must. Read a net figure as "
            "the reported side of that class&rsquo;s week, not as its true "
            "position change. Quant and prop desks netting to near flat on "
            "large gross figures is the expected shape, not an anomaly. "
            + _CLASS_CAVEAT
        ),
    )


def _short_week_table_html(
    short_week: pd.DataFrame, *, top_n: int | None,
    show_mcap: bool, source: str, caption: str,
) -> str:
    """Short positions summed per symbol over the week."""
    if short_week is None or short_week.empty:
        return d.datatable([], [], empty="No reported short positions.")

    df = short_week
    if show_mcap:
        df = df.sort_values("notional", ascending=False)
    n_total = len(df)
    df = df.head(top_n) if top_n else df

    cols = ["Symbol", "Security"] + (["Mkt cap"] if show_mcap else []) + \
           ["Sessions", "Week qty", "Peak day"] + (["&#8776; value"] if show_mcap else [])
    align = ["l", "l"] + (["r"] if show_mcap else []) + \
            ["c", "r", "r"] + (["r"] if show_mcap else [])

    rows = []
    for _, r in df.iterrows():
        s = str(r.get("symbol", ""))
        qty = int(r.get("quantity", 0))
        row = [
            f'<strong>{_e(s)}</strong>{_index_badge(s) if show_mcap else ""}',
            f'<span style="color:{_INK_SOFT};">'
            f'{_e(str(r.get("security_name", s) or s))}</span>',
        ]
        if show_mcap:
            row.append(_fmt_mcap(s))
        row += [
            f'{int(r.get("sessions", 0))}',
            f'<strong>{qty:,}</strong>',
            f'{int(r.get("peak", 0)):,}',
        ]
        if show_mcap:
            row.append(f'<strong>{_fmt_notional(qty, s)}</strong>')
        rows.append(row)

    if top_n and n_total > len(rows):
        caption += (
            f" Showing {len(rows)} of {n_total} names; the attached PDF carries "
            f"all of them."
        )
    return d.datatable(cols, rows, align=align, source=source, caption=caption)


def _material(df: pd.DataFrame, min_cr: float) -> tuple[pd.DataFrame, int]:
    """Rows clearing the floor, and how many were set aside below it.

    Filters on value_cr against the `min_cr` actually passed, rather than
    against a boolean precomputed at aggregation time. An earlier version did
    both, so the two editions silently shared one threshold — the caller's
    argument was accepted and then ignored, which is worse than not offering it.
    """
    if df is None or df.empty or not min_cr or "value_cr" not in df.columns:
        return df, 0
    keep = df[df["value_cr"] >= min_cr]
    return keep, len(df) - len(keep)


def _first_appearance_html(
    new_syms: pd.DataFrame, new_clients: pd.DataFrame,
    *, top_n: int | None, show_badge: bool, source: str, min_cr: float = 0.0,
) -> str:
    """Two stacked tables: names new to the feed, then clients new to it.

    `min_cr` is the materiality floor. The email body passes it; the
    comprehensive PDF passes zero and lists every absent name, so the floor
    never removes anything from the record — only from the read.
    """
    all_empty = (
        (new_syms is None or new_syms.empty)
        and (new_clients is None or new_clients.empty)
    )
    if all_empty:
        return d.datatable(
            [], [],
            empty=(
                f"Every name and client in the week had already appeared in the "
                f"prior {_n(LOOKBACK_WEEKS, 'week')}."
            ),
        )

    new_syms, dropped_syms = _material(new_syms, min_cr)
    new_clients, dropped_clients = _material(new_clients, min_cr)

    # Everything was absent, but nothing was material. Say that, rather than
    # rendering an empty section that reads as "no new names".
    if (new_syms is None or new_syms.empty) and (new_clients is None or new_clients.empty):
        return d.datatable(
            [], [],
            empty=(
                f"{_n(dropped_syms, 'name')} and {_n(dropped_clients, 'client')} "
                f"were absent from the prior {_n(LOOKBACK_WEEKS, 'week')}, but "
                f"none carried {_fmt_cr(min_cr)}&nbsp;cr this week. The attached "
                f"PDF lists them all."
            ),
        )

    blocks: list[str] = []

    if new_syms is not None and not new_syms.empty:
        n_total = len(new_syms)
        rows_df = new_syms.head(top_n) if top_n else new_syms
        rows = []
        for _, r in rows_df.iterrows():
            s = str(r["symbol"])
            rows.append([
                f'<strong>{_e(s)}</strong>{_index_badge(s) if show_badge else ""}',
                f'<span style="color:{_INK_SOFT};">'
                f'{_e(str(r.get("security_name", s) or s))}</span>',
                _fmt_mcap(s),
                f'{int(r["sessions"])}',
                f'{int(r["deals"])}',
                f'<strong>{_fmt_cr(float(r["value_cr"]))}</strong>',
            ])
        head = (
            f'{_n(n_total, "name")} with no reported bulk or block deal in the '
            f'prior {_n(LOOKBACK_WEEKS, "week")}'
        )
        if dropped_syms:
            head += (
                f' and worth {_fmt_cr(min_cr)}&nbsp;cr or more this week '
                f'&middot; {dropped_syms} smaller one{"" if dropped_syms == 1 else "s"} '
                f'set aside'
            )
        if top_n and n_total > len(rows):
            head += f' &middot; largest {len(rows)} by value shown'
        blocks.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0"><tr><td style="padding:0 0 24px 0;">'
            + d.caption_title("Names appearing for the first time")
            + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
              f'border="0"><tr><td style="{d.font(10.5, color=_STONE)}'
              f'padding:0 0 12px 0;">{head}</td></tr></table>'
            + d.datatable(
                ["Symbol", "Security", "Mkt cap", "Sessions", "Deals", "&#8377; cr"],
                rows,
                align=["l", "l", "r", "c", "c", "r"],
                widths=[104, 178, 88, 60, 52, 74],
            )
            + '</td></tr></table>'
        )

    if new_clients is not None and not new_clients.empty:
        n_total = len(new_clients)
        rows_df = new_clients.head(top_n) if top_n else new_clients
        rows = []
        for _, r in rows_df.iterrows():
            rows.append([
                f'<strong>{_e(str(r["client_name"]))}</strong>',
                _tag_html(str(r["class"])),
                f'{int(r["names"])}',
                f'{int(r["sessions"])}',
                f'{int(r["deals"])}',
                f'<strong>{_fmt_cr(float(r["value_cr"]))}</strong>',
            ])
        head = (
            f'{_n(n_total, "client")} with no reported deal in the prior '
            f'{_n(LOOKBACK_WEEKS, "week")}'
        )
        if dropped_clients:
            head += (
                f' and worth {_fmt_cr(min_cr)}&nbsp;cr or more this week '
                f'&middot; {dropped_clients} smaller '
                f'one{"" if dropped_clients == 1 else "s"} set aside'
            )
        if top_n and n_total > len(rows):
            head += f' &middot; largest {len(rows)} by value shown'
        blocks.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0"><tr><td style="padding:0 0 24px 0;">'
            + d.caption_title("Clients appearing for the first time")
            + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
              f'border="0"><tr><td style="{d.font(10.5, color=_STONE)}'
              f'padding:0 0 12px 0;">{head}</td></tr></table>'
            + d.datatable(
                ["Client", "Class", "Names", "Sessions", "Deals", "&#8377; cr"],
                rows,
                align=["l", "c", "c", "c", "c", "r"],
                widths=[196, 60, 56, 68, 52, 74],
            )
            + '</td></tr></table>'
        )

    caveat = (
        f"&ldquo;First appearance&rdquo; is measured against the reported deal "
        f"feeds over the prior {_n(LOOKBACK_WEEKS, 'week')} only. A name absent "
        f"from it has not been quiet &mdash; it has had no deal large enough to "
        f"report, which is a different and much weaker claim. Client matching is "
        f"on the exact name string the exchange published, so a renamed or "
        f"differently-spelled entity can appear here as new."
    )
    if min_cr and (dropped_syms or dropped_clients):
        caveat += (
            f" Roughly half the names in any week are absent from a "
            f"{LOOKBACK_WEEKS}-week window &mdash; most are single-session block "
            f"participants that will not be seen again &mdash; so this section "
            f"reports only those carrying {_fmt_cr(min_cr)}&nbsp;cr or more. The "
            f"attached PDF lists every absent name at any size."
        )
    return (
        "".join(blocks)
        + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
          f'border="0"><tr><td style="{d.font(10.5, color=_STONE)}padding:0 0 0 0;">'
          f'{source}</td></tr>'
          f'<tr><td style="{d.font(10, color=_INK_SOFT, italic=True, leading=16)}'
          f'text-align:justify;padding:12px 0 0 0;">{caveat}</td></tr></table>'
    )


def _scope_banner(
    focus_symbols: list[str], full_metrics: dict, n_full_names: int,
    start: date, end: date, n_sessions: int, basis: str = "market_cap",
) -> str:
    """Explains the focused edition's scope and points at the attached PDF.

    Without this the reader cannot tell a quiet week from a filtered one — the
    single most important thing to be honest about in a scoped report.
    """
    fbm, fbkm = full_metrics["bulk"], full_metrics["block"]
    n50 = sum(1 for s in focus_symbols if sm.is_nifty50(s))

    if focus_symbols and basis == "value":
        lead = (
            f'Each section below carries its own {len(focus_symbols)} largest '
            f'positions <strong>by deal size</strong> rather than by market '
            f'capitalisation &mdash; no market-cap data was available for this '
            f'week&rsquo;s names, so the usual ranking could not be applied.'
        )
    elif focus_symbols:
        lead = (
            f'Each section below carries its own {FOCUS_TOP_N} largest companies '
            f'by market capitalisation &mdash; {len(focus_symbols)} distinct names '
            f'in all'
            + (f', {n50} of them NIFTY50 constituents' if n50 else '')
            + '. Sections are ranked independently, because the biggest names in '
              'the short-selling feed are rarely the ones with reportable bulk or '
              'block deals.'
        )
    elif n_full_names == 0:
        lead = (
            'No bulk, block or short-selling activity was reported anywhere in '
            'this trading week.'
        )
    else:
        lead = (
            f'None of the week&rsquo;s {n_full_names} names could be ranked by '
            f'market capitalisation &mdash; the security master needs a refresh.'
        )

    body = (
        f'{lead} Across the whole market the {_n(n_sessions, "session")} from '
        f'{_house_range(start, end)} carried '
        f'<strong>{fbm["deals"]}</strong> bulk deal{"" if fbm["deals"] == 1 else "s"} '
        f'in <strong>{fbm["names"]}</strong> name{"" if fbm["names"] == 1 else "s"} '
        f'({_fmt_cr(fbm["value_cr"])}&nbsp;cr) and '
        f'<strong>{fbkm["deals"]}</strong> block deal{"" if fbkm["deals"] == 1 else "s"} '
        f'in <strong>{fbkm["names"]}</strong> name{"" if fbkm["names"] == 1 else "s"} '
        f'({_fmt_cr(fbkm["value_cr"])}&nbsp;cr). The persistent-flow, class-net and '
        f'first-appearance sections are computed over that whole market, not over '
        f'the focus names &mdash; a position built over five sessions is the point '
        f'of this report, and narrowing it first would hide most of them.'
    )
    return d.callout(body, accent="navy", title="What you are reading")


# ─── Full HTML assembly ───────────────────────────────────────────────────────

_DISCLAIMER = (
    "Internal research document. Generated automatically from NSE exchange files. "
    "All figures are end-of-day vintage for the sessions named; there is no "
    "intraday data path in this build. Quantities are as reported and are not "
    "adjusted for corporate actions. Not investment advice."
)


def _build_html(
    start: date, end: date, trading_days: list[date],
    frames: dict, wow: dict, highlights: list[dict], generated_at: datetime,
    *,
    edition: str = EDITION_FULL,
    focus_symbols: list[str] | None = None,
    full_metrics: dict | None = None,
    n_full_names: int = 0,
    focus_basis: str = "market_cap",
    # The week-shaped sections rank over the whole market even in the focused
    # edition; these carry the unfiltered frames for them.
    week_frames: dict | None = None,
) -> str:
    is_focus = edition == EDITION_FOCUS
    focus_symbols = focus_symbols or []
    wf = week_frames or frames
    n_sessions = len(trading_days)

    # The topline is the market's week in BOTH editions, not the focus subset's.
    # `wow` is computed against the whole prior week, so pairing it with a
    # scoped value would put a market-wide percentage under a filtered figure —
    # a number that describes neither. The focus lens applies to the per-symbol
    # sections below, and the scope banner says so.
    metrics = wf["metrics"]
    bm, bkm, shm = metrics["bulk"], metrics["block"], metrics["short"]

    import pytz
    gen_ist = generated_at.astimezone(pytz.timezone("Asia/Kolkata"))

    # Row caps: the body shows a readable slice and says so; the PDF is the
    # record and shows everything.
    cap = BODY_ROW_CAP if is_focus else None
    class_cap = FOCUS_TOP_N if is_focus else None

    # ── Masthead ─────────────────────────────────────────────────────────────
    if is_focus:
        edition_label = "Focus edition"
        scope = (
            "Scoped to the largest companies by market capitalisation in each "
            "per-symbol section; the week-shaped sections cover the whole market. "
            "The comprehensive record is attached as a PDF."
        )
    else:
        edition_label = "Comprehensive edition"
        scope = (
            "Every bulk, block and short-selling record reported across the "
            "trading week, unfiltered."
        )

    body = d.masthead(
        kicker="Brindco &middot; Quant Desk",
        title="Weekly Deals",
        dateline=f"<strong>{_house_range(start, end)}</strong> &middot; {edition_label}",
        subline=(
            f"National Stock Exchange of India &middot; "
            f"{_n(n_sessions, 'trading session')} &middot; generated "
            f"{gen_ist.strftime('%d-%b-%Y %H:%M')} IST"
        ),
        scope=scope,
    )

    # ── Topline, with week-on-week ────────────────────────────────────────────
    kpis = [
        {"label": "Bulk deals, week &middot; all names",
         "value": (f"{_fmt_cr(bm['value_cr'])}"
                   f"<span style=\"font-size:13px;color:{_STONE};\"> cr</span>"),
         "sub": (f"{_n(bm['deals'], 'deal')}, {_n(bm['names'], 'name')}<br/>"
                 f"{_fmt_pct_delta(wow.get('bulk_value'))}")},
        {"label": "Block deals, week &middot; all names",
         "value": (f"{_fmt_cr(bkm['value_cr'])}"
                   f"<span style=\"font-size:13px;color:{_STONE};\"> cr</span>"),
         "sub": (f"{_n(bkm['deals'], 'deal')}, {_n(bkm['names'], 'name')}<br/>"
                 f"{_fmt_pct_delta(wow.get('block_value'))}")},
        {"label": "Short selling, week &middot; all names",
         "value": (f"{shm['deals']}"
                   f"<span style=\"font-size:13px;color:{_STONE};\"> positions</span>"),
         "sub": (f"across {_n(n_sessions, 'session')}<br/>"
                 f"{_fmt_pct_delta(wow.get('short_pos'))}")},
    ]
    body += d.row(d.kpi_grid(kpis), pad=d.BLOCK_PAD)

    if is_focus:
        body += d.row(
            _scope_banner(
                focus_symbols, full_metrics or metrics, n_full_names,
                start, end, n_sessions, focus_basis,
            ),
            pad=d.BLOCK_PAD,
        )

    hl = _highlights_html(highlights)
    if hl:
        body += d.row(hl, pad=d.BLOCK_PAD)

    src = f"NSE archives &middot; {_house_range(start, end)}"

    # ── I · The week, session by session ─────────────────────────────────────
    chart = _trend_chart(wf["trend"])
    if chart:
        body += d.row(
            _section("I", "The week, session by session",
                     "bulk and block, rupee crore per session")
            + chart
            + f'<table role="presentation" width="100%" cellpadding="0" '
              f'cellspacing="0" border="0"><tr><td style="'
              f'{d.font(10, color=_INK_SOFT, italic=True, leading=16)}'
              f'text-align:justify;padding:14px 0 0 0;">The larger of the buy or '
              f'sell side is counted per name per session, so a crossed deal '
              f'counts once. Sessions with no reported deals are shown rather '
              f'than dropped &mdash; a gap in the middle of the week is either a '
              f'quiet market or a missed scrape, and both are worth seeing. '
              f'Whole market, both editions.</td></tr></table>'
        )

    # ── II · Concentration ───────────────────────────────────────────────────
    # Placed directly under the session bars, because it answers the question
    # those bars raise: a heavy week and a week with three heavy prints look
    # identical in a bar per session.
    pareto = _pareto_chart(wf.get("concentration"), top_n=FOCUS_TOP_N)
    if pareto:
        body += d.row(
            _section("II", "Where the week&rsquo;s value sat",
                     "largest names by rupee value, with the running share "
                     "&middot; whole market")
            + pareto
            + f'<table role="presentation" width="100%" cellpadding="0" '
              f'cellspacing="0" border="0"><tr><td style="'
              f'{d.font(10, color=_INK_SOFT, italic=True, leading=16)}'
              f'text-align:justify;padding:14px 0 0 0;">A name&rsquo;s value is '
              f'the larger of its buy and sell side in each feed on each '
              f'session, so a crossed deal counts once and these figures sum to '
              f'the same week total as the session chart above. The by-symbol '
              f'tables further down apply that rule over the whole week instead, '
              f'so a name can differ there by a little. '
              f'A week where the running share reaches most of the way '
              f'across in three or four names was not a busy market; it was a '
              f'few holders acting, and the persistent-flow section is where to '
              f'find out whether they kept acting.</td></tr></table>'
        )

    # ── III · Persistent flows — the section the weekly exists for ───────────
    body += d.row(
        _section("III", "Persistent flows",
                 f"same client, same name, more than one session "
                 f"&middot; whole market")
        + _persist_table_html(
            wf["one_way"], len(wf["round_trips"]),
            top_n=cap, show_badge=is_focus, source=src,
        )
    )

    # ── III · Class net flows ────────────────────────────────────────────────
    # The chart goes above the table: the shape of the week is the question a
    # reader brings to this section, and the exact totals are the follow-up.
    flow_chart = _class_flow_chart(
        wf.get("class_daily"), wf["class_flow"], trading_days,
    )
    section_iii = _section(
        "IV", "Net flows by client class",
        "who ended the week on which side &middot; whole market",
    )
    cum_chart = _cumulative_flow_chart(wf.get("cum_class"), trading_days)
    if cum_chart:
        section_iii += (
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0"><tr><td style="'
            f'{d.font(10.5, color=d.GOLD, weight="bold", ls=1.2, upper=True)}'
            f'padding:0 0 10px 0;">Foreign against domestic, cumulative</td>'
            f'</tr></table>'
            + cum_chart
            + f'<table role="presentation" width="100%" cellpadding="0" '
              f'cellspacing="0" border="0"><tr><td style="'
              f'{d.font(10, color=_INK_SOFT, italic=True, leading=16)}'
              f'text-align:justify;padding:14px 0 26px 0;">Each row is the '
              f'running net to the end of that session, not the session&rsquo;s '
              f'own flow, so the tracks show a position being built rather than '
              f'a set of daily results. Both share one scale, which is what '
              f'makes the gap between them readable. A class that did not trade '
              f'on a session carries its previous total forward: a day without '
              f'a deal is a day the position did not change, not a day it went '
              f'to zero. The per-session detail is directly '
              f'below.</td></tr></table>'
        )
    if cum_chart and flow_chart:
        section_iii += (
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0"><tr><td style="'
            f'{d.font(10.5, color=d.GOLD, weight="bold", ls=1.2, upper=True)}'
            f'padding:0 0 10px 0;">Every class, session by session</td>'
            f'</tr></table>'
        )
    if flow_chart:
        section_iii += (
            flow_chart
            + f'<table role="presentation" width="100%" cellpadding="0" '
              f'cellspacing="0" border="0"><tr><td style="'
              f'{d.font(10, color=_INK_SOFT, italic=True, leading=16)}'
              f'text-align:justify;padding:14px 0 26px 0;">Each bar is one '
              f'class&rsquo;s net for one session, measured from the centre rule '
              f'&mdash; left for a net sell, right for a net buy &mdash; and all '
              f'cells share one scale, so a row reads as a sequence and a column '
              f'reads as that session&rsquo;s balance of participants. A class '
              f'sitting on the centre line either did not trade that session or '
              f'finished it flat; the week-total table below separates those two. '
              f'A steady run of bars on one side is a position being worked '
              f'through the market, and a single tall bar is one holder acting '
              f'once &mdash; the same weekly net, but not the same '
              f'event.</td></tr></table>'
        )
    body += d.row(section_iii + _class_flow_table_html(wf["class_flow"], source=src))

    # ── IV · First appearances ───────────────────────────────────────────────
    # The floor applies to the read, not the record: the body reports only
    # material entrants, the PDF lists every absent name at any size.
    first_min_cr = FIRST_APPEARANCE_MIN_CR if is_focus else 0.0
    body += d.row(
        _section("V", "First appearances",
                 f"absent from the deal feeds for the prior "
                 f"{_n(LOOKBACK_WEEKS, 'week')} &middot; whole market"
                 + (f" &middot; {_fmt_cr(first_min_cr)}&nbsp;cr and above"
                    if first_min_cr else ""))
        + _first_appearance_html(
            wf["first_syms"], wf["first_clients"],
            top_n=cap, show_badge=is_focus, source=src, min_cr=first_min_cr,
        )
    )

    # ── V · Bulk by symbol ───────────────────────────────────────────────────
    body += d.row(
        _section("VI", "Bulk deals, by symbol",
                 f"{_n(len(frames['bulk_sym']), 'name')} &middot; week totals, "
                 f"sorted by value")
        + _sym_table_bars_html(
            frames["bulk_sym"], show_mcap=is_focus, source=src,
            caption=(
                "Quantities and values are summed across the week. The net "
                "direction bar is reported buys minus sells in quantity, drawn "
                "from a centre rule &mdash; left for a name distributed on net, "
                "right for one accumulated, and a bar of no length for one where "
                "the two sides balanced. Length is relative to the largest net in "
                "the table, so it ranks the imbalance rather than sizing it."
                + (" A bulk deal needs 0.5% of traded quantity to be reportable, "
                   "so large caps appear here only rarely." if is_focus else "")
            ),
        )
    )

    # ── VI · Block by symbol ─────────────────────────────────────────────────
    body += d.row(
        _section("VII", "Block deals, by symbol",
                 f"{_n(len(frames['block_sym']), 'name')} &middot; pre-open window, "
                 f"week totals")
        + _sym_table_bars_html(
            frames["block_sym"], show_mcap=is_focus, source=src,
            caption=("Negotiated trades in the pre-open block window, "
                     "08:45&ndash;09:00 IST, summed across the week. The net "
                     "direction bar reads as it does in the table above; a block "
                     "is negotiated between two named parties, so a balanced bar "
                     "here usually means both sides of one trade were "
                     "reportable."),
        )
    )

    # ── VII · Shorts ─────────────────────────────────────────────────────────
    body += d.row(
        _section("VIII",
                 "Short selling, by week value" if is_focus
                 else "Short selling, by week quantity",
                 "positions summed per name across the week")
        + _short_week_table_html(
            frames["short_week"], top_n=cap, show_mcap=is_focus, source=src,
            caption=(
                "Reported short positions summed over the week, with the number "
                "of sessions each name appeared on and its heaviest single "
                "session. The feed carries neither price nor client information; "
                "the indicative value is the week&rsquo;s quantity at the last "
                "known close, so it sizes the position rather than pricing the "
                "trades."
                if is_focus else
                "Positions summed over the week. Source feed carries no client "
                "information."
            ),
        )
    )

    # ── VIII / IX · Client compartments ──────────────────────────────────────
    # Ranked over the whole week, not the focus symbols: each class picking its
    # own largest companies is the point of the section, and feeding it the
    # already-narrowed focus set would rank the same ten names in every class.
    body += d.row(
        _section("IX", "Bulk deals, by client class",
                 "who was on the other side, compartmentalised"
                 + (f" &middot; top {FOCUS_TOP_N} per class by market cap, "
                    f"across the whole week" if is_focus else ""))
        + _class_compartments_html(
            wf["bulk_client"], top_n=class_cap, show_badge=is_focus, source=src,
        )
    )
    body += d.row(
        _section("X", "Block deals, by client class",
                 f"{_n(bkm['deals'], 'deal')} in scope &middot; pre-open window")
        + _class_compartments_html(
            wf["block_client"], top_n=class_cap, show_badge=is_focus, source=src,
        )
    )

    # ── XI / XII · Comprehensive-edition charts ──────────────────────────────
    # SVG, so they exist only where a browser renders the page: the PDF. Gated
    # on the edition rather than on a config flag, because the gate is a
    # statement about the medium, not a preference.
    if not is_focus:
        treemap = _treemap_svg(wf.get("class_sym"))
        if treemap:
            body += d.row(
                _section("XI", "The week in one frame",
                         "area is rupee value &middot; client class, then name")
                + treemap
                + f'<table role="presentation" width="100%" cellpadding="0" '
                  f'cellspacing="0" border="0"><tr><td style="'
                  f'{d.font(10, color=_INK_SOFT, italic=True, leading=16)}'
                  f'text-align:justify;padding:14px 0 0 0;">Each block is a client '
                  f'class and each tile inside it a name, both sized by the week&rsquo;s '
                  f'rupee value. A deal is counted under the class of the client on '
                  f'the reported side, so a name where a foreign fund sold to a '
                  f'corporate appears in both blocks &mdash; the same reason the '
                  f'class totals earlier do not sum to zero. Names past the seventh '
                  f'in a class are pooled into one tile rather than drawn as '
                  f'slivers. This chart is in the PDF only.</td></tr></table>'
            )

        scatter = _scatter_svg(wf.get("concentration"), wf["bulk"], wf["block"])
        if scatter:
            body += d.row(
                _section("XII", "Value against company size",
                         "where in the market the week actually happened")
                + scatter
                + f'<table role="presentation" width="100%" cellpadding="0" '
                  f'cellspacing="0" border="0"><tr><td style="'
                  f'{d.font(10, color=_INK_SOFT, italic=True, leading=16)}'
                  f'text-align:justify;padding:14px 0 0 0;">Both axes are '
                  f'logarithmic. Bulk-deal reportability is a percentage of traded '
                  f'quantity, so the feed leans structurally toward smaller '
                  f'companies and the left-hand cluster is the ordinary state of '
                  f'the world; a point far to the right is a large company that '
                  f'saw a genuine block, which is the rarer event and usually the '
                  f'one worth a second look. Market capitalisation comes from the '
                  f'security master, which is refreshed weekly, so a name that '
                  f'listed or moved sharply since the last refresh sits at a stale '
                  f'x-position. This chart is in the PDF only.</td></tr></table>'
            )

    # ── Attachment note ──────────────────────────────────────────────────────
    if is_focus:
        body += d.row(
            d.callout(
                f"The comprehensive edition &mdash; every deal in all "
                f"{n_full_names} names across the {_n(n_sessions, 'session')}, "
                f"with no market-cap filter and no row caps &mdash; is attached to "
                f"this message as a PDF, together with the raw bulk, block and "
                f"short-selling CSVs for the week. Nothing shown here is dropped "
                f"from that record, only deferred.",
                accent="navy",
            ),
            pad=f"20px {d.PAD_X}px 0 {d.PAD_X}px",
        )

    # ── Colophon ─────────────────────────────────────────────────────────────
    provenance = (
        "NSE archives &middot; bulk, block and short feeds &middot; notional "
        "values from the WATP column"
    )
    if is_focus:
        provenance += (
            " &middot; index membership from the NSE constituent files &middot; "
            "market capitalisation from Screener.in"
        )
    body += d.row(d.colophon(provenance, _DISCLAIMER))

    preheader = (
        f"{_n(bm['deals'], 'bulk deal')}, {_n(bkm['deals'], 'block deal')}, "
        f"{shm['deals']} short positions over {_n(n_sessions, 'session')}"
    )
    title = (
        f"Weekly Deals &mdash; NSE &mdash; {_house_range(start, end)} "
        f"&mdash; {edition_label}"
    )
    return d.doc_open(title, preheader) + body + d.DOC_CLOSE


# ─── Entry point ──────────────────────────────────────────────────────────────

def main(
    week_of: date | None = None,
    preview_path: str | None = None,
    eml_path: str | None = None,
) -> int:
    from dotenv import load_dotenv
    from utils.helpers import today_ist

    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    today = date.fromisoformat(today_ist())
    start, end, trading_days = _resolve_window(today, week_of)

    preview_mode = preview_path is not None
    eml_mode = eml_path is not None
    dry_run = preview_mode or eml_mode

    smtp_password = ""
    if eml_mode:
        # Real addresses when configured so the .eml matches what would ship,
        # but no password is needed because nothing is sent.
        smtp_user   = os.environ.get("SMTP_USER", "bac@brindco.com")
        recipients  = [
            r.strip()
            for r in os.environ.get(
                "REPORT_RECIPIENTS", "parv.bangar@brindco.com").split(",")
            if r.strip()
        ]
        sender_name = os.environ.get("REPORT_WEEKLY_SENDER_NAME", "BAC Weekly Deals")
    elif not dry_run:
        smtp_user     = _env("SMTP_USER")
        smtp_password = _env("SMTP_PASSWORD")
        recipients    = [r.strip() for r in _env("REPORT_RECIPIENTS").split(",") if r.strip()]
        sender_name   = os.environ.get("REPORT_WEEKLY_SENDER_NAME", "BAC Weekly Deals")

        # Claimed on the week's last trading day, so a Saturday and a Sunday
        # retry contend for one slot.
        if not _claim_slot(end, recipients):
            return 0
    else:
        smtp_user, recipients, sender_name = "", [], "BAC Weekly Deals"

    try:
        generated_at = datetime.now(timezone.utc)

        bulk_raw  = _fetch_range("bulk_deals",  start, end)
        block_raw = _fetch_range("block_deals", start, end)
        short_raw = _fetch_range("short_deals", start, end)
        logger.info(
            "Fetched for %s..%s: %d bulk, %d block, %d short",
            start, end, len(bulk_raw), len(block_raw), len(short_raw),
        )

        # Distinguish a quiet week from a failed collection, and a quiet session
        # from a swallowed one, before anything is rendered.
        degraded = _scrape_health(start, end)
        missing = _missing_sessions(trading_days, bulk_raw, block_raw)
        if degraded or missing:
            logger.error(
                "WEEK INCOMPLETE for %s..%s: degraded=%s missing_sessions=%s. "
                "Sending anyway, flagged in the subject and body; the job will "
                "exit non-zero so the run goes red.",
                start, end, ", ".join(degraded) or "none",
                ", ".join(dt.isoformat() for dt in missing) or "none",
            )

        bulk  = _enrich_bulk(bulk_raw)
        block = _enrich_block(block_raw)
        short = _enrich_short(short_raw)

        prior_symbols, prior_clients = _fetch_lookback_keys(start, LOOKBACK_WEEKS)

        # ── Comprehensive edition — the whole week, nothing dropped ──────────
        full = _derive(bulk, block, short, trading_days, prior_symbols, prior_clients)
        n_full_names = len(_all_symbols(bulk, block, short))

        # Week-on-week base. Fetched, not cached: a report that recomputes the
        # prior week from source is one that stays right when the prior week is
        # backfilled after the fact.
        p_start, p_end, _ = _prior_window(start)
        try:
            prior_metrics = _topline(
                _enrich_bulk(_fetch_range("bulk_deals",  p_start, p_end)),
                _enrich_block(_fetch_range("block_deals", p_start, p_end)),
                _enrich_short(_fetch_range("short_deals", p_start, p_end)),
            )
            logger.info(
                "Prior week %s..%s: bulk %s cr, block %s cr",
                p_start, p_end,
                prior_metrics["bulk"]["value_cr"], prior_metrics["block"]["value_cr"],
            )
        except Exception as exc:  # noqa: BLE001
            # A missing comparison is a missing column, not a missing report.
            logger.warning("Prior-week fetch failed (%s) — WoW suppressed", exc)
            prior_metrics = _topline(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())

        wow = _wow(full["metrics"], prior_metrics)

        highlights_full = _weekly_highlights(
            bulk, block, full["trend"], full["one_way"], full["class_flow"],
        )
        warn_card = _degraded_card(degraded, missing)
        if warn_card:
            highlights_full = [warn_card] + highlights_full

        html_full = _build_html(
            start, end, trading_days, full, wow, highlights_full, generated_at,
            edition=EDITION_FULL, n_full_names=n_full_names,
        )

        # ── Focused edition — the email body ────────────────────────────────
        scope = _focus_scope(bulk, block, short, n=FOCUS_TOP_N)
        focus_syms = _focus_union(scope)
        focus = _derive(
            _filter_symbols(bulk,  scope["bulk"]),
            _filter_symbols(block, scope["block"]),
            _filter_symbols(short, scope["short"]),
            trading_days, prior_symbols, prior_clients,
        )
        html_focus = _build_html(
            start, end, trading_days, focus, wow, highlights_full, generated_at,
            edition=EDITION_FOCUS,
            focus_symbols=focus_syms,
            full_metrics=full["metrics"],
            n_full_names=n_full_names,
            focus_basis=scope.get("_basis", "market_cap"),
            # Week-shaped sections stay on the whole market even in the body.
            week_frames=full,
        )

        pdf_bytes = render_pdf(html_full)

        if preview_mode:
            base = re.sub(r"\.html?$", "", preview_path, flags=re.I)
            with open(f"{base}.html", "w", encoding="utf-8") as fh:
                fh.write(html_focus)
            with open(f"{base}_comprehensive.html", "w", encoding="utf-8") as fh:
                fh.write(html_full)
            logger.info(
                "Preview saved: %s.html (email body) + %s_comprehensive.html",
                base, base,
            )
            if pdf_bytes:
                with open(f"{base}_comprehensive.pdf", "wb") as fh:
                    fh.write(pdf_bytes)
                logger.info(
                    "Preview PDF saved: %s_comprehensive.pdf (%.0f KB)",
                    base, len(pdf_bytes) / 1024,
                )
            return 0

        rng = _slug_range(start, end)
        attachments = {
            f"BAC_Weekly_Deals_NSE_{rng}_comprehensive.pdf": pdf_bytes or b"",
            f"bulk_deals_{rng}.csv":  _csv_bytes(bulk_raw),
            f"block_deals_{rng}.csv": _csv_bytes(block_raw),
            f"short_deals_{rng}.csv": _csv_bytes(short_raw),
        }
        if not pdf_bytes:
            logger.warning("Sending without the comprehensive PDF — rendering failed")

        # The subject is the only part guaranteed to be seen, so the warning
        # goes there too: a body banner is missable on a phone preview.
        subject = f"BAC Weekly Deals — NSE — {_plain_range(start, end)}"
        if degraded or missing:
            subject = f"[WEEK INCOMPLETE] {subject}"

        if eml_mode:
            msg = _build_message(
                sender=smtp_user, sender_name=sender_name, recipients=recipients,
                subject=subject, html=html_focus, attachments=attachments,
            )
            out = eml_path if eml_path.lower().endswith(".eml") else f"{eml_path}.eml"
            blob = msg.as_bytes()

            # Same guard the daily carries: a quoted-printable soft break must
            # be "=\r\n", or Outlook cannot rejoin the wrapped lines and the
            # HTML arrives as tag soup.
            bare_lf = blob.count(b"\n") - blob.count(b"\r\n")
            bad_soft = len(re.findall(rb"=(?<!\r=)\n", blob))
            if bare_lf or bad_soft:
                raise RuntimeError(
                    f"Refusing to write a malformed .eml: {bare_lf} bare LF and "
                    f"{bad_soft} non-CRLF quoted-printable soft breaks. The "
                    f"message policy must be email.policy.SMTP."
                )

            with open(out, "wb") as fh:
                fh.write(blob)
            logger.info(
                "Wrote %s (%.0f KB, CRLF verified) — From: %s  To: %s  attachments: %s",
                out, len(blob) / 1024, smtp_user, ", ".join(recipients),
                ", ".join(n for n, c in attachments.items() if c) or "none",
            )
            return 0

        _send_email(
            sender=smtp_user, password=smtp_password, sender_name=sender_name,
            recipients=recipients, subject=subject,
            html=html_focus, attachments=attachments,
        )
        _mark_sent(end)
        logger.info(
            "Sent weekly report for %s..%s to %s (body: %d focus names; "
            "persistent flows: %d one-way; PDF: %s)",
            start, end, recipients, len(focus_syms), len(full["one_way"]),
            f"{len(pdf_bytes) / 1024:.0f} KB" if pdf_bytes else "unavailable",
        )

        # The report has gone out either way — a partial read beats no read —
        # but an incomplete week must not look green, or the next gap goes
        # unnoticed exactly like the last one did.
        return 1 if (degraded or missing) else 0

    except Exception as exc:  # noqa: BLE001
        logger.exception("Weekly report generation failed")
        if not dry_run:
            _mark_failed(end, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate BAC Weekly Deals NSE report",
    )
    parser.add_argument(
        "--week-of", metavar="YYYY-MM-DD",
        help="Report on the Mon-Fri week containing this date "
             "(default: the week just finished)",
    )
    parser.add_argument(
        "--preview", metavar="PATH",
        help="Write previews instead of emailing (no DB slot needed). Produces "
             "PATH.html (the email body), PATH_comprehensive.html and "
             "PATH_comprehensive.pdf",
    )
    parser.add_argument(
        "--eml", metavar="PATH",
        help="Write the complete message as a .eml file instead of sending — "
             "body plus PDF and CSV attachments, exactly as it would go over "
             "SMTP. No password or DB slot needed.",
    )
    args = parser.parse_args()
    anchor = date.fromisoformat(args.week_of) if args.week_of else None
    sys.exit(main(anchor, preview_path=args.preview, eml_path=args.eml))
