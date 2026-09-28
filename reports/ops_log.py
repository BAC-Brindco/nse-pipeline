"""
Operator log emails — one short message per run, to the operator only.

The desk-facing reports go to REPORT_RECIPIENTS (the bac-reports Google Group).
This module is the other channel: a plain status line to the pipeline operator
saying what a run did — sent, nothing new so nothing sent, no report today, or
failed and why. It exists so a silent day is never ambiguous: no report in the
inbox plus no log email means the trigger itself never fired.

The recipient is fixed in code on purpose. It is not read from REPORT_RECIPIENTS
or any other secret, so a misconfigured secret can never route a failure notice
(which carries tracebacks and run links) to the whole distribution list.

Standard library only. The workflow's `if: failure()` step imports this with the
runner's system Python, after `pip install` may itself have been what failed.

    python -m reports.ops_log --if-not-sent --report "Daily Deals" \
        --detail "Job failed before the report step ran"
"""

from __future__ import annotations

import argparse
import logging
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape

logger = logging.getLogger("nse.ops_log")

OPS_LOG_RECIPIENT = "parv.bangar@brindco.com"

# Written after a log email goes out, so the workflow's failure step can tell
# "Python already reported this" from "the job died before Python could".
SENT_MARKER = "ops_log_sent.marker"

_IST = timezone(timedelta(hours=5, minutes=30))

_COLOURS = {
    "SENT": "#2f6b3a",
    "UPDATED": "#2f6b3a",
    "NO CHANGE": "#5b6470",
    "NOT SENT": "#5b6470",
    "SKIPPED": "#8a6d1f",
    "INCOMPLETE": "#8a6d1f",
    "FAILED": "#9b2c2c",
}


def _run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run = os.environ.get("GITHUB_RUN_ID")
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else ""


def send_ops_log(report: str, status: str, headline: str, details: list[str] | None = None) -> bool:
    """Email the operator one status line. Never raises; returns True if sent.

    report   — "Daily Deals" / "Daily Announcements"
    status   — SENT, UPDATED, NO CHANGE, NOT SENT, SKIPPED, INCOMPLETE, FAILED
    headline — one sentence: what happened
    details  — optional extra lines (counts, error text, reason)
    """
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    now = datetime.now(_IST)
    subject = f"[BAC ops] {report}: {status} — {now:%a %d %b %H:%M} IST"
    if not user or not password:
        logger.warning("Ops log not sent (no SMTP credentials): %s — %s", subject, headline)
        return False

    lines = list(details or [])
    run_url = _run_url()
    if run_url:
        lines.append(f"Run: {run_url}")

    colour = _COLOURS.get(status, "#5b6470")
    items = "".join(f"<li style='margin:2px 0'>{escape(line)}</li>" for line in lines)
    html = (
        "<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1f2328'>"
        f"<p style='margin:0 0 8px'><b style='color:{colour}'>{escape(status)}</b>"
        f" &middot; {escape(report)} &middot; {now:%d %b %Y %H:%M} IST</p>"
        f"<p style='margin:0 0 8px'>{escape(headline)}</p>"
        + (f"<ul style='margin:0;padding-left:18px;color:#444'>{items}</ul>" if items else "")
        + "<p style='margin:12px 0 0;color:#888;font-size:12px'>Operator log &mdash; "
          "sent only to the pipeline operator, never to the report list.</p></div>"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"BAC Pipeline Log <{user}>"
    msg["To"] = OPS_LOG_RECIPIENT
    msg.set_content("\n".join([f"{status} — {report}", headline, *lines]))
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops log email failed: %s", exc)
        return False
    try:
        with open(SENT_MARKER, "w", encoding="utf-8") as fh:
            fh.write(subject)
    except OSError:
        pass
    logger.info("Ops log sent to %s: %s", OPS_LOG_RECIPIENT, subject)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Send an operator log email")
    ap.add_argument("--report", required=True)
    ap.add_argument("--status", default="FAILED")
    ap.add_argument("--headline", default="The workflow failed before the report step could report.")
    ap.add_argument("--detail", action="append", default=[])
    ap.add_argument("--if-not-sent", action="store_true",
                    help="Do nothing if this run already sent a log email")
    a = ap.parse_args()
    if a.if_not_sent and os.path.exists(SENT_MARKER):
        logger.info("Ops log already sent by this run — nothing to do.")
        sys.exit(0)
    sys.exit(0 if send_ops_log(a.report, a.status, a.headline, a.detail) else 1)
