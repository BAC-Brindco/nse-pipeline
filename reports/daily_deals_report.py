"""
Daily NSE deals email report.

Runs once per trading-day morning. Pulls T-1 bulk / block / short deals
from Supabase, builds an HTML report (summary → by-symbol → by-client)
with the raw rows attached as CSVs, and sends it via Gmail SMTP.

Idempotency: report_log has UNIQUE (report_type, report_date). The first
invocation that successfully INSERTs a row wins; concurrent or duplicate
runs short-circuit. Failed sends update status='failed' so a later run
can retry by deleting the failed row out-of-band (or we just wait for
the next trading day).
"""

from __future__ import annotations

import logging
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

import pandas as pd

from database.client import get_client
from utils.helpers import is_trading_day, today_ist

logger = logging.getLogger("nse.report")


REPORT_TYPE = "daily_deals_email"


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _previous_trading_day(today: date) -> date:
    d = today - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────────────────────
def _claim_slot(report_date: date, recipients: list[str]) -> bool:
    """Insert a pending row. Returns True if we got the slot, False if taken."""
    try:
        get_client().table("report_log").insert({
            "report_type": REPORT_TYPE,
            "report_date": report_date.isoformat(),
            "status": "pending",
            "recipients": ",".join(recipients),
        }).execute()
        return True
    except Exception as exc:  # noqa: BLE001 — supabase wraps unique-violation here
        logger.info("Slot for %s already claimed (%s) — exiting.",
                    report_date, type(exc).__name__)
        return False


def _mark_sent(report_date: date) -> None:
    get_client().table("report_log").update({
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).eq("report_type", REPORT_TYPE).eq("report_date", report_date.isoformat()).execute()


def _mark_failed(report_date: date, err: str) -> None:
    try:
        get_client().table("report_log").update({
            "status": "failed",
            "error_message": err[:2000],
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }).eq("report_type", REPORT_TYPE).eq("report_date", report_date.isoformat()).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not mark failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Data fetch
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_deals(table: str, report_date: date) -> pd.DataFrame:
    """Page through Supabase to get all rows (the client caps at 1000/req)."""
    client = get_client()
    page = 0
    page_size = 1000
    out: list[dict] = []
    while True:
        resp = (
            client.table(table)
            .select("*")
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


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────
def _summary(bulk: pd.DataFrame, block: pd.DataFrame, short: pd.DataFrame) -> pd.DataFrame:
    def _row(name: str, df: pd.DataFrame) -> dict:
        if df.empty:
            return {"Type": name, "Deals": 0, "Total Quantity": 0}
        return {
            "Type": name,
            "Deals": len(df),
            "Total Quantity": int(df["quantity"].fillna(0).astype("int64").sum()),
        }
    return pd.DataFrame([
        _row("Bulk Deals",  bulk),
        _row("Block Deals", block),
        _row("Short Deals", short),
    ])


def _by_symbol(df: pd.DataFrame, has_buy_sell: bool) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["quantity"] = df["quantity"].fillna(0).astype("int64")
    if has_buy_sell:
        agg = (
            df.groupby(["symbol", "security_name", "buy_sell"], dropna=False)
              .agg(deals=("quantity", "size"), quantity=("quantity", "sum"))
              .reset_index()
        )
        wide = agg.pivot_table(
            index=["symbol", "security_name"],
            columns="buy_sell",
            values=["deals", "quantity"],
            fill_value=0,
        )
        # Flatten ('deals','B') -> 'Buy Deals' etc.
        flat_cols = []
        for stat, side in wide.columns:
            label = {"B": "Buy", "S": "Sell"}.get(side, str(side))
            flat_cols.append(f"{label} {stat.title()}")
        wide.columns = flat_cols
        wide = wide.reset_index()
        # Sort by total volume descending
        vol_cols = [c for c in wide.columns if "Quantity" in c]
        wide["_total"] = wide[vol_cols].sum(axis=1)
        wide = wide.sort_values("_total", ascending=False).drop(columns="_total")
        return wide
    else:
        agg = (
            df.groupby(["symbol", "security_name"], dropna=False)
              .agg(Deals=("quantity", "size"), Quantity=("quantity", "sum"))
              .reset_index()
              .sort_values("Quantity", ascending=False)
        )
        return agg


def _by_client(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "client_name" not in df.columns:
        return pd.DataFrame()
    df = df.copy()
    df["quantity"] = df["quantity"].fillna(0).astype("int64")
    agg = (
        df.groupby(["client_name", "buy_sell"], dropna=False)
          .agg(deals=("quantity", "size"), quantity=("quantity", "sum"))
          .reset_index()
    )
    wide = agg.pivot_table(
        index="client_name", columns="buy_sell",
        values=["deals", "quantity"], fill_value=0,
    )
    flat = []
    for stat, side in wide.columns:
        label = {"B": "Buy", "S": "Sell"}.get(side, str(side))
        flat.append(f"{label} {stat.title()}")
    wide.columns = flat
    wide = wide.reset_index()
    vol_cols = [c for c in wide.columns if "Quantity" in c]
    wide["_total"] = wide[vol_cols].sum(axis=1)
    wide = wide.sort_values("_total", ascending=False).drop(columns="_total")
    return wide


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────
_STYLE = """
<style>
  body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; color: #222; max-width: 1100px; }
  h2 { color: #1a4d8c; border-bottom: 2px solid #1a4d8c; padding-bottom: 6px; }
  h3 { color: #1a4d8c; margin-top: 28px; }
  h4 { color: #444; margin-top: 18px; margin-bottom: 6px; }
  table.report-table { border-collapse: collapse; margin: 6px 0 16px 0; font-size: 13px; }
  table.report-table th, table.report-table td {
    border: 1px solid #d8dde6; padding: 6px 12px; text-align: right;
  }
  table.report-table th { background-color: #f4f6fa; font-weight: 600; color: #1a4d8c; }
  table.report-table td:first-child, table.report-table th:first-child { text-align: left; }
  table.report-table tr:nth-child(even) td { background-color: #fafbfd; }
  .meta { color: #666; font-size: 12px; }
  .empty { color: #999; font-style: italic; }
</style>
"""


def _table_html(df: pd.DataFrame, heading: str) -> str:
    if df.empty:
        return f"<h4>{heading}</h4><p class='empty'>No deals.</p>"
    return (
        f"<h4>{heading}</h4>"
        + df.to_html(index=False, border=0, classes="report-table",
                     escape=True, float_format="{:,.0f}".format)
    )


def _build_html(
    report_date: date,
    summary: pd.DataFrame,
    bulk_sym: pd.DataFrame, block_sym: pd.DataFrame, short_sym: pd.DataFrame,
    bulk_client: pd.DataFrame, block_client: pd.DataFrame,
) -> str:
    pretty = report_date.strftime("%A, %d %b %Y")
    parts = [
        _STYLE,
        f"<h2>NSE Deals Report — {pretty}</h2>",
        '<p class="meta">Trading-day summary. Source: NSE snapshots, refreshed before report generation. '
        'Raw rows attached as CSV files.</p>',

        "<h3>1. Summary</h3>",
        summary.to_html(index=False, border=0, classes="report-table",
                        escape=True, float_format="{:,.0f}".format),

        "<h3>2. Symbol-wise Analysis</h3>",
        _table_html(bulk_sym,  "Bulk Deals by Symbol"),
        _table_html(block_sym, "Block Deals by Symbol"),
        _table_html(short_sym, "Short Deals by Symbol"),

        "<h3>3. Client-wise Analysis</h3>",
        '<p class="meta">Short deals don\'t carry client information at source.</p>',
        _table_html(bulk_client,  "Bulk Deals by Client"),
        _table_html(block_client, "Block Deals by Client"),

        '<p class="meta">— Generated by NSE surveillance pipeline.</p>',
    ]
    return f"<html><head><meta charset='utf-8'></head><body>{''.join(parts)}</body></html>"


# ─────────────────────────────────────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────────────────────────────────────
def _send_email(
    *, sender: str, password: str, sender_name: str, recipients: list[str],
    subject: str, html: str, attachments: dict[str, bytes],
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender}>"
    msg["To"] = ", ".join(recipients)
    msg.set_content("This report requires an HTML-capable email client.")
    msg.add_alternative(html, subtype="html")

    for filename, content in attachments.items():
        if content:
            msg.add_attachment(content, maintype="text", subtype="csv",
                               filename=filename)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def _csv_bytes(df: pd.DataFrame) -> bytes:
    if df.empty:
        return b""
    return df.to_csv(index=False).encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    gmail_user     = _env("GMAIL_USER")
    gmail_password = _env("GMAIL_APP_PASSWORD")
    recipients     = [r.strip() for r in _env("REPORT_RECIPIENTS").split(",") if r.strip()]
    sender_name    = os.environ.get("REPORT_SENDER_NAME", "BAC Surveillance")

    today = date.fromisoformat(today_ist())
    report_date = _previous_trading_day(today)
    logger.info("Building report for trading day %s (today IST = %s)",
                report_date, today)

    if not _claim_slot(report_date, recipients):
        return 0  # another run already handled it

    try:
        bulk  = _fetch_deals("bulk_deals",  report_date)
        block = _fetch_deals("block_deals", report_date)
        short = _fetch_deals("short_deals", report_date)
        logger.info("Fetched: %d bulk, %d block, %d short",
                    len(bulk), len(block), len(short))

        summary     = _summary(bulk, block, short)
        bulk_sym    = _by_symbol(bulk,  has_buy_sell=True)
        block_sym   = _by_symbol(block, has_buy_sell=True)
        short_sym   = _by_symbol(short, has_buy_sell=False)
        bulk_client  = _by_client(bulk)
        block_client = _by_client(block)

        html = _build_html(
            report_date, summary,
            bulk_sym, block_sym, short_sym,
            bulk_client, block_client,
        )
        attachments = {
            f"bulk_deals_{report_date}.csv":  _csv_bytes(bulk),
            f"block_deals_{report_date}.csv": _csv_bytes(block),
            f"short_deals_{report_date}.csv": _csv_bytes(short),
        }

        _send_email(
            sender=gmail_user, password=gmail_password, sender_name=sender_name,
            recipients=recipients,
            subject=f"NSE Deals Report — {report_date.strftime('%d %b %Y')}",
            html=html, attachments=attachments,
        )
        _mark_sent(report_date)
        logger.info("Sent report for %s to %s", report_date, recipients)
        return 0

    except Exception as exc:  # noqa: BLE001
        logger.exception("Report generation failed")
        _mark_failed(report_date, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
