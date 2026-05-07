import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]

NSE_BASE_URL = "https://www.nseindia.com"
NSE_ARCHIVE_URL = "https://archives.nseindia.com"

# NSE market closes at 15:30 IST; data is finalised ~16:00 IST
NSE_TIMEZONE = "Asia/Kolkata"

# Retry / rate-limit config
MAX_RETRIES = 5
BACKOFF_MULTIPLIER = 2.0
REQUEST_DELAY_SECONDS = 1.5   # polite inter-request pause

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "keep-alive",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Historical backfill start dates (conservative safe starts)
BACKFILL_START = {
    "bulk_deals":  "2002-01-01",
    "block_deals": "2007-01-01",
    "asm":         "2013-01-01",
    "gsm":         "2016-06-01",
    "t2t":         "2010-01-01",
    "pit":         "2015-05-15",   # SEBI PIT Regulations 2015 effective date
}
