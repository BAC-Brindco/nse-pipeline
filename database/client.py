"""
Supabase client wrapper with upsert helpers, run-log tracking,
and backfill checkpointing.
"""

import logging
import uuid
from datetime import datetime, timezone, date
from typing import Any

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


# ─────────────────────────────────────────────────────────────────────────────
# Backfill checkpoint helpers
#
# A backfill that completes window [W1] but crashes mid-[W2] should restart
# from W2 on the next run, not from BACKFILL_START. We update the checkpoint
# only after a window upserts successfully — if the process dies, we'll just
# re-fetch the in-flight window (idempotent thanks to upserts).
# ─────────────────────────────────────────────────────────────────────────────
def get_checkpoint(dataset: str) -> date | None:
    try:
        resp = (
            get_client()
            .table("backfill_checkpoint")
            .select("last_completed_date")
            .eq("dataset", dataset)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows or not rows[0].get("last_completed_date"):
            return None
        return date.fromisoformat(rows[0]["last_completed_date"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_checkpoint(%s) failed: %s", dataset, exc)
        return None


def set_checkpoint(dataset: str, last_completed_date: date, rows_added: int = 0,
                   run_id: str | None = None) -> None:
    try:
        # Atomically advance: only move forward, never backward.
        existing = get_checkpoint(dataset)
        if existing and existing >= last_completed_date:
            return
        get_client().table("backfill_checkpoint").upsert({
            "dataset": dataset,
            "last_completed_date": last_completed_date.isoformat(),
            "rows_total": rows_added,
            "last_run_id": run_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="dataset").execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_checkpoint(%s, %s) failed: %s", dataset, last_completed_date, exc)


def upsert(table: str, records: list[dict], conflict_columns: list[str] | None = None) -> int:
    if not records:
        return 0
    client = get_client()
    # Supabase upsert uses ON CONFLICT DO UPDATE semantics
    resp = client.table(table).upsert(records, on_conflict=",".join(conflict_columns or [])).execute()
    inserted = len(resp.data) if resp.data else 0
    logger.info("Upserted %d rows into %s", inserted, table)
    return inserted


def bulk_upsert(table: str, records: list[dict], conflict_columns: list[str] | None = None, batch_size: int = 500) -> int:
    total = 0
    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        total += upsert(table, batch, conflict_columns)
    return total


class RunLogger:
    """Context manager that writes start/end rows to scrape_run_log."""

    def __init__(self, dataset: str, scrape_date: str):
        self.dataset = dataset
        self.scrape_date = scrape_date
        self.run_id = str(uuid.uuid4())
        self.start_time = datetime.now(timezone.utc)
        self.records_fetched = 0
        self.records_upserted = 0
        self._error: str | None = None

    def __enter__(self) -> "RunLogger":
        return self

    def set_fetched(self, n: int):
        self.records_fetched = n

    def set_upserted(self, n: int):
        self.records_upserted = n

    def fail(self, msg: str):
        self._error = msg

    def __exit__(self, exc_type, exc_val, exc_tb):
        status = "success"
        if exc_type is not None:
            status = "failed"
            self._error = self._error or str(exc_val)
        elif self._error:
            status = "partial"

        row = {
            "run_id": self.run_id,
            "dataset": self.dataset,
            "status": status,
            "records_fetched": self.records_fetched,
            "records_upserted": self.records_upserted,
            "error_message": self._error,
            "start_time": self.start_time.isoformat(),
            "end_time": datetime.now(timezone.utc).isoformat(),
            "scrape_date": self.scrape_date,
        }
        try:
            get_client().table("scrape_run_log").insert(row).execute()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write run log: %s", exc)

        return False  # do not suppress exceptions
