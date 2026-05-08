"""
PIT (Prohibition of Insider Trading) Disclosures scraper.

NSE exposes TWO related PIT endpoints with different shapes:

  /api/corporates-pit      → PARSED transaction content (acquirer name,
                              share counts, percentages). Older windows
                              return rich data; recent windows often
                              return empty until disclosures are processed.
  /api/corporates-pit-gg   → FILINGS INDEX (submission metadata + URLs
                              to the XBRL files). Always populated for
                              recent disclosures. Transaction details
                              are NOT in this payload — they live in
                              the linked XBRL file at xml_url.

Strategy:
  - Both endpoints' rows land in pit_disclosures.
  - -gg rows carry app_id / xml_url / broadcast_at; these will drive
    a future XBRL-parsing pass (xbrl_parsed flag tracks completion).
  - corporates-pit rows carry parsed transaction columns directly.
  - app_id is the unique key for -gg filings (idempotent re-runs).

Segments per endpoint:
  equities    — main NSE board (bulk of disclosures)
  sme         — NSE Emerge platform
  invitsreits — InvITs and REITs
"""

import logging
from datetime import date, timedelta
from typing import Any

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger, get_checkpoint, set_checkpoint
from utils.helpers import clean_str, clean_date, clean_numeric, buy_sell_flag, today_ist
from config import BACKFILL_START

logger = logging.getLogger(__name__)

_PIT_URLS = (
    "https://www.nseindia.com/api/corporates-pit",
    "https://www.nseindia.com/api/corporates-pit-gg",
)
_SEGMENTS = ("equities", "sme", "invitsreits")
_WINDOW_DAYS = 90


def _api_date(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _first(row: dict, *keys: str) -> Any:
    """Return the first non-None value in `row` for any of the given keys."""
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "-"):
            return v
    return None


def _parse_broadcast(val: str | None) -> str | None:
    """NSE's `broadcastDateTime` arrives as 'DD-Mon-YYYY HH:MM:SS' in IST.
    Returns ISO string with IST offset for storage as TIMESTAMPTZ."""
    if not val:
        return None
    try:
        from datetime import datetime as _dt
        dt = _dt.strptime(str(val).strip(), "%d-%b-%Y %H:%M:%S")
        return dt.isoformat() + "+05:30"
    except (ValueError, TypeError):
        return None


def _parse_pit_record(row: dict, segment: str, scrape_date: str, source_url: str) -> dict:
    # Wide candidate list — NSE has shipped at least 4 different field-name
    # conventions for these endpoints over the years. We try every known
    # variant and fall back to raw_payload for forensic recovery.
    return {
        "symbol":               clean_str(_first(row, "symbol", "Symbol", "SYMBOL", "scripCode")),
        "company_name":         clean_str(_first(row, "company", "companyName", "issuerName",
                                                 "nameOfCompany", "anyOtherCompany")),
        "isin":                 clean_str(_first(row, "isin", "ISIN", "scripIsinNo")),
        "acquirer_name":        clean_str(_first(row, "acqName", "acquirerName", "name",
                                                 "nameOfThePerson", "namePerson",
                                                 "personName", "anyPerson",
                                                 "personDP")),
        "acquirer_category":    clean_str(_first(row, "personCategory", "acqCategory",
                                                 "categoryOfPerson", "categoryPerson",
                                                 "categoryName")),
        "regulation":           clean_str(_first(row, "regulation", "Regulation",
                                                 "regulationName")),
        "acq_disp":             buy_sell_flag(_first(row, "secAcq", "buyOrSell",
                                                     "acquistionDisposal",
                                                     "acquisitionDisposal",
                                                     "modeOfAcq", "modeOfAcquisition",
                                                     "buySellTradeType", "tdpTransactionType")),
        # Share-count candidates separate from pct candidates. NSE field
        # names like `befAcqSharesNo` are share counts, NOT percentages —
        # the original parser blew up the NUMERIC(10,6) pct columns by
        # routing share counts there. The schema is now NUMERIC(20,*) so
        # bad data won't crash the insert, but the parser is also fixed.
        "before_acq_shares":    clean_numeric(_first(row, "befAcqSharesNo",
                                                     "noOfSharesPriorTransaction",
                                                     "shareCountPrior")),
        "before_acq_pct":       clean_numeric(_first(row, "beforeAcqSharePercentage",
                                                     "befAcqShareholdingPercentage",
                                                     "shareholdingPriorPct",
                                                     "preTransactionPct",
                                                     "befAcqSharesPer")),
        "acq_disp_shares":      clean_numeric(_first(row, "secAcq",
                                                     "noOfShares",
                                                     "noOfSharesAcquiredOrDisposed",
                                                     "shareCount", "qty",
                                                     "acquiredDisposedShares")),
        "acq_disp_pct":         clean_numeric(_first(row, "secAcqDispPct",
                                                     "secAcqDisp",
                                                     "acquiredDisposedSharesPercentage",
                                                     "shareholdingPercentage",
                                                     "transactionPct")),
        "after_acq_shares":     clean_numeric(_first(row, "afterAcqSharesNo",
                                                     "noOfSharesPostTransaction",
                                                     "shareCountPost")),
        "after_acq_pct":        clean_numeric(_first(row, "afterAcqSharePercentage",
                                                     "afterAcqShareholdingPercentage",
                                                     "shareholdingAfterPct",
                                                     "postTransactionPct",
                                                     "afterAcqSharesPer")),
        "transaction_type":     clean_str(_first(row, "tdpTransactionType",
                                                 "modeOfAcq", "modeOfAcquisition",
                                                 "transactionType")),
        "date_of_allotment":    clean_date(_first(row, "date", "dateOfAllotment",
                                                  "acqfromDt", "fromDate",
                                                  "transactionDate",
                                                  "dateOfAcquisition",
                                                  "dateOfAllotmentAdvice")),
        "date_of_intimation":   clean_date(_first(row, "intimDt", "dateOfIntimation",
                                                  "intimationDate",
                                                  "dateOfIntimationToCompany",
                                                  "intimationDateToCompany",
                                                  "dt")),
        "mode_of_acq":          clean_str(_first(row, "modeOfAcq", "modeOfAcquisition",
                                                 "tdpTransactionType",
                                                 "modeOfAcquisitionDisposal")),
        "exchange":             clean_str(_first(row, "exchange") or "NSE"),
        "segment":              segment,
        "remarks":              clean_str(_first(row, "remarks", "Remarks", "note", "revisionRemark")),
        # Filings-index columns — populated by /api/corporates-pit-gg.
        # NULL for /api/corporates-pit rows. The xbrl_parsed flag and
        # xml_url/ixbrl_url drive the future XBRL extraction pass.
        "app_id":               clean_str(_first(row, "appId", "applicationId")),
        "prev_app_id":          clean_str(_first(row, "prevAppId", "previousAppId")),
        "xml_url":              clean_str(_first(row, "xmlFileName", "xmlUrl")),
        "ixbrl_url":            clean_str(_first(row, "ixbrl", "ixbrlFileName", "ixbrlUrl")),
        "broadcast_at":         _parse_broadcast(_first(row, "broadcastDateTime", "broadcasttime")),
        "submission_type":      clean_str(_first(row, "typeOfSubmission", "submissionType")),
        "xbrl_parsed":          False,
        "data_source":          "historical_api",
        "source_url":           source_url,
        "raw_payload":          row,  # JSONB — full upstream record for forensic recovery
        "scrape_date":          scrape_date,
    }


_PIT_REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"


def _fetch_window(session: NSESession, base_url: str, segment: str,
                  from_dt: date, to_dt: date) -> tuple[list[dict], str]:
    params = {
        "index":     segment,
        "from_date": _api_date(from_dt),
        "to_date":   _api_date(to_dt),
    }
    url_with_params = (
        f"{base_url}?index={segment}&from_date={_api_date(from_dt)}"
        f"&to_date={_api_date(to_dt)}"
    )
    payload = session.get_json(base_url, params=params, headers={"Referer": _PIT_REFERER})
    raw = payload.get("data", payload) if isinstance(payload, dict) else payload
    rows = raw if isinstance(raw, list) else []
    if rows:
        logger.info("PIT [%s] %s → %s: %d rows from %s",
                    segment, from_dt, to_dt, len(rows),
                    base_url.rsplit("/", 1)[-1])
    return rows, url_with_params


def _scrape_segment_window(session: NSESession, segment: str, from_dt: date, to_dt: date,
                           scrape_date: str) -> list[dict]:
    """Union over both endpoints for a (segment, window). Dedup by raw payload."""
    seen: set[str] = set()
    out: list[dict] = []
    for base_url in _PIT_URLS:
        try:
            raw_rows, src = _fetch_window(session, base_url, segment, from_dt, to_dt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PIT %s @ %s [%s → %s] failed: %s",
                           segment, base_url.rsplit("/", 1)[-1], from_dt, to_dt, exc)
            continue
        for r in raw_rows:
            key = repr(sorted(r.items())) if isinstance(r, dict) else repr(r)
            if key in seen:
                continue
            seen.add(key)
            rec = _parse_pit_record(r, segment, scrape_date, src)
            if rec.get("symbol"):
                out.append(rec)
    return out


def scrape_pit_daily(session: NSESession | None = None) -> dict:
    """Daily PIT scrape with a 14-day rolling window.

    NSE's corporates-pit (parsed-content) endpoint sometimes returns nothing
    for the last 1-2 days (disclosures still being processed) but populates
    older entries. A 14-day window ensures we eventually capture every
    disclosure as it gets parsed by NSE — without needing to re-fetch
    arbitrarily old ranges. Upsert is idempotent on (segment, app_id) for
    -gg rows, so re-fetching the same window doesn't duplicate.
    """
    session = session or NSESession()
    scrape_date = today_ist()
    today = date.fromisoformat(scrape_date)
    window_start = today - timedelta(days=14)

    total_fetched = total_upserted = 0

    with RunLogger("pit", scrape_date) as run:
        for segment in _SEGMENTS:
            try:
                records = _scrape_segment_window(session, segment, window_start, today, scrape_date)
                total_fetched += len(records)
                if records:
                    n = bulk_upsert(
                        "pit_disclosures", records,
                        conflict_columns=["segment", "app_id"],
                    )
                    total_upserted += n
            except Exception as exc:  # noqa: BLE001
                logger.error("PIT daily %s failed: %s", segment, exc)

        run.set_fetched(total_fetched)
        run.set_upserted(total_upserted)

    logger.info("PIT daily: %d fetched, %d upserted", total_fetched, total_upserted)
    return {"fetched": total_fetched, "upserted": total_upserted}


def scrape_pit_historical(
    session: NSESession | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    resume: bool = True,
) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()

    if resume and start_date is None:
        ck = get_checkpoint("pit")
        if ck:
            start = ck + timedelta(days=1)
            logger.info("Resuming pit backfill from checkpoint %s", start)
        else:
            start = date.fromisoformat(BACKFILL_START["pit"])
    else:
        start = date.fromisoformat(start_date or BACKFILL_START["pit"])

    end = date.fromisoformat(end_date or scrape_date)
    if start > end:
        logger.info("pit: nothing to backfill (start %s > end %s)", start, end)
        return {"fetched": 0, "upserted": 0}

    total_fetched = total_upserted = 0

    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=_WINDOW_DAYS - 1), end)
        window_records: list[dict] = []

        for segment in _SEGMENTS:
            logger.info("PIT [%s] window %s → %s", segment, cursor, window_end)
            try:
                records = _scrape_segment_window(session, segment, cursor, window_end, scrape_date)
                window_records.extend(records)
            except Exception as exc:  # noqa: BLE001
                logger.error("PIT [%s] window %s failed: %s", segment, cursor, exc)

        total_fetched += len(window_records)
        if window_records:
            # Split rows by whether they have app_id (-gg index) or not
            # (parsed corporates-pit). Different upsert keys per shape.
            with_app    = [r for r in window_records if r.get("app_id")]
            without_app = [r for r in window_records if not r.get("app_id")]

            n = 0
            if with_app:
                n += bulk_upsert("pit_disclosures", with_app,
                                 conflict_columns=["segment", "app_id"])
            if without_app:
                # corporates-pit rows: no stable filing ID, append-only.
                # raw_payload preserves truth; downstream views can dedup.
                n += bulk_upsert("pit_disclosures", without_app)
            total_upserted += n
            logger.info("PIT window %s → %s: %d rows (%d w/app_id, %d w/o)",
                        cursor, window_end, n, len(with_app), len(without_app))

        set_checkpoint("pit", window_end, rows_added=total_upserted)
        cursor = window_end + timedelta(days=1)

    logger.info("PIT historical done: %d fetched, %d upserted", total_fetched, total_upserted)
    return {"fetched": total_fetched, "upserted": total_upserted}
