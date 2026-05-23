"""
Daily NSE deals email report — humanised newspaper format.

Runs once per trading-day morning (Tue–Sat IST), reporting on the previous
trading day's bulk / block / short deals.

Env vars required:
  SUPABASE_URL, SUPABASE_KEY
  SMTP_USER           — sending address (bac@brindco.com)
  SMTP_PASSWORD       — Google app-specific password for SMTP_USER
  REPORT_RECIPIENTS   — comma-separated list, e.g. parv.bangar@brindco.com
  REPORT_SENDER_NAME  — optional, defaults to "BAC Daily Deals"
"""

from __future__ import annotations

import logging
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape as _e

import pandas as pd

logger = logging.getLogger("nse.report")

REPORT_TYPE = "daily_deals_email"
SHORT_MIN_QTY = 5_000

# ─── Palette (matches the humanised template) ─────────────────────────────────
_INK        = "#1a1410"
_INK_SOFT   = "#3a322a"
_STONE      = "#837763"
_PARCHMENT  = "#faf5e8"
_SAND       = "#e8dec8"
_TAN        = "#c6b896"
_CREAM      = "#fcf8ec"
_WARM_GREY  = "#f3ecd6"
_BURGUNDY   = "#6f1d1b"
_NAVY       = "#1c2956"
_OLIVE      = "#5a5e2a"
_AMBER      = "#a6562b"

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


def _ordinal(n: int) -> str:
    s = "th" if 11 <= (n % 100) <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}<sup style='font-size:9px;'>{s}</sup>"


def _long_date(d: date) -> str:
    """'Thursday, the 14th of May, two thousand and twenty-six'"""
    ones = ["", "one", "two", "three", "four", "five", "six", "seven",
            "eight", "nine", "ten", "eleven", "twelve", "thirteen",
            "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty",
            "sixty", "seventy", "eighty", "ninety"]
    year = d.year
    if 2000 <= year < 2100:
        rem = year - 2000
        if rem == 0:
            yr_words = "two thousand"
        elif rem < 20:
            yr_words = f"two thousand and {ones[rem]}"
        else:
            yr_words = f"two thousand and {tens[rem // 10]}" + (
                f"-{ones[rem % 10]}" if rem % 10 else ""
            )
    else:
        yr_words = str(year)

    months = ["", "January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    return (
        f"<span style='font-style:italic;'>{d.strftime('%A')}</span>, "
        f"the {_ordinal(d.day)} of {months[d.month]}, {yr_words}"
    )


def _fmt_cr(v: float) -> str:
    if v <= 0:
        return "—"
    if v >= 1000:
        return f"₹&nbsp;{v:,.0f}"
    if v >= 100:
        return f"₹&nbsp;{v:.0f}"
    return f"₹&nbsp;{v:.1f}"


def _fmt_qty(n: int | float) -> str:
    if not n or n == 0:
        return '<span style="color:#837763;">—</span>'
    return f"{int(n):,}"


def _vcr(qty, price) -> float:
    try:
        return round(float(qty) * float(price) / 1e7, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


# ─── Client classification ────────────────────────────────────────────────────

_CLIENT_RULES: list[tuple[str, list[str]]] = [
    ("FII",    ["FPI ", "FOREIGN PORTFOLIO", "EMERGING MARKET", "GQG",
                "GOLDMAN SACHS", "JP MORGAN", "MORGAN STANLEY", "NOMURA",
                "CITIBANK", "CITIGROUP", "DEUTSCHE BANK", "BARCLAYS",
                "BLACKROCK", "VANGUARD", "FIDELITY", "TEMPLETON",
                "ABERDEEN", "SCHRODERS", "SOCIETE GENERALE", "BNP PARIBAS",
                "UBS PRINCIPAL", "HSBC BANK", "LAZARD", "MERRILL LYNCH",
                "CRAFT EM", "CRAFT EMERGING", "SINGAPORE PTE",
                "MAURITIUS COMPANY", "CAYMAN", "OFFSHORE FUND",
                "ODI HOLDER", "PARTICIPATORY NOTE"]),
    ("DII/MF", ["MUTUAL FUND", "NIPPON INDIA MF", "HDFC MUTUAL", "SBI MUTUAL",
                "AXIS MUTUAL", "KOTAK MUTUAL", "ADITYA BIRLA MF",
                "UTI MUTUAL", "DSP MUTUAL", "MIRAE ASSET MF",
                "INSURANCE CO", "LIC OF INDIA", "BAJAJ ALLIANZ",
                "NATIONAL PENSION", "MOTILAL MF", "TATA MUTUAL",
                "INVESCO MF", "CANARA ROBECO"]),
    ("AIF",    ["AIF ", " AIF", "ALTERNATIVE INVESTMENT", "REF IFSC",
                "REAL ESTATE FUND", "CATEGORY I AIF", "CATEGORY II AIF",
                "CATEGORY III AIF", "COMMERCIAL REF", "VENTURE FUND IFSC"]),
    ("HFT",    ["GRAVITON RESEARCH", "JUMP TRADING", "MICROCURVES",
                "OPTIVER", "VIRTU", "CITADEL", "TWO SIGMA", "JANE STREET",
                "IMC FINANCIAL", "SUSQUEHANNA", "TOWER RESEARCH",
                "XTX MARKETS", "HUDSON RIVER", "HRTI", "MILLENIUM MGMT",
                "POINT72", "SQUAREPOINT", "QUBE RESEARCH", "ALPHAWAVE",
                "DE SHAW", "RENAISSANCE TECH"]),
    ("PROP",   ["SECURITIES RESEARCH", "FINSOL", "JUNOMONETA", "YUGA STOCKS",
                "BHANA EQUITY", "NK SECURITIES", "PROP TRADING",
                "TRADING STRATEGIES LLP", "TRADE TECH"]),
    ("BRKR",   ["BROKING LTD", "BROKING PVT", " BROKER ", "ANGEL ONE",
                "ZERODHA", "EDELWEISS SECURITIES", "SHAREKHAN",
                "ANTIQUE STOCK", "PRABHUDAS LILLADHER", "NIRMAL BANG",
                "MOTILAL OSWAL SEC", "AXIS SECURITIES", "ICICI SEC",
                "HDFC SEC", "KOTAK SEC"]),
    ("STRAT",  ["ESTATE OF LATE", "ESTATE OF MR", "JHUNJHUNWALA",
                "PROMOTER GROUP", "STRATEGIC INVESTOR", "PROMOTER A/C"]),
    ("TRUST",  ["FAMILY OFFICE", "FAMILY TRUST", "FOUNDATION",
                "ENDOWMENT", "CHARITABLE TRUST"]),
    ("CORP",   ["PRIVATE LIMITED", "PVT. LTD", "PVT LTD", "HITECH PRIVATE",
                "INDUSTRIES LTD", "ENTERPRISES LTD", "CORPORATION LTD",
                "TECHNOLOGIES LTD", "INFRA LIMITED", "PROJECTS LTD",
                "HOLDINGS LTD", "VENTURES LTD"]),
]

_TAG_COLOR = {
    "FII": _NAVY, "DII/MF": _OLIVE, "AIF": _OLIVE,
    "HFT": _BURGUNDY, "PROP": _BURGUNDY, "BRKR": _STONE,
    "CORP": _STONE, "STRAT": _BURGUNDY, "HNI": _STONE,
    "TRUST": _STONE, "OTHER": _STONE,
}


def _classify(name: str) -> str:
    if not name:
        return "OTHER"
    up = name.upper()
    for tag, keywords in _CLIENT_RULES:
        if any(k in up for k in keywords):
            return tag
    corp_markers = ["LTD", "LIMITED", "LLP", "FUND", "BANK", "INSURANCE",
                    "CAPITAL", "SECURITIES", "FINANCIAL", "BROKING",
                    "INVESTMENT", "ASSET", "MGMT", "VENTURES", "FINSOL"]
    return "CORP" if any(m in up for m in corp_markers) else "HNI"


def _tag_html(tag: str) -> str:
    c = _TAG_COLOR.get(tag, _STONE)
    return (
        f'<span style="border:1px solid {c}; color:{c}; '
        f'padding:1px 6px 2px 6px; font-size:9px; letter-spacing:0.14em; '
        f'font-weight:600; font-family:\'Times New Roman\',Times,serif;">'
        f'{_e(tag)}</span>'
    )


def _side_html(side: str) -> str:
    if side == "b":
        return f'<span style="color:{_OLIVE}; font-weight:600; font-family:\'Times New Roman\',Times,serif;">b</span>'
    if side == "s":
        return f'<span style="color:{_BURGUNDY}; font-weight:600; font-family:\'Times New Roman\',Times,serif;">s</span>'
    return f'<span style="color:{_STONE}; font-style:italic; font-family:\'Times New Roman\',Times,serif;">b·s</span>'


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


def _short_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    df = df[df["quantity"] > SHORT_MIN_QTY].copy()
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
    by_cs["_ctot"] = by_cs.groupby("client_name")["vcr"].transform("sum")
    by_cs = by_cs.sort_values(["_ctot", "vcr"], ascending=[False, False]).drop(columns="_ctot")
    return by_cs.reset_index(drop=True)


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
                "tag_color": _AMBER,
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
                        "tag_color": _BURGUNDY,
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


# ─── HTML bar chart (email-safe — SVG not rendered by most email clients) ─────

def _svg_chart(top: pd.DataFrame) -> str:
    """Horizontal bar chart rendered as a plain HTML table for email compatibility."""
    if top.empty:
        return ""
    max_val = float(top["value_cr"].max())
    if max_val <= 0:
        return ""

    BAR_AREA_PX = 360

    rows: list[str] = []
    for i, row in enumerate(top.itertuples()):
        bar_w = max(4, int(float(row.value_cr) / max_val * BAR_AREA_PX))
        fill = _BURGUNDY if i == 0 else _INK
        val_str = _fmt_cr(float(row.value_cr))
        rows.append(
            f'<tr>'
            f'<td width="115" align="right" valign="middle" '
            f'style="padding:4px 10px 4px 0; font-family:\'Times New Roman\',Times,serif; '
            f'font-size:12px; font-weight:700; color:{_INK}; white-space:nowrap;">'
            f'{_e(str(row.symbol))}</td>'
            f'<td valign="middle" style="padding:4px 0;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td width="{bar_w}" height="18" bgcolor="{fill}" '
            f'style="background:{fill}; height:18px; width:{bar_w}px; '
            f'font-size:1px; line-height:1px;">&nbsp;</td>'
            f'</tr></table>'
            f'</td>'
            f'<td width="90" valign="middle" '
            f'style="padding:4px 0 4px 10px; font-family:\'Times New Roman\',Times,serif; '
            f'font-size:12px; color:{_INK}; white-space:nowrap;">'
            f'{val_str}&thinsp;cr</td>'
            f'</tr>'
        )

    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse; width:100%; max-width:565px;">'
        + ''.join(rows)
        + '</table>'
    )


# ─── Section renderers ────────────────────────────────────────────────────────

def _section_hdr(num: str, title: str, subtitle: str = "", desc: str = "") -> str:
    sub_html = f'<td align="right" valign="baseline" style="font-family:\'Times New Roman\',Times,serif; font-size:12.5px; color:{_STONE}; font-style:italic;">{subtitle}</td>' if subtitle else ""
    desc_html = f'<p style="font-family:\'Times New Roman\',Times,serif; font-size:13.5px; color:{_INK_SOFT}; margin:10px 0 0 0; line-height:1.55; max-width:540px;">{desc}</p>' if desc else ""
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
  <tr><td style="padding:36px 36px 0 36px;">
    <div style="border-top:1px solid {_TAN}; padding-top:24px;"></div>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
      <td valign="baseline">
        <span style="font-family:'Times New Roman',Times,serif; font-size:46px; font-weight:400; color:{_BURGUNDY}; line-height:0.9; letter-spacing:-0.02em; font-style:italic;">{num}.</span>
        <span style="font-family:'Times New Roman',Times,serif; font-size:30px; font-weight:500; color:{_INK}; letter-spacing:-0.012em; margin-left:14px;">{title}</span>
      </td>
      {sub_html}
    </tr></table>
    {desc_html}
  </td></tr>
</table>"""


def _th(label: str, align: str = "left") -> str:
    return (
        f'<th align="{align}" style="padding:8px 10px 8px 12px; font-weight:600; font-size:9.5px; '
        f'letter-spacing:0.2em; text-transform:uppercase; color:{_STONE}; border-bottom:1.5px solid {_INK};">'
        f'{label}</th>'
    )


def _sym_table_html(sym: pd.DataFrame, n_quartile: int = 0) -> str:
    """Render a symbol-wise table (bulk or block style)."""
    if sym.empty:
        return f'<p style="color:{_STONE}; font-style:italic; font-family:\'Times New Roman\',Times,serif; font-size:13px; padding:0 36px;">No deals.</p>'
    rows: list[str] = []
    quartile_cut = sym.nlargest(max(1, n_quartile), "total_vcr")["symbol"].tolist() if n_quartile else []
    for i, row in enumerate(sym.itertuples()):
        sym_name = str(row.symbol)
        sec_name = str(row.security_name or sym_name)
        bq = int(getattr(row, "buy_qty", 0))
        sq = int(getattr(row, "sell_qty", 0))
        vcr = float(row.total_vcr)
        flag = str(row.flag)
        flag_color = _OLIVE if flag == "=" else _AMBER
        has_rule = sym_name in quartile_cut
        rule_style = f"border-left:3px solid {_BURGUNDY}; " if has_rule else ""
        rows.append(
            f'<tr>'
            f'<td style="padding:7px 10px 7px 12px; border-bottom:1px solid {_SAND}; {rule_style}'
            f'font-weight:600; color:{_INK}; font-family:\'Times New Roman\',Times,serif; font-size:13px;">{_e(sym_name)}</td>'
            f'<td style="padding:7px 10px; border-bottom:1px solid {_SAND}; '
            f'font-family:\'Times New Roman\',Times,serif; font-style:italic; color:{_INK_SOFT};">{_e(sec_name)}</td>'
            f'<td align="right" style="padding:7px 6px; border-bottom:1px solid {_SAND};">{_fmt_qty(bq)}</td>'
            f'<td align="right" style="padding:7px 6px; border-bottom:1px solid {_SAND};">{_fmt_qty(sq)}</td>'
            f'<td align="right" style="padding:7px 10px; border-bottom:1px solid {_SAND}; font-weight:600;">{_fmt_cr(vcr)}</td>'
            f'<td align="center" style="padding:7px 12px 7px 6px; border-bottom:1px solid {_SAND}; '
            f'color:{flag_color};">{flag}</td>'
            f'</tr>'
        )
    thead = (
        f'<tr>'
        + _th("Symbol") + _th("Security") + _th("Buy", "right")
        + _th("Sell", "right") + _th("₹ Cr", "right") + _th("Flag", "center")
        + '</tr>'
    )
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="border-collapse:collapse; font-family:\'Times New Roman\',Times,serif; '
        f'font-size:12.5px; font-variant-numeric:tabular-nums;">'
        f'<thead>{thead}</thead><tbody>{"".join(rows)}</tbody></table>'
        f'<div style="font-family:\'Times New Roman\',Times,serif; font-size:11.5px; color:{_STONE}; padding:10px 2px 0 2px; font-style:italic;">'
        f'<span style="color:{_OLIVE}; font-weight:500;">=</span>&nbsp;crossed (buy equals sell)'
        f'&nbsp;·&nbsp;<span style="color:{_AMBER}; font-weight:500;">▲</span>&nbsp;asymmetric'
        f'&nbsp;·&nbsp;ruled left edge marks the top quartile by value</div>'
    )


def _short_table_html(short: pd.DataFrame) -> str:
    if short.empty:
        return f'<p style="color:{_STONE}; font-style:italic; font-family:\'Times New Roman\',Times,serif; font-size:13px; padding:0 36px;">No qualifying positions.</p>'
    rows: list[str] = []
    for _, row in short.iterrows():
        sym = str(row.get("symbol", ""))
        sec = str(row.get("security_name", sym) or sym)
        qty = int(row.get("quantity", 0))
        rows.append(
            f'<tr>'
            f'<td style="padding:7px 10px 7px 12px; border-bottom:1px solid {_SAND}; '
            f'font-weight:600; color:{_INK}; font-family:\'Times New Roman\',Times,serif; font-size:13px;">{_e(sym)}</td>'
            f'<td style="padding:7px 10px; border-bottom:1px solid {_SAND}; '
            f'font-family:\'Times New Roman\',Times,serif; font-style:italic; color:{_INK_SOFT};">{_e(sec)}</td>'
            f'<td align="right" style="padding:7px 10px; border-bottom:1px solid {_SAND};">{qty:,}</td>'
            f'</tr>'
        )
    thead = f'<tr>{_th("Symbol")}{_th("Security")}{_th("Quantity", "right")}</tr>'
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="border-collapse:collapse; font-family:\'Times New Roman\',Times,serif; font-size:12.5px; font-variant-numeric:tabular-nums;">'
        f'<thead>{thead}</thead><tbody>{"".join(rows)}</tbody></table>'
    )


def _client_table_html(client: pd.DataFrame) -> str:
    if client.empty:
        return f'<p style="color:{_STONE}; font-style:italic; font-family:\'Times New Roman\',Times,serif; font-size:13px; padding:0 36px;">No client data.</p>'

    rows: list[str] = []
    clients_seen: dict[str, int] = {}
    # Pre-compute rowspan per client
    rowspan: dict[str, int] = client.groupby("client_name").size().to_dict()

    prev_client = None
    for _, row in client.iterrows():
        cn = str(row["client_name"])
        sym = str(row["symbol"])
        sec = str(row.get("security_name", sym) or sym)
        side = str(row["side"])
        qty = int(row["qty"])
        vcr = float(row["vcr"])
        cls = str(row["class"])
        n_syms = rowspan.get(cn, 1)

        is_group_end = (
            cn != prev_client and
            n_syms > 1
        )

        # Border between client groups
        border_color = _TAN if cn != prev_client and prev_client is not None else _SAND

        client_cell = ""
        if cn != prev_client:
            clients_seen[cn] = 0
            rs = rowspan[cn]
            rs_attr = f' rowspan="{rs}"' if rs > 1 else ""
            subtitle = ""
            if rs > 1:
                # Count unique symbols
                subtitle = (
                    f'<div style="font-size:10.5px; color:{_STONE}; font-style:italic; margin-top:2px;">'
                    f'{rs} position{"s" if rs > 1 else ""}</div>'
                )
            bg = f"background:{_WARM_GREY}; " if rs > 1 else ""
            client_cell = (
                f'<td{rs_attr} valign="top" style="padding:8px 10px 6px 12px; '
                f'border-bottom:1px solid {border_color}; {bg}'
                f'font-family:\'Times New Roman\',Times,serif;">'
                f'<div style="color:{_INK}; font-weight:500;">{_e(cn)}</div>'
                f'{subtitle}</td>'
            )
        clients_seen[cn] = clients_seen.get(cn, 0) + 1
        b_col = _TAN if clients_seen.get(cn, 1) == n_syms else _SAND

        rows.append(
            f'<tr>'
            f'{client_cell}'
            f'<td align="center" style="padding:6px 4px; border-bottom:1px solid {b_col};">{_tag_html(cls)}</td>'
            f'<td style="padding:6px 10px; border-bottom:1px solid {b_col}; font-weight:600;">{_e(sym)}</td>'
            f'<td align="right" style="padding:6px 4px; border-bottom:1px solid {b_col};">{_side_html(side)}</td>'
            f'<td align="right" style="padding:6px 6px; border-bottom:1px solid {b_col}; font-variant-numeric:tabular-nums;">{qty:,}</td>'
            f'<td align="right" style="padding:6px 12px 6px 10px; border-bottom:1px solid {b_col}; font-weight:600;">{_fmt_cr(vcr)}</td>'
            f'</tr>'
        )
        prev_client = cn

    thead = (
        f'<tr>'
        + _th("Client", "left")
        + f'<th align="center" style="padding:8px 4px; font-weight:600; font-size:9.5px; letter-spacing:0.2em; text-transform:uppercase; color:{_STONE}; border-bottom:1.5px solid {_INK};">Class</th>'
        + _th("Symbol", "left") + _th("Side", "right")
        + _th("Qty", "right") + _th("₹ Cr", "right")
        + '</tr>'
    )
    legend = (
        f'<div style="font-family:\'Times New Roman\',Times,serif; font-size:12px; color:{_INK_SOFT}; padding:14px 2px 0 2px; line-height:1.7;">'
        f'<span style="font-style:italic; color:{_STONE};">Class tags &mdash;</span>'
        f'&nbsp;<b style="font-weight:500;">FII</b> foreign portfolio investor,'
        f'&nbsp;<b style="font-weight:500;">DII/MF</b> domestic mutual fund or insurer,'
        f'&nbsp;<b style="font-weight:500;">AIF</b> alternative investment fund,'
        f'&nbsp;<b style="font-weight:500;">HFT</b> high-frequency / quant prop,'
        f'&nbsp;<b style="font-weight:500;">PROP</b> proprietary trading,'
        f'&nbsp;<b style="font-weight:500;">CORP</b> corporate,'
        f'&nbsp;<b style="font-weight:500;">STRAT</b> strategic or promoter,'
        f'&nbsp;<b style="font-weight:500;">HNI</b> individual,'
        f'&nbsp;<b style="font-weight:500;">TRUST</b> family office or trust.'
        f'</div>'
    )
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="border-collapse:collapse; font-family:\'Times New Roman\',Times,serif; font-size:12px; font-variant-numeric:tabular-nums;">'
        f'<thead>{thead}</thead><tbody>{"".join(rows)}</tbody></table>'
        + legend
    )


# ─── Full HTML assembly ───────────────────────────────────────────────────────

def _highlights_html(cards: list[dict]) -> str:
    if not cards:
        return ""

    def _card(c: dict, style: str = "") -> str:
        tag_color = c.get("tag_color", _INK)
        tag_right = c.get("tag_right", "")
        tag_right_html = (
            f'<td align="right" valign="top" style="font-family:\'Times New Roman\',Times,serif; '
            f'font-size:10px; letter-spacing:0.22em; text-transform:uppercase; '
            f'color:{_STONE}; font-weight:500;">{tag_right}</td>'
        ) if tag_right else ""
        return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="{style}">
<tr><td style="padding:20px 24px 22px 24px;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
    <td valign="top" style="font-family:'Times New Roman',Times,serif; font-size:10px; letter-spacing:0.22em; text-transform:uppercase; color:{tag_color}; font-weight:600;">{c['tag']}</td>
    {tag_right_html}
  </tr></table>
  <div style="font-family:'Times New Roman',Times,serif; font-size:{'34px' if c.get('lead') else '22px'}; font-weight:500; color:{_INK}; margin-top:10px; line-height:1.05; letter-spacing:-0.012em;">
    {c['title']}
  </div>
  <p style="font-family:'Times New Roman',Times,serif; font-size:{'15px' if c.get('lead') else '13.5px'}; color:{_INK}; line-height:1.55; margin:14px 0 0 0;">
    {c['body']}
  </p>
</td></tr>
</table>"""

    lead_card_style = (
        f"background:{_CREAM}; border:1px solid {_TAN}; border-left:4px solid {_BURGUNDY};"
    )
    small_card_style = f"background:{_CREAM}; border:1px solid {_TAN}; height:100%;"

    lead_html = _card(cards[0], lead_card_style) if cards else ""
    small_cards = cards[1:]

    # Arrange remaining cards in rows of 2
    grid_rows_html = ""
    for i in range(0, len(small_cards), 2):
        pair = small_cards[i:i + 2]
        left_html = f'<td width="50%" valign="top" style="padding:0 7px 14px 0;">{_card(pair[0], small_card_style)}</td>'
        right_html = (
            f'<td width="50%" valign="top" style="padding:0 0 14px 7px;">{_card(pair[1], small_card_style)}</td>'
            if len(pair) > 1 else '<td width="50%"></td>'
        )
        grid_rows_html += f'<tr>{left_html}{right_html}</tr>'

    grid_html = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-top:14px;">'
        f'{grid_rows_html}'
        f'</table>'
    ) if grid_rows_html else ""

    return f"""
<a name="highlights"></a>
{_section_hdr("i", "Five things worth knowing", "a quick scan of the day")}
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
  <tr><td style="padding:16px 36px 8px 36px;">
    {lead_html}
    {grid_html}
  </td></tr>
</table>"""


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
) -> str:
    bm = metrics["bulk"]
    bkm = metrics["block"]
    sm = metrics["short"]

    n_bulk_names = bm["names"]
    n_block_names = bkm["names"]
    n_bulk_sym = len(bulk_sym) if not bulk_sym.empty else 0
    n_block_sym = len(block_sym) if not block_sym.empty else 0

    # Approximate issue number: trading days from 2026-01-01
    try:
        delta = (report_date - date(2026, 1, 1)).days
        trading_days_approx = max(1, int(delta * 5 / 7))
        issue_num = trading_days_approx
    except Exception:
        issue_num = 1

    import pytz
    _ist = pytz.timezone("Asia/Kolkata")
    _gen_ist = generated_at.astimezone(_ist)
    gen_time_str = _gen_ist.strftime("%H:%M IST on the ")
    gen_day_str = _ordinal(_gen_ist.day)
    gen_month = _gen_ist.strftime("%B, %Y")

    svg = _svg_chart(top)
    chart_html = svg or (
        f'<p style="color:{_STONE}; font-style:italic; '
        f"font-family:'Times New Roman',Times,serif;"
        f'">No value data available.</p>'
    )
    hlights_html = _highlights_html(highlights)

    # Nav strip items
    nav_items = [
        ('<a href="#highlights" style="color:{c}; text-decoration:none;">Highlights</a>', True),
        ('<a href="#symbols" style="color:{c}; text-decoration:none;">Bulk Symbols</a>', not bulk_sym.empty),
        ('<a href="#blocks" style="color:{c}; text-decoration:none;">Block Symbols</a>', not block_sym.empty),
        ('<a href="#shorts" style="color:{c}; text-decoration:none;">Shorts</a>', not short_filt.empty),
        ('<a href="#clients" style="color:{c}; text-decoration:none;">Clients</a>', not bulk_client.empty),
    ]
    nav_html = (
        f' <span style="color:{_TAN}; margin:0 9px;">/</span> '
    ).join(
        item.format(c=_INK)
        for item, show in nav_items if show
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BAC Daily Deals — NSE — {report_date.day} {report_date.strftime('%b %Y')}</title>
<style>
.num-tab {{ font-variant-numeric: tabular-nums; }}
  a {{ color: inherit; }}
</style>
</head>
<body style="margin:0; padding:28px 12px; background:{_SAND}; font-family:'Times New Roman',Times,serif; color:{_INK}; -webkit-font-smoothing:antialiased;">

<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" width="760" style="width:760px; max-width:760px; margin:0 auto; background:{_PARCHMENT}; border:1px solid {_TAN};">
<tr><td style="padding:0;">

  <!-- MASTHEAD -->
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:{_PARCHMENT};">
    <tr><td style="padding:30px 36px 6px 36px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
        <td valign="middle" style="padding:0;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
            <td style="font-family:'Times New Roman',Times,serif; font-size:10.5px; letter-spacing:0.32em; text-transform:uppercase; color:{_BURGUNDY}; font-weight:500;">Brindco Alpha Capital</td>
            <td style="padding:0 10px; color:{_TAN}; font-size:12px;">◆</td>
            <td style="font-family:'Times New Roman',Times,serif; font-size:11px; font-style:italic; color:{_STONE}; font-weight:400;">a daily note from the quant desk</td>
          </tr></table>
        </td>
        <td align="right" valign="middle" style="font-family:'Times New Roman',Times,serif; font-size:11px; color:{_STONE}; font-style:italic;">
          № {issue_num}
        </td>
      </tr></table>

      <div style="font-family:'Times New Roman',Times,serif; font-size:54px; font-weight:500; color:{_INK}; letter-spacing:-0.018em; margin:14px 0 0 0; line-height:1;">Daily&nbsp;Deals</div>
      <div style="font-family:'Times New Roman',Times,serif; font-size:20px; font-weight:400; color:{_INK}; font-style:italic; margin:2px 0 18px 2px; line-height:1;">National Stock Exchange of India</div>

      <div style="border-top:3px solid {_INK}; padding-top:1px;"></div>
      <div style="border-top:1px solid {_INK}; margin-top:2px;"></div>

      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-top:12px;">
        <tr>
          <td valign="top" style="font-family:'Times New Roman',Times,serif; font-size:13.5px; color:{_INK};">
            {_long_date(report_date)}
          </td>
          <td align="right" valign="top" style="font-family:'Times New Roman',Times,serif; font-size:10.5px; letter-spacing:0.22em; text-transform:uppercase; color:{_STONE};">
            Mumbai · IST
          </td>
        </tr>
      </table>
    </td></tr>
  </table>

  <!-- TOPLINE METRICS -->
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
    <tr><td style="padding:18px 36px 6px 36px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:{_WARM_GREY}; border-top:1px solid {_TAN}; border-bottom:1px solid {_TAN};">
        <tr>
          <td width="33%" valign="top" style="padding:14px 14px 14px 18px; border-right:1px solid {_TAN};">
            <div style="font-family:'Times New Roman',Times,serif; font-size:9.5px; letter-spacing:0.24em; text-transform:uppercase; color:{_STONE}; font-weight:500;">Bulk Deals</div>
            <div style="font-family:'Times New Roman',Times,serif; font-size:28px; font-weight:500; color:{_INK}; font-variant-numeric:tabular-nums; margin-top:6px; line-height:1; letter-spacing:-0.01em;">{_fmt_cr(bm['value_cr'])}<span style="font-size:14px; color:{_STONE}; font-style:italic; font-weight:400; margin-left:3px;">cr</span></div>
            <div style="font-family:'Times New Roman',Times,serif; font-size:12px; color:{_STONE}; margin-top:6px; font-style:italic;">{bm['deals']} deals, {bm['names']} names</div>
          </td>
          <td width="33%" valign="top" style="padding:14px 14px 14px 16px; border-right:1px solid {_TAN};">
            <div style="font-family:'Times New Roman',Times,serif; font-size:9.5px; letter-spacing:0.24em; text-transform:uppercase; color:{_STONE}; font-weight:500;">Block Deals</div>
            <div style="font-family:'Times New Roman',Times,serif; font-size:28px; font-weight:500; color:{_INK}; font-variant-numeric:tabular-nums; margin-top:6px; line-height:1; letter-spacing:-0.01em;">{_fmt_cr(bkm['value_cr'])}<span style="font-size:14px; color:{_STONE}; font-style:italic; font-weight:400; margin-left:3px;">cr</span></div>
            <div style="font-family:'Times New Roman',Times,serif; font-size:12px; color:{_STONE}; margin-top:6px; font-style:italic;">{bkm['deals']} deals, {bkm['names']} names</div>
          </td>
          <td width="34%" valign="top" style="padding:14px 18px 14px 16px;">
            <div style="font-family:'Times New Roman',Times,serif; font-size:9.5px; letter-spacing:0.24em; text-transform:uppercase; color:{_STONE}; font-weight:500;">Short Selling <span style="font-style:italic; letter-spacing:0; text-transform:none; font-family:'Times New Roman',Times,serif; font-weight:400;">(qty&nbsp;≥&nbsp;5k)</span></div>
            <div style="font-family:'Times New Roman',Times,serif; font-size:28px; font-weight:500; color:{_INK}; font-variant-numeric:tabular-nums; margin-top:6px; line-height:1; letter-spacing:-0.01em;">{sm['deals']}<span style="font-size:14px; color:{_STONE}; font-style:italic; font-weight:400; margin-left:3px;">deals</span></div>
            <div style="font-family:'Times New Roman',Times,serif; font-size:12px; color:{_STONE}; margin-top:6px; font-style:italic;">{sm['above_threshold']} over threshold</div>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>

  <!-- NAV STRIP -->
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
    <tr><td style="padding:4px 36px 0 36px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
        <td style="padding:14px 0; font-family:'Times New Roman',Times,serif; font-size:10.5px; letter-spacing:0.20em; text-transform:uppercase; color:{_INK};">
          {nav_html}
        </td>
        <td align="right" style="font-family:'Times New Roman',Times,serif; font-size:12px; font-style:italic; color:{_STONE};">data from NSE archives</td>
      </tr></table>
    </td></tr>
  </table>

  {hlights_html}

  <!-- SECTION ii — TOP NAMES BY VALUE -->
  <a name="summary"></a>
  {_section_hdr("ii", "Top names by value", "where the rupees actually went",
    "Bulk and block combined, the larger of buy/sell side shown per name.")}
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
    <tr><td style="padding:22px 36px 4px 36px;">
      {chart_html}
    </td></tr>
  </table>

  <!-- SECTION iii — BULK BY SYMBOL -->
  <a name="symbols"></a>
  {_section_hdr("iii", "Bulk Deals, by symbol",
    f"{n_bulk_names} name{'s' if n_bulk_names != 1 else ''} · sorted by value",
    "What moved, by name. Triangle marks asymmetric quantities; equals marks a clean crossed deal.")}
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
    <tr><td style="padding:18px 36px 6px 36px;">
      {_sym_table_html(bulk_sym, n_quartile=max(1, len(bulk_sym) // 4))}
    </td></tr>
  </table>

  <!-- SECTION iv — BLOCK BY SYMBOL -->
  <a name="blocks"></a>
  {_section_hdr("iv", "Block Deals, by symbol",
    f"{n_block_names} name{'s' if n_block_names != 1 else ''} · block window",
    "Negotiated trades in the pre-open block window (08:45–09:00 IST).")}
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
    <tr><td style="padding:18px 36px 6px 36px;">
      {_sym_table_html(block_sym, n_quartile=max(1, len(block_sym) // 4))}
    </td></tr>
  </table>

  <!-- SECTION v — SHORTS -->
  <a name="shorts"></a>
  {_section_hdr("v", "Short Selling, top by quantity",
    f"positions ≥ {SHORT_MIN_QTY:,} shares",
    "Intraday short positions of at least five thousand shares. Source feed carries no client information.")}
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
    <tr><td style="padding:18px 36px 6px 36px;">
      {_short_table_html(short_filt)}
    </td></tr>
  </table>

  <!-- SECTION vi — BULK BY CLIENT -->
  <a name="clients"></a>
  {_section_hdr("vi", "Bulk Deals, by client", "who was on the other side",
    "One row per client–symbol pair, ordered by client total value descending.")}
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
    <tr><td style="padding:18px 36px 6px 36px;">
      {_client_table_html(bulk_client)}
    </td></tr>
  </table>

  <!-- SECTION vii — BLOCK BY CLIENT -->
  {_section_hdr("vii", "Block Deals, by client",
    f"{bkm['deals']} deal{'s' if bkm['deals'] != 1 else ''} · pre-open window")}
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
    <tr><td style="padding:18px 36px 6px 36px;">
      {_client_table_html(block_client)}
    </td></tr>
  </table>

  <!-- COLOPHON -->
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-top:32px;">
    <tr><td style="padding:0 36px 32px 36px;">
      <div style="border-top:3px solid {_INK}; padding-top:1px;"></div>
      <div style="border-top:1px solid {_INK}; margin-top:2px;"></div>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-top:18px;">
        <tr>
          <td valign="top" width="55%" style="padding-right:24px;">
            <div style="font-family:'Times New Roman',Times,serif; font-size:9.5px; letter-spacing:0.24em; text-transform:uppercase; color:{_BURGUNDY}; font-weight:600; margin-bottom:6px;">Colophon</div>
            <p style="font-family:'Times New Roman',Times,serif; font-size:12.5px; color:{_INK}; line-height:1.65; margin:0;">
              Set in <span style="font-style:italic;">Times New Roman</span>. Compiled by the NSE Deals pipeline at {gen_time_str}{gen_day_str} of {gen_month}.
            </p>
          </td>
          <td valign="top" width="45%">
            <div style="font-family:'Times New Roman',Times,serif; font-size:9.5px; letter-spacing:0.24em; text-transform:uppercase; color:{_BURGUNDY}; font-weight:600; margin-bottom:6px;">Sources &amp; correspondence</div>
            <div style="font-family:'Times New Roman',Times,serif; font-size:12.5px; color:{_INK}; line-height:1.7;">
              Data from NSE archives &mdash; bulk, block, and short feeds. Notional values from the WATP column.<br>
              Write to <a href="mailto:bac@brindco.com" style="color:{_BURGUNDY}; text-decoration:none; border-bottom:1px dotted {_TAN};">bac@brindco.com</a> with corrections or class additions.
            </div>
          </td>
        </tr>
      </table>
      <div style="margin-top:18px; text-align:center; font-family:'Times New Roman',Times,serif; font-size:11px; font-style:italic; color:{_STONE};">
        ◆&nbsp;&nbsp;&nbsp;◆&nbsp;&nbsp;&nbsp;◆
      </div>
    </td></tr>
  </table>

</td></tr>
</table>
</body>
</html>"""


# ─── Email ────────────────────────────────────────────────────────────────────

def _send_email(
    *, sender: str, password: str, sender_name: str,
    recipients: list[str], subject: str, html: str,
    attachments: dict[str, bytes],
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender}>"
    msg["To"] = ", ".join(recipients)
    msg.set_content("This report requires an HTML-capable email client.")
    msg.add_alternative(html, subtype="html")
    for filename, content in attachments.items():
        if content:
            msg.add_attachment(content, maintype="text", subtype="csv", filename=filename)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def _csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8") if not df.empty else b""


# ─── Short-deals readiness gate ──────────────────────────────────────────────

def _short_deals_ready(report_date: date, min_records: int = 10) -> bool:
    """Return True if short_deals for report_date has been populated by NSE."""
    from database.client import get_client
    try:
        resp = (
            get_client().table("short_deals")
            .select("id", count="exact")
            .eq("deal_date", report_date.isoformat())
            .execute()
        )
        return (resp.count or 0) >= min_records
    except Exception:
        return True  # DB error → don't block; let report proceed


# ─── Entry point ──────────────────────────────────────────────────────────────

def main(report_date_override: date | None = None, preview_path: str | None = None) -> int:
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
    if not preview_mode:
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

        bulk  = _enrich_bulk(bulk_raw)
        block = _enrich_block(block_raw)
        short = _enrich_short(short_raw)

        metrics      = _topline(bulk, block, short)
        bulk_sym     = _sym_rows(bulk)
        block_sym    = _sym_rows(block)
        short_filt   = _short_rows(short)
        bulk_client  = _client_rows(bulk)
        block_client = _client_rows(block)
        top          = _top_names(bulk, block, n=10)
        hlights      = _highlights(bulk, block)

        html = _build_html(
            report_date, bulk, block, short,
            bulk_sym, block_sym, short_filt,
            bulk_client, block_client,
            top, metrics, hlights, generated_at,
        )

        if preview_mode:
            with open(preview_path, "w", encoding="utf-8") as fh:
                fh.write(html)
            logger.info("Preview saved to %s", preview_path)
            return 0

        attachments = {
            f"bulk_deals_{report_date}.csv":  _csv_bytes(bulk_raw),
            f"block_deals_{report_date}.csv": _csv_bytes(block_raw),
            f"short_deals_{report_date}.csv": _csv_bytes(short_raw),
        }

        pretty_date = report_date.strftime("%d %b %Y")
        _send_email(
            sender=smtp_user, password=smtp_password, sender_name=sender_name,
            recipients=recipients,
            subject=f"BAC Daily Deals — NSE — {pretty_date}",
            html=html, attachments=attachments,
        )
        _mark_sent(report_date)
        logger.info("Sent report for %s to %s", report_date, recipients)
        return 0

    except Exception as exc:  # noqa: BLE001
        logger.exception("Report generation failed")
        if not preview_mode:
            _mark_failed(report_date, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate BAC Daily Deals NSE report")
    parser.add_argument("--date", help="Override report date (YYYY-MM-DD)")
    parser.add_argument("--preview", metavar="PATH",
                        help="Save HTML to file instead of emailing (no DB slot needed)")
    args = parser.parse_args()
    override = date.fromisoformat(args.date) if args.date else None
    sys.exit(main(override, preview_path=args.preview))
