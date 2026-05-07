import logging
import re
from datetime import date, timedelta, datetime
from typing import Generator

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
