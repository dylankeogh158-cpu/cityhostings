"""Resend email wrapper.

One function: send_monthly_report. Attaches the PDF, includes a dashboard link.
"""
from __future__ import annotations

import os
import base64
import logging
from typing import Optional
import resend

log = logging.getLogger(__name__)


def _client() -> None:
    resend.api_key = os.environ["RESEND_API_KEY"]


def send_monthly_report(
    to_email: str,
    owner_name: str,
    property_name: str,
    month_label: str,
    pdf_bytes: bytes,
    dashboard_url: str,
    from_email: Optional[str] = None,
) -> str:
    """Send the PDF to the owner. Returns Resend message id."""
    _client()
    sender = from_email or os.environ.get("REPORTS_FROM_EMAIL", "onboarding@resend.dev")

    body = (
        f"Hi {owner_name.split()[0] if owner_name else 'there'},\n\n"
        f"Your {month_label} performance report for {property_name} is attached.\n\n"
        f"You can also view live KPIs and past reports in your dashboard:\n"
        f"{dashboard_url}\n\n"
        f"Any questions, just reply to this email.\n\n"
        f"— CityHostings"
    )

    msg = resend.Emails.send({
        "from": f"CityHostings Reports <{sender}>",
        "to": [to_email],
        "subject": f"{property_name} — {month_label} Owner Report",
        "text": body,
        "attachments": [{
            "filename": f"{property_name.replace(' ', '_')}_{month_label.replace(' ', '_')}.pdf",
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
        }],
    })
    log.info("Sent monthly report to %s, resend id=%s", to_email, msg.get("id"))
    return msg.get("id", "")
