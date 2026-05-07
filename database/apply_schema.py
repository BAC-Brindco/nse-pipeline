"""
Applies schema.sql to the connected Supabase project via the REST API.
Run this once on initial setup, or via the schema_migration workflow.
"""

import os
import sys
import logging
from pathlib import Path

from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("schema")


def apply():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]

    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")

    client = create_client(url, key)

    # Supabase JS client exposes rpc; the Python client does too.
    # We split on statement boundaries and execute each individually.
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    logger.info("Applying %d SQL statements…", len(statements))

    for i, stmt in enumerate(statements, 1):
        try:
            client.rpc("exec_sql", {"sql": stmt + ";"}).execute()
            logger.info("[%d/%d] OK", i, len(statements))
        except Exception as exc:  # noqa: BLE001
            # exec_sql may not exist; fall back to a raw postgrest call
            logger.warning("[%d/%d] rpc failed (%s), trying raw POST…", i, len(statements), exc)
            import requests
            resp = requests.post(
                f"{url}/rest/v1/rpc/exec_sql",
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={"sql": stmt + ";"},
                timeout=30,
            )
            if resp.ok:
                logger.info("[%d/%d] OK (raw)", i, len(statements))
            else:
                logger.error("[%d/%d] FAILED: %s — %s", i, len(statements), resp.status_code, resp.text)

    logger.info("Schema migration complete.")


if __name__ == "__main__":
    apply()
