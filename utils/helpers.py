import logging
import re
from datetime import date, timedelta, datetime
from typing import Generator, Iterable

import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def date_range(start: str, end: str) -> Generator[date, None, None]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    current = s
    while current <= e:
        yield current
        current += timedelta(days=1)


# ─────────────────────────────────────────────────────────────────────────────
# Trading-day calendar
# ─────────────────────────────────────────────────────────────────────────────
# Loaded lazily from Supabase on first call. Holds CM-segment NSE holidays.
# Falls back to weekend-only if the table is empty / unreachable so the
# pipeline degrades gracefully rather than failing closed.
_HOLIDAY_CACHE: set[date] | None = None


def _load_holiday_cache() -> set[date]:
    global _HOLIDAY_CACHE
    if _HOLIDAY_CACHE is not None:
        return _HOLIDAY_CACHE
    try:
        from database.client import get_client
        resp = (
            get_client()
            .table("nse_trading_holidays")
            .select("holiday_date")
            .eq("segment", "CM")
            .execute()
        )
        rows = resp.data or []
        _HOLIDAY_CACHE = {date.fromisoformat(r["holiday_date"]) for r in rows if r.get("holiday_date")}
        logger.info("Loaded %d NSE trading holidays into cache", len(_HOLIDAY_CACHE))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load holiday calendar (%s) — falling back to weekend-only", exc)
        _HOLIDAY_CACHE = set()
    return _HOLIDAY_CACHE


def is_trading_day(d: date) -> bool:
    """True iff `d` is a Mon-Fri *and* not in the NSE holiday cache."""
    if d.weekday() >= 5:
        return False
    return d not in _load_holiday_cache()


def iter_trading_days(start: str | date, end: str | date) -> Iterable[date]:
    """
    Yield every NSE trading day in [start, end] inclusive.
    Skips weekends and any date present in nse_trading_holidays(segment='CM').
    """
    s = date.fromisoformat(start) if isinstance(start, str) else start
    e = date.fromisoformat(end) if isinstance(end, str) else end
    cur = s
    while cur <= e:
        if is_trading_day(cur):
            yield cur
        cur += timedelta(days=1)


def clean_numeric(val: str | None) -> float | None:
    if val is None:
        return None
    val = str(val).replace(",", "").strip()
    if val in ("-", "", "NA", "N/A", "--"):
        return None
    try:
        return float(val)
    except ValueError:
        return None


def clean_int(val: str | None) -> int | None:
    f = clean_numeric(val)
    return int(f) if f is not None else None


def clean_date(val: str | None, formats: list[str] | None = None) -> str | None:
    if not val or str(val).strip() in ("-", "NA", "N/A", "--", ""):
        return None
    val = str(val).strip()
    _formats = formats or [
        "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d",
        "%d/%m/%Y", "%d %b %Y", "%b %d, %Y",
    ]
    for fmt in _formats:
        try:
            return datetime.strptime(val, fmt).date().isoformat()
        except ValueError:
            continue
    logger.debug("Could not parse date: %r", val)
    return None


def clean_str(val: str | None) -> str | None:
    if val is None:
        return None
    val = str(val).strip()
    return val if val not in ("-", "NA", "N/A", "--", "") else None


def buy_sell_flag(val: str | None) -> str | None:
    if not val:
        return None
    v = val.strip().upper()
    if v in ("BUY", "B", "PURCHASE", "ACQ", "ACQUISITION"):
        return "B"
    if v in ("SELL", "S", "SALE", "DISP", "DISPOSAL"):
        return "S"
    return v[0] if v else None
