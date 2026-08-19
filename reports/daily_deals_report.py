"""
Daily NSE deals email report — humanised newspaper format.

Runs once per trading-day morning (Tue–Sat IST), reporting on the previous
trading day's bulk / block / short deals.

Two editions of the same report are produced each run:

  * FOCUS — the email body. Scoped to the ten largest companies by market
    capitalisation among the day's deal names, each badged with its NIFTY index
    tier. Bulk deals trigger at >0.5% of traded quantity, which large-cap free
    floats almost never reach, so an index-membership filter (e.g. NIFTY50-only)
    would leave the body empty on the overwhelming majority of sessions. Ranking
    the day's actual names by market cap keeps the large-cap lens the desk wants
    while guaranteeing the body always carries the session's biggest names.

  * COMPREHENSIVE — every deal, unfiltered, rendered to a PDF and attached.
    Nothing is dropped from the record; the email body is a lens onto it.

Env vars required:
  SUPABASE_URL, SUPABASE_KEY
  SMTP_USER           — sending address (bac@brindco.com)
  SMTP_PASSWORD       — Google app-specific password for SMTP_USER
  REPORT_RECIPIENTS   — comma-separated list, e.g. parv.bangar@brindco.com
  REPORT_SENDER_NAME  — optional, defaults to "BAC Daily Deals"

Optional:
  REPORT_FOCUS_TOP_N  — how many companies the email body covers (default 10)
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formatdate, make_msgid
from html import escape as _e

import pandas as pd

from reports import client_class as cc
from reports import design as d
from reports.pdf_render import render_pdf
from utils import security_master as sm

logger = logging.getLogger("nse.report")

REPORT_TYPE = "daily_deals_email"
SHORT_MIN_QTY = 5_000

# How many companies the email body covers, largest by market cap first.
FOCUS_TOP_N = int(os.environ.get("REPORT_FOCUS_TOP_N", "10"))

EDITION_FULL = "full"
EDITION_FOCUS = "focus"

# ─── Palette ──────────────────────────────────────────────────────────────────
# Sourced from reports/design.py, which carries the BAC house style transcribed
# from the morning brief. No colour literal belongs in this file: the whole point
# of the shared module is that deals amber and morning-brief amber are the same
# hex by construction. These are local aliases for brevity only.
_INK        = d.INK
_INK_SOFT   = d.INK_SOFT
_STONE      = d.INK_FAINT
_PAPER      = d.PAPER
_NAVY       = d.NAVY
_NAVY_SOFT  = d.NAVY_SOFT
_GOLD       = d.GOLD
_RULE       = d.RULE
_BAND       = d.BAND
_GOOD       = d.GOOD
_BAD        = d.BAD
_WARN       = d.WARN

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _previous_trading_day(today: date) -> date:
    from utils.helpers import is_trading_day
    d = today - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _house_date(dt: date) -> str:
    """'Thu 30-Jul-2026' — the house dateline format.

    Parameter is `dt`, not `d`: `d` is the design module in this file's scope and
    shadowing it here would silently break every style call in the callee.
    """
    return dt.strftime("%a %d-%b-%Y")


def _fmt_cr(v: float) -> str:
    """Rupee crore. Zero or missing prints the em-dash, never '0'."""
    if v is None or v <= 0:
        return d.EM_DASH
    if v >= 1000:
        return f"&#8377;&nbsp;{v:,.0f}"
    if v >= 100:
        return f"&#8377;&nbsp;{v:.0f}"
    return f"&#8377;&nbsp;{v:.1f}"


def _fmt_qty(n: int | float) -> str:
    if not n or n == 0:
        return f'<span style="color:{_STONE};">{d.EM_DASH}</span>'
    return f"{int(n):,}"


def _vcr(qty, price) -> float:
    try:
        return round(float(qty) * float(price) / 1e7, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


# ─── Client classification ────────────────────────────────────────────────────
# Lives in reports/client_class.py, derived against all 1,280 distinct client
# names in the deal history and pinned by tests/test_client_class.py. The rules
# it replaced were keyword substrings with a greedy CORP fallback, which put
# 3,937 of 7,975 client rows into CORP — including 104 broker names and every
# quant desk that trades under a "SECURITIES" or "RESEARCH" title.

_TAG_COLOR = {
    cc.FII: _NAVY, cc.DII: _NAVY, cc.AIF: _NAVY,
    cc.HFT: _NAVY_SOFT, cc.PROP: _NAVY_SOFT, cc.STRAT: _NAVY_SOFT,
    cc.BRKR: _STONE, cc.CORP: _STONE, cc.HNI: _STONE, cc.TRUST: _STONE,
}


def _classify(name: str) -> str:
    return cc.classify(name)



def _tag_html(tag: str) -> str:
    return d.badge(tag, _TAG_COLOR.get(tag, _STONE)).lstrip("&nbsp;")


def _side_html(side: str) -> str:
    """Buy / sell / both. Direction is meaning, so it takes the semantic pair."""
    if side == "b":
        return f'<span style="{d.font(12, color=_GOOD, weight="bold")}">buy</span>'
    if side == "s":
        return f'<span style="{d.font(12, color=_BAD, weight="bold")}">sell</span>'
    return f'<span style="{d.font(12, color=_STONE)}">both</span>'


# ─── Index tier badges ────────────────────────────────────────────────────────

_TIER_STYLE = {
    sm.TIER_NIFTY50:  (_NAVY,      "NIFTY50"),
    sm.TIER_NIFTY100: (_NAVY_SOFT, "NIFTY100"),
    sm.TIER_NIFTY500: (_STONE,     "NIFTY500"),
}


def _index_badge(symbol: str) -> str:
    """Index-tier badge, or '' for names outside the NIFTY500."""
    style = _TIER_STYLE.get(sm.tier(symbol))
    if not style:
        return ""
    color, label = style
    return d.badge(label, color)


def _fmt_mcap(symbol: str) -> str:
    """Market cap as a compact rupee figure — lakh crore above 1,00,000 cr."""
    v = sm.market_cap_cr(symbol)
    if v is None:
        return f'<span style="color:{_STONE};">{d.EM_DASH}</span>'
    if v >= 100_000:
        return f"&#8377;&nbsp;{v / 100_000:.2f}&nbsp;L&nbsp;cr"
    return f"&#8377;&nbsp;{v:,.0f}&nbsp;cr"


# ─── Focus selection (email body scope) ──────────────────────────────────────

def _all_symbols(*frames: pd.DataFrame) -> list[str]:
    out: list[str] = []
    for df in frames:
        if df is not None and not df.empty and "symbol" in df.columns:
            out.extend(df["symbol"].dropna().astype(str).tolist())
    return list(dict.fromkeys(out))


def _focus_scope(
    bulk: pd.DataFrame, block: pd.DataFrame, short: pd.DataFrame,
    n: int = FOCUS_TOP_N,
) -> dict[str, list[str]]:
    """Top n by market cap *per dataset*, not once across all three.

    A single global ranking is the obvious reading of "top 10 by market cap"
    and it produces a near-empty email: the ten largest companies transacting
    on any given day are almost never the ones with reportable bulk or block
    deals (a bulk deal needs 0.5% of traded quantity, which mega-cap free
    floats do not reach). Ranking within each dataset gives every section its
    own ten largest names, so each one carries the biggest names that actually
    appear in it.
    """
    candidates = _all_symbols(bulk, block, short)
    known, total = sm.coverage(candidates)
    if total and known < total:
        logger.info(
            "Market-cap coverage: %d/%d of today's names (%d unknown, ranked out). "
            "Refresh with: python -m scrapers.security_master_refresh --from-deals",
            known, total, total - known,
        )

    scope = {
        "bulk":  sm.rank_by_market_cap(_all_symbols(bulk), n),
        "block": sm.rank_by_market_cap(_all_symbols(block), n),
        "short": sm.rank_by_market_cap(_all_symbols(short), n),
    }

    # Fallback: market caps unavailable (security master missing, or never
    # seeded past index membership) but deals exist. Ranking by deal value
    # keeps the email useful instead of shipping an empty body; the banner
    # says which basis was used so nobody reads it as a market-cap ranking.
    if known == 0 and total > 0:
        logger.warning(
            "No market caps available for any of today's %d names — falling back "
            "to deal-value ranking. Seed with: "
            "python -m scrapers.security_master_refresh --from-deals",
            total,
        )
        scope = {
            "bulk":  _rank_by_value(bulk, n),
            "block": _rank_by_value(block, n),
            "short": _rank_by_qty(short, n),
            "_basis": "value",
        }

    for name, syms in scope.items():
        if name.startswith("_"):
            continue
        logger.info("Focus scope [%s]: %d names — %s", name, len(syms), ", ".join(syms) or "none")
    return scope


def _rank_by_value(df: pd.DataFrame, n: int) -> list[str]:
    if df is None or df.empty or "value_cr" not in df.columns:
        return []
    agg = df.groupby("symbol")["value_cr"].sum().sort_values(ascending=False)
    return [str(s) for s in agg.head(n).index]


def _rank_by_qty(df: pd.DataFrame, n: int) -> list[str]:
    if df is None or df.empty or "quantity" not in df.columns:
        return []
    agg = df.groupby("symbol")["quantity"].sum().sort_values(ascending=False)
    return [str(s) for s in agg.head(n).index]


def _focus_union(scope: dict[str, list[str]]) -> list[str]:
    """All focus names across sections, largest first — for the scope banner."""
    seen: list[str] = []
    for key, syms in scope.items():
        if key.startswith("_"):
            continue
        seen.extend(syms)
    ranked = sm.rank_by_market_cap(seen)
    # Under the deal-value fallback nothing has a market cap, so ranking drops
    # everything; keep first-seen order instead of returning an empty list.
    return ranked or list(dict.fromkeys(seen))


def _filter_symbols(df: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    if df is None or df.empty or "symbol" not in df.columns or not symbols:
        return df.iloc[0:0].copy() if df is not None and not df.empty else pd.DataFrame()
    return df[df["symbol"].astype(str).isin(set(symbols))].copy()


# ─── Data-integrity guard ─────────────────────────────────────────────────────

_DEAL_DATASETS = ("bulk_deals", "block_deals", "short_deals")


def _scrape_health(report_date: date) -> list[str]:
    """Datasets that are empty *because the scrape failed*, not because the
    session was quiet.

    An empty section is ambiguous -- "no deals were reported" and "we could not
    fetch the deals" render identically -- and only the scrape log can tell them
    apart. Conflating them is exactly how 2026-08-17's 100 bulk deals and
    2026-08-18's bulk + block deals were mailed out as "none", twice, to the
    full distribution list, with every run reporting success.

    A dataset is degraded when it has no rows for report_date AND its most
    recent scrape did not succeed. Rows present, or a clean scrape that simply
    found nothing, are both fine.

    Never raises: a broken guard must not be able to stop the report.
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
            got = client.table(ds).select("id", count="exact").eq(
                "deal_date", report_date.isoformat()).limit(1).execute()
            if (got.count or 0) > 0:
                continue
            last = client.table("scrape_run_log").select("status").eq(
                "dataset", ds).order("start_time", desc=True).limit(1).execute()
            status = last.data[0]["status"] if last.data else None
            if status != "success":
                degraded.append(ds)
                logger.error(
                    "%s: no rows for %s and last scrape status=%s", ds, report_date, status
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Scrape-health check failed for %s: %s", ds, exc)
    return degraded


def _degraded_card(degraded: list[str]) -> dict:
    names = ", ".join(d.replace("_", " ") for d in degraded)
    return {
        "title": "Data incomplete &mdash; do not read empty sections as zero",
        "body": (
            f"The {names} feed could not be collected for this session, so the "
            f"section below is blank because the data is missing, not because "
            f"there were no deals. Treat it as unavailable pending a re-run."
        ),
    }


# ─── Idempotency ──────────────────────────────────────────────────────────────

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
        logger.info("Slot for %s already claimed (%s) — exiting.", report_date, type(exc).__name__)
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


# ─── Data fetch ───────────────────────────────────────────────────────────────

def _fetch(table: str, report_date: date) -> pd.DataFrame:
    from database.client import get_client
    client = get_client()
    page, page_size, out = 0, 1000, []
    while True:
        resp = (
            client.table(table).select("*")
            .eq("deal_date", report_date.isoformat())
            .range(page * page_size, (page + 1) * page_size - 1)
            .execute()
        )
        chunk = resp.data or []
        out.extend(chunk)
        if len(chunk) < page_size:
            break
        page += 1
    return pd.DataFrame(out)


# ─── Data enrichment ──────────────────────────────────────────────────────────

def _enrich_bulk(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["quantity"] = df["quantity"].fillna(0).astype("int64")
    df["value_cr"] = df.apply(
        lambda r: _vcr(r["quantity"], r.get("avg_price")), axis=1
    )
    return df


def _enrich_block(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["quantity"] = df["quantity"].fillna(0).astype("int64")
    df["value_cr"] = df.apply(
        lambda r: _vcr(r["quantity"], r.get("trade_price")), axis=1
    )
    return df


def _enrich_short(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["quantity"] = df["quantity"].fillna(0).astype("int64")
    return df


# ─── Aggregations ─────────────────────────────────────────────────────────────

def _topline(bulk: pd.DataFrame, block: pd.DataFrame, short: pd.DataFrame) -> dict:
    def _side_totals(df: pd.DataFrame) -> tuple[int, int, float]:
        if df.empty:
            return 0, 0, 0.0
        deals = len(df)
        names = df["symbol"].nunique() if "symbol" in df.columns else 0
        # Use max(buy_value, sell_value) per symbol to avoid double-counting
        if "value_cr" in df.columns and "buy_sell" in df.columns:
            sym_val = df.groupby(["symbol", "buy_sell"])["value_cr"].sum().reset_index()
            val = sym_val.groupby("symbol")["value_cr"].max().sum()
        else:
            val = 0.0
        return deals, names, round(float(val), 1)

    bd, bn, bv = _side_totals(bulk)
    bkd, bkn, bkv = _side_totals(block)
    if not short.empty:
        sd = len(short)
        sn = int((short["quantity"] > SHORT_MIN_QTY).sum())
    else:
        sd, sn = 0, 0

    return {
        "bulk":  {"deals": bd, "names": bn, "value_cr": bv},
        "block": {"deals": bkd, "names": bkn, "value_cr": bkv},
        "short": {"deals": sd, "above_threshold": sn},
    }


def _sym_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Per-symbol aggregation: buy qty, sell qty, total ₹ Cr, crossed flag."""
    if df.empty:
        return pd.DataFrame()
    df = df.copy()

    grp = (
        df.groupby(["symbol", "security_name", "buy_sell"], dropna=False)
        .agg(qty=("quantity", "sum"), vcr=("value_cr", "sum"))
        .reset_index()
    )
    wide = grp.pivot_table(
        index=["symbol", "security_name"],
        columns="buy_sell",
        values=["qty", "vcr"],
        fill_value=0,
    )
    wide.columns = [f"{'buy' if s == 'B' else 'sell'}_{v}" for v, s in wide.columns]
    wide = wide.reset_index()

    bq = wide.get("buy_qty", pd.Series(0, index=wide.index))
    sq = wide.get("sell_qty", pd.Series(0, index=wide.index))
    bv = wide.get("buy_vcr", pd.Series(0.0, index=wide.index))
    sv = wide.get("sell_vcr", pd.Series(0.0, index=wide.index))

    wide["buy_qty"]  = bq.fillna(0).astype("int64")
    wide["sell_qty"] = sq.fillna(0).astype("int64")
    wide["buy_vcr"]  = bv.fillna(0.0)
    wide["sell_vcr"] = sv.fillna(0.0)
    wide["total_vcr"] = wide.apply(lambda r: max(r["buy_vcr"], r["sell_vcr"]), axis=1)
    wide = wide.sort_values("total_vcr", ascending=False)

    def _flag(row) -> str:
        bq_, sq_ = row["buy_qty"], row["sell_qty"]
        if bq_ == 0 or sq_ == 0:
            return "▲"
        return "=" if min(bq_, sq_) / max(bq_, sq_) >= 0.95 else "▲"

    wide["flag"] = wide.apply(_flag, axis=1)
    return wide.reset_index(drop=True)


def _short_rows(df: pd.DataFrame, min_qty: int = SHORT_MIN_QTY) -> pd.DataFrame:
    """Shorts above a quantity floor, largest first.

    The floor exists to strip noise from a market-wide feed. It is a *share
    count*, so it scales with price in exactly the wrong direction: 5,000
    shares of a ₹13 stock is ₹65,000 while 5,000 shares of RELIANCE is ₹65
    lakh. The focused edition passes min_qty=0 because ranking by market cap
    has already done the filtering, and applying both would hide most of the
    large-cap short activity the section exists to show.
    """
    if df.empty:
        return pd.DataFrame()
    df = df[df["quantity"] > min_qty].copy() if min_qty else df.copy()
    return df.sort_values("quantity", ascending=False).reset_index(drop=True)


def _client_rows(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (client, symbol) with side, qty, ₹ Cr, class tag."""
    if df.empty or "client_name" not in df.columns:
        return pd.DataFrame()
    df = df.copy()

    agg = (
        df.groupby(["client_name", "symbol", "security_name", "buy_sell"], dropna=False)
        .agg(qty=("quantity", "sum"), vcr=("value_cr", "sum"))
        .reset_index()
    )

    by_cs = (
        agg.groupby(["client_name", "symbol", "security_name"])
        .apply(lambda g: pd.Series({
            "buy_qty": int(g.loc[g["buy_sell"] == "B", "qty"].sum()),
            "sell_qty": int(g.loc[g["buy_sell"] == "S", "qty"].sum()),
            "vcr": float(g["vcr"].sum()),
        }))
        .reset_index()
    )

    def _side(row) -> str:
        b, s = row["buy_qty"] > 0, row["sell_qty"] > 0
        return "b·s" if (b and s) else ("b" if b else "s")

    by_cs["qty"] = by_cs["buy_qty"] + by_cs["sell_qty"]
    by_cs["side"] = by_cs.apply(_side, axis=1)
    by_cs["class"] = by_cs["client_name"].apply(_classify)
    by_cs["mcap_cr"] = by_cs["symbol"].apply(
        lambda s: sm.market_cap_cr(str(s)) or 0.0
    )
    by_cs["_ctot"] = by_cs.groupby("client_name")["vcr"].transform("sum")
    # Sort by client total desc, then value desc within client — but keep
    # client_name as a tiebreaker so two clients with identical totals (e.g. the
    # two custody legs of a symmetric basket cross) never interleave. The
    # continuation-marker Client column in _client_table_html requires each
    # client's rows to be contiguous; without this tiebreaker they alternate.
    by_cs = by_cs.sort_values(
        ["_ctot", "client_name", "vcr"], ascending=[False, True, False]
    ).drop(columns="_ctot")
    return by_cs.reset_index(drop=True)


def _client_rows_by_class(
    client: pd.DataFrame, top_n: int | None = None,
) -> list[tuple[str, pd.DataFrame, int, float]]:
    """Split client rows into class compartments, largest company first.

    Returns [(class, rows, total_rows_in_class, total_vcr_in_class), ...] in
    CLASS_ORDER, skipping classes with no activity. `top_n` truncates each
    compartment; the untruncated count is still returned so the section can say
    what it is not showing rather than implying the class had only ten trades.

    Ranking is by the market capitalisation of the traded company, so the
    compartment answers "what are the biggest names this class touched" — not
    "what were its biggest tickets", which is what value-ranking would give.
    Ties break on deal value so a class trading several mega caps still leads
    with its largest ticket among them.
    """
    if client is None or client.empty:
        return []

    out: list[tuple[str, pd.DataFrame, int, float]] = []
    for tag in cc.CLASS_ORDER:
        rows = client[client["class"] == tag]
        if rows.empty:
            continue
        n_total = len(rows)
        vcr_total = float(rows["vcr"].sum())
        ranked = rows.sort_values(
            ["mcap_cr", "vcr"], ascending=[False, False]
        ).reset_index(drop=True)
        if top_n:
            ranked = ranked.head(top_n)
        out.append((tag, ranked, n_total, vcr_total))
    return out


# ─── Top names (combined bulk + block) for the SVG chart ─────────────────────

def _top_names(bulk: pd.DataFrame, block: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    frames = []
    for df in (bulk, block):
        if not df.empty and "value_cr" in df.columns:
            frames.append(df.groupby("symbol")["value_cr"].sum().reset_index())
    if not frames:
        return pd.DataFrame(columns=["symbol", "value_cr"])
    combined = pd.concat(frames).groupby("symbol")["value_cr"].sum().reset_index()
    return combined.sort_values("value_cr", ascending=False).head(n).reset_index(drop=True)


# ─── Auto-highlights (4 cards from data) ─────────────────────────────────────

def _highlights(bulk: pd.DataFrame, block: pd.DataFrame) -> list[dict]:
    cards: list[dict] = []
    all_parts = []
    for df, kind in [(block, "block"), (bulk, "bulk")]:
        if not df.empty:
            tmp = df.copy()
            tmp["_kind"] = kind
            all_parts.append(tmp)
    if not all_parts:
        return cards

    all_ = pd.concat(all_parts, ignore_index=True)
    all_ = all_[all_["value_cr"] > 0] if "value_cr" in all_.columns else pd.DataFrame()
    if all_.empty:
        return cards

    used_syms: set[str] = set()

    # Card 1 — largest single deal (lead card)
    top = all_.nlargest(1, "value_cr").iloc[0]
    sym1 = top["symbol"]
    used_syms.add(sym1)
    vcr1 = top["value_cr"]
    client1 = str(top.get("client_name", "")).strip() or "Unknown"
    side1 = "bought" if top["buy_sell"] == "B" else "sold"
    qty1 = int(top["quantity"])
    qty_m1 = qty1 / 1e6
    kind1 = str(top.get("_kind", "bulk"))
    sec1 = str(top.get("security_name", sym1) or sym1)
    cards.append({
        "lead": True,
        "tag": "Largest deal of the day",
        "tag_right": "by value",
        "title": (
            f'{_e(sym1)} &nbsp;·&nbsp; '
            f'<span style="font-variant-numeric:tabular-nums;">'
            f'{_fmt_cr(vcr1)}</span>'
            f'<span style="font-size:18px; color:{_STONE}; font-style:italic; font-weight:400;"> cr</span>'
        ),
        "body": (
            f'{_e(client1)} {side1} {qty_m1:.2f}&nbsp;million shares of '
            f'<em>{_e(sec1)}</em> in a {kind1} deal &mdash; '
            f'the largest rupee print of the session.'
        ),
    })

    # Card 2 — largest asymmetric flow
    if "buy_sell" in all_.columns:
        sym_sides = all_.groupby("symbol").apply(lambda g: pd.Series({
            "buy_qty": int(g.loc[g.buy_sell == "B", "quantity"].sum()),
            "sell_qty": int(g.loc[g.buy_sell == "S", "quantity"].sum()),
            "total_vcr": float(g["value_cr"].sum()),
        })).reset_index()
        sym_sides["gap"] = (sym_sides["buy_qty"] - sym_sides["sell_qty"]).abs()
        sym_sides["gap_pct"] = sym_sides.apply(
            lambda r: r["gap"] / max(r["buy_qty"], r["sell_qty"])
            if max(r["buy_qty"], r["sell_qty"]) > 0 else 0.0,
            axis=1,
        )
        asym = sym_sides[
            ~sym_sides["symbol"].isin(used_syms)
            & (sym_sides["gap_pct"] > 0.10)
            & (sym_sides["total_vcr"] > 2)
        ].nlargest(1, "total_vcr")
        if not asym.empty:
            ar = asym.iloc[0]
            sym2, bq2, sq2, gap2 = ar["symbol"], int(ar["buy_qty"]), int(ar["sell_qty"]), int(ar["gap"])
            used_syms.add(sym2)
            dom = "Buy" if bq2 > sq2 else "Sell"
            dom_n = max(bq2, sq2)
            other_n = min(bq2, sq2)
            gap_m = gap2 / 1e6
            cards.append({
                "tag": "an asymmetric flow &mdash;",
                "tag_color": _WARN,
                "title": (
                    f'{_e(sym2)} &nbsp;<span style="color:{_STONE}; font-style:italic;">·</span>&nbsp; '
                    f'<span style="font-variant-numeric:tabular-nums;">{gap_m:.1f}m</span>'
                    f'<span style="font-size:13px; color:{_STONE}; font-style:italic; font-weight:400;"> gap</span>'
                ),
                "body": (
                    f'{dom} side took {dom_n / 1e6:.1f}&thinsp;m shares; '
                    f'other side delivered {other_n / 1e6:.1f}&thinsp;m &mdash; '
                    f'a {gap2:,} share imbalance worth a second look.'
                ),
            })

    # Card 3 — most active multi-symbol client
    if "client_name" in all_.columns:
        multi = (
            all_.groupby("client_name")["symbol"].nunique()
            .reset_index(name="n")
            .query("n >= 3")
        )
        if not multi.empty:
            mc_row = multi.nlargest(1, "n").iloc[0]
            mc_name = mc_row["client_name"]
            mc_n = int(mc_row["n"])
            mc_syms = list(all_.loc[all_.client_name == mc_name, "symbol"].unique())[:5]
            mc_is_rt = all_[all_.client_name == mc_name]["buy_sell"].nunique() > 1
            sym_str = ", ".join(mc_syms) + (" &amp; more" if mc_n > len(mc_syms) else "")
            rt_note = " Every position buy equals sell." if mc_is_rt else ""
            cards.append({
                "tag": "the props were busy &mdash;",
                "tag_color": _NAVY,
                "title": (
                    f'{_e(mc_name)} &nbsp;<span style="color:{_STONE}; font-style:italic;">·</span>&nbsp; '
                    f'{mc_n} names'
                ),
                "body": f'Active across {sym_str}.{rt_note}',
            })

    # Card 4 — notable one-sided strategic or institutional position
    if "client_name" in all_.columns:
        class_map = all_[["client_name"]].drop_duplicates().copy()
        class_map["class"] = class_map["client_name"].apply(_classify)
        notable_classes = {"STRAT", "DII/MF", "FII"}
        notable_clients = class_map[class_map["class"].isin(notable_classes)]["client_name"]
        notable = all_[all_["client_name"].isin(notable_clients)]
        if not notable.empty:
            # Find clients with only sells (exits) or only buys (entries)
            cs = notable.groupby("client_name")["buy_sell"].unique()
            pure = cs[cs.apply(lambda x: len(x) == 1)]
            if not pure.empty:
                pure_df = notable[notable["client_name"].isin(pure.index)]
                pure_df = pure_df[~pure_df["symbol"].isin(used_syms)]
                if not pure_df.empty:
                    nr = pure_df.nlargest(1, "value_cr").iloc[0]
                    n_sym4 = nr["symbol"]
                    used_syms.add(n_sym4)
                    n_client = str(nr.get("client_name", ""))
                    n_side = "sell" if nr["buy_sell"] == "S" else "buy"
                    n_vcr = nr["value_cr"]
                    n_qty = int(nr["quantity"])
                    action = "reduced" if n_side == "sell" else "added to"
                    cards.append({
                        "tag": f"a strategic {n_side} &mdash;",
                        "tag_color": _NAVY_SOFT,
                        "title": (
                            f'{_e(n_sym4)} &nbsp;<span style="color:{_STONE}; font-style:italic;">·</span>&nbsp; '
                            f'<span style="font-variant-numeric:tabular-nums;">{_fmt_cr(n_vcr)}</span>'
                            f'<span style="font-size:13px; color:{_STONE}; font-style:italic; font-weight:400;"> cr</span>'
                        ),
                        "body": (
                            f'{_e(n_client)} {action} {n_sym4} &mdash; '
                            f'{n_qty:,} shares at {_fmt_cr(n_vcr)}&thinsp;cr today.'
                        ),
                    })

    return cards[:5]


# ─── Derived-frame bundle ────────────────────────────────────────────────────

def _derive(
    bulk: pd.DataFrame, block: pd.DataFrame, short: pd.DataFrame,
    short_min_qty: int = SHORT_MIN_QTY,
) -> dict:
    """Runs every aggregation over one scope of deals.

    Called twice per run — once for the comprehensive edition and once for the
    market-cap-focused email body — so the two editions cannot drift apart in
    how they compute totals.
    """
    return {
        "bulk": bulk,
        "block": block,
        "short": short,
        "metrics": _topline(bulk, block, short),
        "bulk_sym": _sym_rows(bulk),
        "block_sym": _sym_rows(block),
        "short_filt": _short_rows(short, min_qty=short_min_qty),
        "bulk_client": _client_rows(bulk),
        "block_client": _client_rows(block),
        "top": _top_names(bulk, block, n=10),
        "highlights": _highlights(bulk, block),
    }


# ─── Presentation (BAC house style — see reports/design.py) ──────────────────

def _bar_chart(top: pd.DataFrame) -> str:
    """Horizontal bars built from table cells — no image, no CSS bar.

    An <img> would be blocked by Outlook until the reader opts in, and a CSS bar
    needs width on a div, which Word ignores. Nested cells with bgcolor are the
    only construction that renders everywhere on first open.
    """
    if top.empty:
        return ""
    max_val = float(top["value_cr"].max())
    if max_val <= 0:
        return ""

    BAR_PX = 300
    rows = []
    for i, r in enumerate(top.itertuples()):
        bar_w = max(3, int(float(r.value_cr) / max_val * BAR_PX))
        fill = d.SERIES[0] if i else d.GOLD      # leader in gold, rest in navy
        rows.append(
            f'<tr>'
            f'<td width="104" align="right" style="width:104px;'
            f'{d.font(11.5, weight="bold")}padding:3px 9px 3px 0;white-space:nowrap;">'
            f'{_e(str(r.symbol))}</td>'
            f'<td style="padding:3px 0;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="border-collapse:collapse;"><tr>'
            f'<td width="{bar_w}" bgcolor="{fill}" style="width:{bar_w}px;height:13px;'
            f'font-size:0;line-height:0;">&nbsp;</td>'
            f'</tr></table></td>'
            f'<td width="92" style="width:92px;{d.font(11.5, color=d.INK_SOFT)}'
            f'padding:3px 0 3px 9px;white-space:nowrap;">{_fmt_cr(float(r.value_cr))}&nbsp;cr</td>'
            f'</tr>'
        )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="border-collapse:collapse;">' + "".join(rows) + "</table>"
    )


# ─── Section renderers ────────────────────────────────────────────────────────

_ROMAN = ["", "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]


def _section(num: str, title: str, standfirst: str = "") -> str:
    """The gold caption that heads every section, with an optional standfirst.

    House convention: gold uppercase caption, explanation *after* the table as an
    italic caption rather than before it.
    """
    head = f"{num} &middot; {title}" if num else title
    out = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td style="{d.font(10.5, color=d.GOLD, weight="bold", ls=1.6, upper=True)}'
        f'padding:0 0 {"6px" if standfirst else "14px"} 0;">{head}</td></tr>'
    )
    if standfirst:
        out += (
            f'<tr><td style="{d.font(10.5, color=d.INK_FAINT)}padding:0 0 14px 0;">'
            f'{standfirst}</td></tr>'
        )
    return out + "</table>"


def _sym_table_html(sym: pd.DataFrame, show_mcap: bool = False,
                    source: str = "", caption: str = "") -> str:
    """Per-symbol table (bulk or block).

    `show_mcap` adds the index badge and a market-cap column — used by the
    focused edition, where the reader needs to see why these names were picked.
    """
    if sym.empty:
        return d.datatable([], [], empty="No deals in scope.")

    cols = ["Symbol", "Security"] + (["Mkt cap"] if show_mcap else []) + \
           ["Buy qty", "Sell qty", "&#8377; cr", ""]
    align = ["l", "l"] + (["r"] if show_mcap else []) + ["r", "r", "r", "c"]

    rows = []
    for r in sym.itertuples():
        s = str(r.symbol)
        flag = str(r.flag)
        # A crossed deal is clean, an asymmetric one is worth a second look —
        # that is a judgement, so it takes the semantic pair.
        flag_html = (
            f'<span style="color:{_GOOD};font-weight:bold;">=</span>' if flag == "="
            else f'<span style="color:{_WARN};font-weight:bold;">&#9650;</span>'
        )
        row = [f'<strong>{_e(s)}</strong>{_index_badge(s) if show_mcap else ""}',
               f'<span style="color:{_INK_SOFT};">{_e(str(r.security_name or s))}</span>']
        if show_mcap:
            row.append(_fmt_mcap(s))
        row += [
            _fmt_qty(int(getattr(r, "buy_qty", 0))),
            _fmt_qty(int(getattr(r, "sell_qty", 0))),
            f'<strong>{_fmt_cr(float(r.total_vcr))}</strong>',
            flag_html,
        ]
        rows.append(row)

    return d.datatable(cols, rows, align=align, source=source, caption=caption)


def _fmt_notional(qty: int, symbol: str) -> str:
    """Indicative rupee value of a short position, from the last known close.

    The short-selling feed carries no price, so without this a reader cannot
    compare 20,000 shares of one name against 600 of another. Marked indicative
    because the price is the security master's, not the trade date's.
    """
    px = sm.last_price(symbol)
    if not px or not qty:
        return f'<span style="color:{_STONE};">{d.EM_DASH}</span>'
    val_cr = qty * px / 1e7
    if val_cr >= 1:
        return f"&#8377;&nbsp;{val_cr:,.1f}&nbsp;cr"
    lakh = qty * px / 1e5
    if lakh >= 1:
        return f"&#8377;&nbsp;{lakh:,.1f}&nbsp;L"
    return f"&#8377;&nbsp;{qty * px:,.0f}"


def _short_table_html(short: pd.DataFrame, show_mcap: bool = False,
                      source: str = "", caption: str = "") -> str:
    if short.empty:
        return d.datatable([], [], empty="No qualifying positions.")

    # The focused edition drops the share-count floor, so order by rupee value
    # instead — otherwise a 20,000-share position in a small cap outranks a
    # materially larger position in a mega cap.
    if show_mcap:
        short = short.copy()
        short["_notional"] = [
            (sm.last_price(str(s)) or 0) * float(q or 0)
            for s, q in zip(short["symbol"], short["quantity"])
        ]
        short = short.sort_values("_notional", ascending=False).reset_index(drop=True)

    cols = ["Symbol", "Security"] + (["Mkt cap"] if show_mcap else []) + \
           ["Quantity"] + (["&#8776; value"] if show_mcap else [])
    align = ["l", "l"] + (["r"] if show_mcap else []) + ["r"] + (["r"] if show_mcap else [])

    rows = []
    for _, r in short.iterrows():
        s = str(r.get("symbol", ""))
        qty = int(r.get("quantity", 0))
        row = [f'<strong>{_e(s)}</strong>{_index_badge(s) if show_mcap else ""}',
               f'<span style="color:{_INK_SOFT};">{_e(str(r.get("security_name", s) or s))}</span>']
        if show_mcap:
            row.append(_fmt_mcap(s))
        row.append(f"{qty:,}")
        if show_mcap:
            row.append(f'<strong>{_fmt_notional(qty, s)}</strong>')
        rows.append(row)

    return d.datatable(cols, rows, align=align, source=source, caption=caption)


def _client_table_html(client: pd.DataFrame, show_badge: bool = False,
                       source: str = "", caption: str = "") -> str:
    """One row per client-symbol pair.

    The client column repeats as a blank on continuation rows rather than using
    rowspan: Word's engine mishandles rowspan inside a nested presentation table,
    and a blank continuation reads the same without the risk.
    """
    if client.empty:
        return d.datatable([], [], empty="No client data.")

    rows = []
    prev = None
    for _, r in client.iterrows():
        cn = str(r["client_name"])
        s = str(r["symbol"])
        first = cn != prev
        rows.append([
            (f'<strong>{_e(cn)}</strong>' if first
             else f'<span style="color:{_STONE};">&#8942;</span>'),
            _tag_html(str(r["class"])),
            f'{_e(s)}{_index_badge(s) if show_badge else ""}',
            _side_html(str(r["side"])),
            f'{int(r["qty"]):,}',
            f'<strong>{_fmt_cr(float(r["vcr"]))}</strong>',
        ])
        prev = cn

    return d.datatable(
        ["Client", "Class", "Symbol", "Side", "Qty", "&#8377; cr"],
        rows,
        align=["l", "c", "l", "c", "r", "r"],
        source=source,
        caption=caption,
    )


_CLASS_LEGEND = (
    "Class tags &mdash; <b>FII</b> foreign portfolio investor, <b>DII/MF</b> domestic "
    "mutual fund or insurer, <b>AIF</b> alternative investment fund, <b>HFT</b> "
    "high-frequency or quant prop, <b>PROP</b> proprietary trading, <b>BRKR</b> broker, "
    "<b>CORP</b> corporate, <b>STRAT</b> strategic or promoter, <b>HNI</b> individual, "
    "<b>TRUST</b> family office or trust."
)

_CLASS_CAVEAT = (
    "Class is inferred from the client name, which is all the deal feed carries. "
    "Named desks are pinned; the rest is pattern-matched. Broker and proprietary "
    "desks are the least separable &mdash; nothing in &ldquo;X Securities Pvt "
    "Ltd&rdquo; says which it is &mdash; so an unidentified securities firm is "
    "counted as a broker rather than guessed into a prop desk."
)


def _class_compartments_html(
    client: pd.DataFrame,
    *,
    top_n: int | None,
    show_badge: bool,
    source: str,
) -> str:
    """One table per client class, ranked by the market cap of the company traded."""
    compartments = _client_rows_by_class(client, top_n=top_n)
    if not compartments:
        return d.datatable([], [], empty="No client data.")

    blocks: list[str] = []
    for tag, rows, n_total, vcr_total in compartments:
        shown = len(rows)
        # Say what is being withheld. "Top 10 of 47" is a different claim from
        # "10", and only one of them is true when the class traded 47 times.
        if top_n and n_total > shown:
            standfirst = (
                f"top {shown} of {n_total} trades by market cap "
                f"&middot; {_fmt_cr(vcr_total)}&nbsp;cr across the class"
            )
        else:
            standfirst = (
                f"{n_total} trade{'' if n_total == 1 else 's'} "
                f"&middot; {_fmt_cr(vcr_total)}&nbsp;cr"
            )

        table_rows = []
        prev = None
        for _, r in rows.iterrows():
            cn = str(r["client_name"])
            s = str(r["symbol"])
            first = cn != prev
            table_rows.append([
                (f'<strong>{_e(cn)}</strong>' if first
                 else f'<span style="color:{_STONE};">&#8942;</span>'),
                f'{_e(s)}{_index_badge(s) if show_badge else ""}',
                _fmt_mcap(s),
                _side_html(str(r["side"])),
                f'{int(r["qty"]):,}',
                f'<strong>{_fmt_cr(float(r["vcr"]))}</strong>',
            ])
            prev = cn

        blocks.append(
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0"><tr><td style="padding:0 0 26px 0;">'
            + _class_heading(tag, standfirst)
            + d.datatable(
                ["Client", "Symbol", "Mkt cap", "Side", "Qty", "&#8377; cr"],
                table_rows,
                align=["l", "l", "r", "c", "r", "r"],
                # Pinned so every compartment lines up with the ones above and
                # below it; without this each class table sizes to its own
                # longest client name and the stack reads as ragged.
                widths=[196, 104, 82, 40, 84, 62],
            )
            + '</td></tr></table>'
        )

    return (
        "".join(blocks)
        + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
          f'border="0"><tr><td style="{d.font(10.5, color=_STONE)}padding:4px 0 0 0;">'
          f'{source}</td></tr>'
          f'<tr><td style="{d.font(10, color=_INK_SOFT, italic=True, leading=16)}'
          f'text-align:justify;padding:12px 0 0 0;">{_CLASS_CAVEAT}</td></tr></table>'
    )


def _class_heading(tag: str, standfirst: str) -> str:
    """A class compartment header: navy chip, full name, then the standfirst."""
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0"><tr>'
        f'<td width="52" style="width:52px;vertical-align:middle;padding:0 8px 8px 0;">'
        f'{_tag_html(tag)}</td>'
        f'<td style="vertical-align:middle;padding:0 0 8px 0;">'
        f'<span style="{d.font(11.5, color=_INK, weight="bold")}">'
        f'{cc.CLASS_LABEL.get(tag, tag)}</span>'
        f'<span style="{d.font(10.5, color=_STONE)}"> &middot; {standfirst}</span>'
        f'</td>'
        f'</tr></table>'
    )


# ─── Editorial blocks ────────────────────────────────────────────────────────

def _highlights_html(cards: list[dict]) -> str:
    """The 'things that matter' gold callout."""
    if not cards:
        return ""
    items = [
        (_strip_html(c.get("title", "")), _strip_html(c.get("body", "")))
        for c in cards[:4]
    ]
    inner = d.numbered_list(items)
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background-color:{d.GOLD_PALE};border-left:4px solid {d.GOLD};">'
        f'<tr><td style="padding:12px 14px;">'
        f'{d.caption_title("Things that matter today")}{inner}'
        f'</td></tr></table>'
    )


def _scope_banner(
    focus_symbols: list[str],
    full_metrics: dict,
    n_full_names: int,
    basis: str = "market_cap",
) -> str:
    """Explains the focused edition's scope and points at the attached PDF.

    Without this the reader cannot tell a quiet session from a filtered one —
    the single most important thing to be honest about in a scoped report.
    """
    fbm, fbkm = full_metrics["bulk"], full_metrics["block"]
    n50 = sum(1 for s in focus_symbols if sm.is_nifty50(s))

    if focus_symbols and basis == "value":
        lead = (
            f'Each section below carries its own {len(focus_symbols)} largest positions '
            f'<strong>by deal size</strong>, not by market capitalisation &mdash; no '
            f'market-cap data was available for today&rsquo;s names, so the usual '
            f'ranking could not be applied.'
        )
    elif focus_symbols:
        lead = (
            f'Each section below carries its own ten largest companies by market '
            f'capitalisation &mdash; {len(focus_symbols)} distinct names in all'
            + (f', {n50} of them NIFTY50 constituents' if n50 else '')
            + '. Sections are ranked independently because the biggest names in the '
            'short-selling feed are rarely the ones with reportable bulk or block deals.'
        )
    elif n_full_names == 0:
        lead = 'No bulk, block or short-selling activity was reported for this session.'
    else:
        lead = (
            f'None of today&rsquo;s {n_full_names} names could be ranked by market '
            f'capitalisation &mdash; the security master needs a refresh.'
        )

    body = (
        f'{lead} Across the whole market the session carried '
        f'<strong>{fbm["deals"]}</strong> bulk deal{"" if fbm["deals"] == 1 else "s"} in '
        f'<strong>{fbm["names"]}</strong> name{"" if fbm["names"] == 1 else "s"} '
        f'({_fmt_cr(fbm["value_cr"])}&nbsp;cr) and '
        f'<strong>{fbkm["deals"]}</strong> block deal{"" if fbkm["deals"] == 1 else "s"} in '
        f'<strong>{fbkm["names"]}</strong> name{"" if fbkm["names"] == 1 else "s"} '
        f'({_fmt_cr(fbkm["value_cr"])}&nbsp;cr).'
    )
    return d.callout(body, accent="navy", title="What you are reading")


# ─── Full HTML assembly ───────────────────────────────────────────────────────

_DISCLAIMER = (
    "Internal research document. Generated automatically from NSE exchange files. "
    "All figures are prior-session end-of-day vintage; there is no intraday data "
    "path in this build. Not investment advice."
)


def _build_html(
    report_date: date,
    bulk: pd.DataFrame,
    block: pd.DataFrame,
    short: pd.DataFrame,
    bulk_sym: pd.DataFrame,
    block_sym: pd.DataFrame,
    short_filt: pd.DataFrame,
    bulk_client: pd.DataFrame,
    block_client: pd.DataFrame,
    top: pd.DataFrame,
    metrics: dict,
    highlights: list[dict],
    generated_at: datetime,
    edition: str = EDITION_FULL,
    focus_symbols: list[str] | None = None,
    full_metrics: dict | None = None,
    n_full_names: int = 0,
    short_min_qty: int = SHORT_MIN_QTY,
    focus_basis: str = "market_cap",
    class_bulk_client: pd.DataFrame | None = None,
    class_block_client: pd.DataFrame | None = None,
) -> str:
    bm, bkm, shm = metrics["bulk"], metrics["block"], metrics["short"]
    is_focus = edition == EDITION_FOCUS
    focus_symbols = focus_symbols or []

    import pytz
    gen_ist = generated_at.astimezone(pytz.timezone("Asia/Kolkata"))

    def _n(count: int, word: str) -> str:
        return f"{count} {word}{'' if count == 1 else 's'}"

    # ── Masthead ─────────────────────────────────────────────────────────────
    if is_focus:
        edition_label = "Focus edition"
        scope = (
            "Scoped to the largest companies by market capitalisation in each "
            "section. The comprehensive record is attached as a PDF."
        )
    else:
        edition_label = "Comprehensive edition"
        scope = (
            "Every bulk, block and short-selling record reported for the session, "
            "unfiltered."
        )

    head = d.masthead(
        kicker="Brindco Alpha Capital &middot; Quant Desk",
        title="Daily Deals",
        dateline=f"<strong>{_house_date(report_date)}</strong> &middot; {edition_label}",
        subline=(
            f"National Stock Exchange of India &middot; generated "
            f"{gen_ist.strftime('%d-%b-%Y %H:%M')} IST"
        ),
        scope=scope,
    )

    # ── Topline ──────────────────────────────────────────────────────────────
    kpis = [
        {"label": "Bulk deals",
         "value": f"{_fmt_cr(bm['value_cr'])}<span style=\"font-size:13px;color:{_STONE};\"> cr</span>",
         "sub": f"{_n(bm['deals'], 'deal')}, {_n(bm['names'], 'name')}"},
        {"label": "Block deals",
         "value": f"{_fmt_cr(bkm['value_cr'])}<span style=\"font-size:13px;color:{_STONE};\"> cr</span>",
         "sub": f"{_n(bkm['deals'], 'deal')}, {_n(bkm['names'], 'name')}"},
        {"label": "Short selling",
         "value": f"{shm['deals']}<span style=\"font-size:13px;color:{_STONE};\"> positions</span>",
         "sub": ("all sizes shown" if not short_min_qty
                 else f"{shm['above_threshold']} over threshold")},
    ]

    body = head
    body += d.row(d.kpi_grid(kpis), pad=d.BLOCK_PAD)

    if is_focus:
        body += d.row(
            _scope_banner(focus_symbols, full_metrics or metrics, n_full_names, focus_basis),
            pad=d.BLOCK_PAD,
        )

    hl = _highlights_html(highlights)
    if hl:
        body += d.row(hl, pad=d.BLOCK_PAD)

    # ── ii · Top names by value ──────────────────────────────────────────────
    chart = _bar_chart(top)
    if chart:
        body += d.row(
            _section("II", "Top names by value", "bulk and block combined")
            + chart
            + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
              f'<tr><td style="{d.font(10, color=_INK_SOFT, italic=True, leading=16)}'
              f'text-align:justify;padding:14px 0 0 0;">The larger of the buy or sell '
              f'side is shown per name, so a crossed deal counts once.'
              f'{" Scoped to the focus names." if is_focus else ""}</td></tr></table>'
        )

    src = f"NSE archives &middot; as of {_house_date(report_date)}"

    # ── iii · Bulk by symbol ─────────────────────────────────────────────────
    body += d.row(
        _section("III", "Bulk deals, by symbol",
                 f"{_n(len(bulk_sym), 'name')} &middot; sorted by value")
        + _sym_table_html(
            bulk_sym, show_mcap=is_focus, source=src,
            caption=("Triangle marks asymmetric quantities; equals marks a clean "
                     "crossed deal." +
                     (" A bulk deal needs 0.5% of traded quantity to be reportable, "
                      "so large caps appear here only rarely." if is_focus else "")),
        )
    )

    # ── iv · Block by symbol ─────────────────────────────────────────────────
    body += d.row(
        _section("IV", "Block deals, by symbol",
                 f"{_n(len(block_sym), 'name')} &middot; pre-open window")
        + _sym_table_html(
            block_sym, show_mcap=is_focus, source=src,
            caption="Negotiated trades in the pre-open block window, 08:45&ndash;09:00 IST.",
        )
    )

    # ── v · Shorts ───────────────────────────────────────────────────────────
    body += d.row(
        _section("V",
                 "Short selling, by value" if is_focus else "Short selling, by quantity",
                 ("all positions in these names" if is_focus
                  else ("every reported position" if not short_min_qty
                        else f"positions &#8805; {short_min_qty:,} shares")))
        + _short_table_html(
            short_filt, show_mcap=is_focus, source=src,
            caption=(
                "Every intraday short position in these names, whatever the size &mdash; "
                "the five-thousand-share floor used market-wide would hide most "
                "large-cap activity, since the same share count is far more money in a "
                "mega cap. Ordered by indicative rupee value, priced at the last known "
                "close: the feed itself carries neither price nor client information."
                if is_focus else
                "Source feed carries no client information."
            ),
        )
    )

    # ── vi · Bulk by client, compartmentalised by class ──────────────────────
    # These compartments rank over the WHOLE session, not the focus symbols.
    # Each class picking its own ten largest companies is the point of the
    # section; feeding it the already-narrowed focus set would rank ten names
    # against the same ten names in every class. The comprehensive edition shows
    # every trade in each class, so the PDF stays the full record.
    class_top_n = FOCUS_TOP_N if is_focus else None
    cbc = class_bulk_client if class_bulk_client is not None else bulk_client
    cbk = class_block_client if class_block_client is not None else block_client
    body += d.row(
        _section("VI", "Bulk deals, by client class",
                 "who was on the other side, compartmentalised"
                 + (f" &middot; top {FOCUS_TOP_N} per class by market cap, "
                    f"across the whole session" if is_focus else ""))
        + _class_compartments_html(
            cbc, top_n=class_top_n, show_badge=is_focus, source=src,
        )
    )

    # ── vii · Block by client, compartmentalised by class ────────────────────
    body += d.row(
        _section("VII", "Block deals, by client class",
                 f"{_n(bkm['deals'], 'deal')} &middot; pre-open window")
        + _class_compartments_html(
            cbk, top_n=class_top_n, show_badge=is_focus, source=src,
        )
    )

    # ── Attachment note ──────────────────────────────────────────────────────
    if is_focus:
        body += d.row(
            d.callout(
                f"The comprehensive edition &mdash; every deal in all {n_full_names} "
                f"names, with no market-cap filter and no size floor &mdash; is attached "
                f"to this message as a PDF, together with the raw bulk, block and "
                f"short-selling CSVs. Nothing shown here is dropped from that record, "
                f"only deferred.",
                accent="navy",
            ),
            pad=f"20px {d.PAD_X}px 0 {d.PAD_X}px",
        )

    # ── Colophon ─────────────────────────────────────────────────────────────
    provenance = (
        f"NSE archives &middot; bulk, block and short feeds &middot; notional values "
        f"from the WATP column"
    )
    if is_focus:
        provenance += (
            " &middot; index membership from the NSE constituent files &middot; "
            "market capitalisation from Screener.in"
        )
    body += d.row(d.colophon(provenance, _DISCLAIMER))

    preheader = (
        f"{_n(bm['deals'], 'bulk deal')}, {_n(bkm['deals'], 'block deal')}, "
        f"{shm['deals']} short positions"
    )
    title = (
        f"Daily Deals &mdash; NSE &mdash; {_house_date(report_date)} "
        f"&mdash; {edition_label}"
    )
    return d.doc_open(title, preheader) + body + d.DOC_CLOSE


# ─── Email ────────────────────────────────────────────────────────────────────

def _build_message(
    *, sender: str, sender_name: str, recipients: list[str],
    subject: str, html: str, attachments: dict[str, bytes],
) -> EmailMessage:
    """Assembles the outgoing message.

    Split out from _send_email so `--eml` can write the exact bytes that would
    go over SMTP — a hand-rolled preview would not prove the real MIME
    structure or attachment types.

    Attachment MIME type is inferred from the extension: a PDF sent as text/csv
    is rejected or mangled by most clients.

    policy=SMTP_POLICY pins CRLF line endings. The default policy uses bare LF,
    which smtplib silently corrects on send but which corrupts a message
    serialised straight to a .eml file: quoted-printable soft line breaks become
    "=\\n" instead of the RFC 2045 "=\\r\\n", and strict decoders (Outlook) then
    fail to rejoin the wrapped lines and render the HTML as tag soup.
    """
    msg = EmailMessage(policy=SMTP_POLICY)
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender}>"
    msg["To"] = ", ".join(recipients)
    # Real MTAs stamp these; set them so a .eml written to disk is a complete
    # message rather than one Outlook shows with an empty date.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or "brindco.com")
    msg.set_content("This report requires an HTML-capable email client.")
    msg.add_alternative(html, subtype="html")
    for filename, content in attachments.items():
        if not content:
            continue
        if filename.lower().endswith(".pdf"):
            maintype, subtype = "application", "pdf"
        else:
            maintype, subtype = "text", "csv"
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def _send_email(
    *, sender: str, password: str, sender_name: str,
    recipients: list[str], subject: str, html: str,
    attachments: dict[str, bytes],
) -> None:
    msg = _build_message(
        sender=sender, sender_name=sender_name, recipients=recipients,
        subject=subject, html=html, attachments=attachments,
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def _csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8") if not df.empty else b""


# ─── Slack (incoming webhook) ─────────────────────────────────────────────────

_HTML_ENTITIES = [
    ("&mdash;", "—"), ("&ndash;", "–"), ("&nbsp;", " "), ("&amp;", "&"),
    ("&thinsp;", " "), ("&hellip;", "…"), ("&middot;", "·"), ("&bull;", "•"),
    ("&lt;", "<"), ("&gt;", ">"), ("&sup2;", "²"),
]

def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    for ent, rep in _HTML_ENTITIES:
        s = s.replace(ent, rep)
    return re.sub(r" {2,}", " ", s).strip()


def _fmt_cr_plain(v: float) -> str:
    if v <= 0:
        return "—"
    if v >= 1000:
        return f"₹ {v:,.0f} cr"
    if v >= 100:
        return f"₹ {v:.0f} cr"
    return f"₹ {v:.1f} cr"


def _slack_s(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def _code_table(headers: list[str], widths: list[int], rows: list[list[str]]) -> str:
    def fmt(cells: list[str]) -> str:
        return "  ".join(str(c)[:w].ljust(w) for c, w in zip(cells, widths))
    lines = ["```", fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in rows]
    lines.append("```")
    return "\n".join(lines)


def _build_slack_blocks(
    report_date: date,
    bulk_sym: pd.DataFrame,
    block_sym: pd.DataFrame,
    short_filt: pd.DataFrame,
    bulk_client: pd.DataFrame,
    block_client: pd.DataFrame,
    top: pd.DataFrame,
    metrics: dict,
    highlights: list[dict],
) -> list[dict]:
    bm  = metrics["bulk"]
    bkm = metrics["block"]
    shm = metrics["short"]   # not `sm` — that name is the security_master module
    blocks: list[dict] = []

    _months = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
    date_str = f"{report_date.strftime('%A')}, {report_date.day} {_months[report_date.month]} {report_date.year}"

    # ── Masthead ──────────────────────────────────────────────────────────────
    blocks.append({"type": "header", "text": {"type": "plain_text",
        "text": f"Daily Deals — NSE — {report_date.strftime('%d %b %Y')}"}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "Brindco Alpha Capital  ◆  _a daily note from the quant desk_"}]})
    blocks.append(_slack_s(f"_{date_str}_"))
    blocks.append({"type": "divider"})

    # ── Topline metrics ───────────────────────────────────────────────────────
    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*Bulk Deals*\n{_fmt_cr_plain(bm['value_cr'])}  ·  {bm['deals']} deals  ·  {bm['names']} names"},
        {"type": "mrkdwn", "text": f"*Block Deals*\n{_fmt_cr_plain(bkm['value_cr'])}  ·  {bkm['deals']} deals  ·  {bkm['names']} names"},
        {"type": "mrkdwn", "text": f"*Short Selling*\n{shm['deals']} positions  ·  {shm['above_threshold']} above 5,000 shares"},
    ]})
    blocks.append({"type": "divider"})

    # ── Highlights ────────────────────────────────────────────────────────────
    if highlights:
        blocks.append(_slack_s("*i.  Highlights*"))
        for card in highlights[:4]:
            tag   = _strip_html(card.get("tag", ""))
            title = _strip_html(card.get("title", ""))
            body  = _strip_html(card.get("body", ""))
            blocks.append(_slack_s(f"*{tag}*\n*{title}*\n{body}"))
        blocks.append({"type": "divider"})

    # ── Top names by value (bar chart) ────────────────────────────────────────
    if not top.empty:
        blocks.append(_slack_s("*ii.  Top names by value*\n_Bulk and block combined_"))
        max_val = float(top["value_cr"].max())
        lines = []
        for row in top.head(10).itertuples():
            bar = "█" * max(1, int(float(row.value_cr) / max_val * 20))
            lines.append(f"`{str(row.symbol):<14}` {bar}  {_fmt_cr_plain(float(row.value_cr))}")
        blocks.append(_slack_s("\n".join(lines)))
        blocks.append({"type": "divider"})

    # ── Bulk by symbol ────────────────────────────────────────────────────────
    if not bulk_sym.empty:
        blocks.append(_slack_s(f"*iii.  Bulk Deals, by symbol*\n_{bm['names']} names · sorted by value_"))
        rows = []
        for row in bulk_sym.head(30).itertuples():
            bq = int(getattr(row, "buy_qty", 0))
            sq = int(getattr(row, "sell_qty", 0))
            rows.append([str(row.symbol), f"{bq:,}" if bq else "—",
                         f"{sq:,}" if sq else "—", _fmt_cr_plain(float(row.total_vcr)), str(row.flag)])
        tbl = _code_table(["Symbol", "Buy Qty", "Sell Qty", "₹ Cr", "Flg"],
                          [14, 14, 14, 14, 3], rows)
        if len(bulk_sym) > 30:
            tbl += f"\n_…and {len(bulk_sym) - 30} more_"
        blocks.append(_slack_s(tbl))
        blocks.append({"type": "divider"})

    # ── Block by symbol ───────────────────────────────────────────────────────
    if not block_sym.empty:
        blocks.append(_slack_s(f"*iv.  Block Deals, by symbol*\n_{bkm['names']} names · pre-open block window_"))
        rows = []
        for row in block_sym.itertuples():
            bq = int(getattr(row, "buy_qty", 0))
            sq = int(getattr(row, "sell_qty", 0))
            rows.append([str(row.symbol), f"{bq:,}" if bq else "—",
                         f"{sq:,}" if sq else "—", _fmt_cr_plain(float(row.total_vcr)), str(row.flag)])
        blocks.append(_slack_s(_code_table(["Symbol", "Buy Qty", "Sell Qty", "₹ Cr", "Flg"],
                                           [14, 14, 14, 14, 3], rows)))
        blocks.append({"type": "divider"})

    # ── Short selling ─────────────────────────────────────────────────────────
    blocks.append(_slack_s("*v.  Short Selling*\n_Intraday positions ≥ 5,000 shares_"))
    if not short_filt.empty:
        rows = [[str(r.get("symbol", "")), f"{int(r.get('quantity', 0)):,}"]
                for _, r in short_filt.head(30).iterrows()]
        blocks.append(_slack_s(_code_table(["Symbol", "Quantity"], [16, 14], rows)))
    else:
        blocks.append(_slack_s("_No qualifying short positions today._"))
    blocks.append({"type": "divider"})

    # ── Bulk by client ────────────────────────────────────────────────────────
    if not bulk_client.empty:
        blocks.append(_slack_s("*vi.  Bulk Deals, by client*\n_One row per client–symbol pair, ordered by total value descending_"))
        rows = []
        for _, r in bulk_client.head(20).iterrows():
            rows.append([str(r["client_name"])[:22], str(r["class"]),
                         str(r["symbol"]), str(r["side"]), _fmt_cr_plain(float(r["vcr"]))])
        tbl = _code_table(["Client", "Class", "Symbol", "Side", "₹ Cr"],
                          [24, 7, 12, 5, 14], rows)
        if len(bulk_client) > 20:
            tbl += f"\n_…and {len(bulk_client) - 20} more_"
        blocks.append(_slack_s(tbl))
        blocks.append({"type": "divider"})

    # ── Block by client ───────────────────────────────────────────────────────
    if not block_client.empty:
        blocks.append(_slack_s(f"*vii.  Block Deals, by client*\n_{bkm['deals']} deal{'s' if bkm['deals'] != 1 else ''} · pre-open window_"))
        rows = []
        for _, r in block_client.iterrows():
            rows.append([str(r["client_name"])[:22], str(r["class"]),
                         str(r["symbol"]), str(r["side"]), _fmt_cr_plain(float(r["vcr"]))])
        blocks.append(_slack_s(_code_table(["Client", "Class", "Symbol", "Side", "₹ Cr"],
                                           [24, 7, 12, 5, 14], rows)))
        blocks.append({"type": "divider"})

    # ── Colophon ──────────────────────────────────────────────────────────────
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"◆  ◆  ◆  Data from NSE archives — bulk, block, and short feeds.  {report_date.strftime('%d %b %Y')}"}]})

    return blocks[:50]  # Slack hard limit


def _send_slack(webhook_url: str, blocks: list[dict], report_date: date) -> None:
    import json
    payload = json.dumps({
        "text": f"BAC Daily Deals — NSE — {report_date.strftime('%d %b %Y')}",
        "blocks": blocks,
    }).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"Slack webhook {resp.status}: {body}")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main(
    report_date_override: date | None = None,
    preview_path: str | None = None,
    eml_path: str | None = None,
) -> int:
    from dotenv import load_dotenv
    from utils.helpers import today_ist

    load_dotenv()  # pick up .env if present (also loaded by database.client via config)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    today = date.fromisoformat(today_ist())
    report_date = report_date_override or _previous_trading_day(today)
    logger.info("Building report for %s (today IST = %s)", report_date, today)

    preview_mode = preview_path is not None
    eml_mode = eml_path is not None
    dry_run = preview_mode or eml_mode

    if eml_mode:
        # Real addresses when configured so the .eml matches what would ship,
        # but no password is needed — nothing is sent. Placeholders keep the
        # flag usable on a machine that has no SMTP config at all.
        smtp_user   = os.environ.get("SMTP_USER", "bac@brindco.com")
        recipients  = [
            r.strip()
            for r in os.environ.get("REPORT_RECIPIENTS", "parv.bangar@brindco.com").split(",")
            if r.strip()
        ]
        sender_name = os.environ.get("REPORT_SENDER_NAME", "BAC Daily Deals")
    elif not dry_run:
        smtp_user     = _env("SMTP_USER")
        smtp_password = _env("SMTP_PASSWORD")
        recipients    = [r.strip() for r in _env("REPORT_RECIPIENTS").split(",") if r.strip()]
        sender_name   = os.environ.get("REPORT_SENDER_NAME", "BAC Daily Deals")

        if not _claim_slot(report_date, recipients):
            return 0

    try:
        generated_at = datetime.now(timezone.utc)

        bulk_raw  = _fetch("bulk_deals",  report_date)
        block_raw = _fetch("block_deals", report_date)
        short_raw = _fetch("short_deals", report_date)
        logger.info("Fetched: %d bulk, %d block, %d short", len(bulk_raw), len(block_raw), len(short_raw))

        # Distinguish "quiet session" from "collection failed" before anything
        # is rendered, so a blank section can be labelled rather than implied.
        degraded = _scrape_health(report_date)
        if degraded:
            logger.error(
                "DATA INCOMPLETE for %s: %s. Sending anyway, flagged in the "
                "subject and body; the job will exit non-zero so the run goes red.",
                report_date, ", ".join(degraded),
            )

        bulk  = _enrich_bulk(bulk_raw)
        block = _enrich_block(block_raw)
        short = _enrich_short(short_raw)

        # ── Comprehensive edition — every deal, rendered to the attached PDF ──
        # short_min_qty=0: the 5,000-share floor is a noise filter for a screen
        # read, and it drops ~80% of reported short positions. The PDF is the
        # record of the session, so it carries all of them.
        full = _derive(bulk, block, short, short_min_qty=0)
        n_full_names = len(_all_symbols(bulk, block, short))
        if degraded:
            full["highlights"] = [_degraded_card(degraded)] + full["highlights"]

        html_full = _build_html(
            report_date, full["bulk"], full["block"], full["short"],
            full["bulk_sym"], full["block_sym"], full["short_filt"],
            full["bulk_client"], full["block_client"],
            full["top"], full["metrics"], full["highlights"], generated_at,
            edition=EDITION_FULL,
            short_min_qty=0,
        )

        # ── Focused edition — the email body ─────────────────────────────────
        scope = _focus_scope(bulk, block, short, n=FOCUS_TOP_N)
        focus_syms = _focus_union(scope)
        focus = _derive(
            _filter_symbols(bulk,  scope["bulk"]),
            _filter_symbols(block, scope["block"]),
            _filter_symbols(short, scope["short"]),
            # Market-cap ranking is already the filter; the share-count floor
            # would hide most large-cap shorts on top of it.
            short_min_qty=0,
        )
        if degraded:
            focus["highlights"] = [_degraded_card(degraded)] + focus["highlights"]
        html_focus = _build_html(
            report_date, focus["bulk"], focus["block"], focus["short"],
            focus["bulk_sym"], focus["block_sym"], focus["short_filt"],
            focus["bulk_client"], focus["block_client"],
            focus["top"], focus["metrics"], focus["highlights"], generated_at,
            edition=EDITION_FOCUS,
            focus_symbols=focus_syms,
            full_metrics=full["metrics"],
            n_full_names=n_full_names,
            short_min_qty=0,
            focus_basis=scope.get("_basis", "market_cap"),
            # Class compartments rank over the full session, not the focus set.
            class_bulk_client=full["bulk_client"],
            class_block_client=full["block_client"],
        )

        pdf_bytes = render_pdf(html_full)

        if preview_mode:
            base = re.sub(r"\.html?$", "", preview_path, flags=re.I)
            with open(f"{base}.html", "w", encoding="utf-8") as fh:
                fh.write(html_focus)
            with open(f"{base}_comprehensive.html", "w", encoding="utf-8") as fh:
                fh.write(html_full)
            logger.info("Preview saved: %s.html (email body) + %s_comprehensive.html", base, base)
            if pdf_bytes:
                with open(f"{base}_comprehensive.pdf", "wb") as fh:
                    fh.write(pdf_bytes)
                logger.info("Preview PDF saved: %s_comprehensive.pdf (%.0f KB)",
                            base, len(pdf_bytes) / 1024)
            return 0

        attachments = {
            f"BAC_Daily_Deals_NSE_{report_date}_comprehensive.pdf": pdf_bytes or b"",
            f"bulk_deals_{report_date}.csv":  _csv_bytes(bulk_raw),
            f"block_deals_{report_date}.csv": _csv_bytes(block_raw),
            f"short_deals_{report_date}.csv": _csv_bytes(short_raw),
        }
        if not pdf_bytes:
            logger.warning("Sending without the comprehensive PDF — rendering failed")

        pretty_date = report_date.strftime("%d %b %Y")
        # The subject is the only part guaranteed to be seen, so the warning
        # goes there too -- a body banner is missable on a phone preview.
        subject = f"BAC Daily Deals — NSE — {pretty_date}"
        if degraded:
            subject = f"[DATA INCOMPLETE] {subject}"

        if eml_mode:
            # Same builder the SMTP path uses, so this file is the message
            # byte-for-byte rather than an approximation of it.
            msg = _build_message(
                sender=smtp_user, sender_name=sender_name, recipients=recipients,
                subject=subject,
                html=html_focus, attachments=attachments,
            )
            out = eml_path if eml_path.lower().endswith(".eml") else f"{eml_path}.eml"
            blob = msg.as_bytes()

            # Guard the exact defect that made the first .eml unreadable in
            # Outlook: a quoted-printable soft break must be "=\r\n". Bare LF
            # leaves the decoder unable to rejoin wrapped lines, and the HTML
            # arrives as tag soup with every text node swallowed.
            bare_lf = blob.count(b"\n") - blob.count(b"\r\n")
            bad_soft = len(re.findall(rb"=(?<!\r=)\n", blob))
            if bare_lf or bad_soft:
                raise RuntimeError(
                    f"Refusing to write a malformed .eml: {bare_lf} bare LF and "
                    f"{bad_soft} non-CRLF quoted-printable soft breaks. The "
                    f"message policy must be email.policy.SMTP."
                )

            # Binary mode: text mode on Windows would rewrite the CRLFs we just
            # verified into CRCRLF.
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
            recipients=recipients,
            subject=subject,
            html=html_focus, attachments=attachments,
        )
        _mark_sent(report_date)
        logger.info(
            "Sent report for %s to %s (body: %d focus names; PDF: %s)",
            report_date, recipients, len(focus_syms),
            f"{len(pdf_bytes) / 1024:.0f} KB" if pdf_bytes else "unavailable",
        )

        slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
        if slack_webhook:
            try:
                # Slack stays on the comprehensive scope — it is a channel
                # notification, not the curated morning read.
                slack_blocks = _build_slack_blocks(
                    report_date, full["bulk_sym"], full["block_sym"],
                    full["short_filt"], full["bulk_client"], full["block_client"],
                    full["top"], full["metrics"], full["highlights"],
                )
                _send_slack(slack_webhook, slack_blocks, report_date)
                logger.info("Slack notification sent for %s", report_date)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Slack send failed (non-fatal): %s", exc)

        # The report has gone out either way -- a partial read beats no read --
        # but a degraded run must not look green, or the next gap goes unnoticed
        # exactly like the last two did.
        return 1 if degraded else 0

    except Exception as exc:  # noqa: BLE001
        logger.exception("Report generation failed")
        if not dry_run:
            _mark_failed(report_date, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate BAC Daily Deals NSE report")
    parser.add_argument("--date", help="Override report date (YYYY-MM-DD)")
    parser.add_argument("--preview", metavar="PATH",
                        help="Write previews instead of emailing (no DB slot needed). "
                             "Produces PATH.html (the email body), "
                             "PATH_comprehensive.html and PATH_comprehensive.pdf")
    parser.add_argument("--eml", metavar="PATH",
                        help="Write the complete message as a .eml file instead of "
                             "sending — body plus PDF and CSV attachments, exactly "
                             "as it would go over SMTP. Open it in any mail client "
                             "to verify. No password or DB slot needed.")
    args = parser.parse_args()
    override = date.fromisoformat(args.date) if args.date else None
    sys.exit(main(override, preview_path=args.preview, eml_path=args.eml))
