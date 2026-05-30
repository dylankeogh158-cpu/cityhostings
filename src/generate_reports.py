"""Monthly report generator.

Runs on the 1st of each month from GitHub Actions. For each active property:
  1. Compute comparison pack (this month, prev month, same month last year)
  2. Generate AI commentary (single Claude API call, cached system prompt)
  3. Render KPI charts (matplotlib → SVG embedded in HTML)
  4. Render HTML from Jinja2 template
  5. Convert HTML → PDF (WeasyPrint)
  6. Upload PDF to Supabase Storage
  7. Email PDF to owner via Resend (or to TEST_RECIPIENT in dry-run mode)
  8. Insert audit row in monthly_reports

Env vars:
  TEST_MODE=true        — sends all PDFs to TEST_RECIPIENT instead of owners
  TEST_RECIPIENT=...    — your email for dry runs
  REPORT_MONTH=YYYY-MM  — override which month to generate (defaults to last month)
"""
from __future__ import annotations

import os
import sys
import json
import logging
import base64
from io import BytesIO
from datetime import date
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML
from supabase import create_client
from dateutil.relativedelta import relativedelta

from cityhostings.db import fetch_active_properties, conn
from cityhostings.kpis import comparison_pack
from cityhostings.ai import generate_commentary
from cityhostings.email import send_monthly_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reports")

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "reports" / "templates"
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://cityhostings-owner.streamlit.app")


def target_month() -> date:
    """Default: last full calendar month. Override with REPORT_MONTH=YYYY-MM."""
    override = os.environ.get("REPORT_MONTH")
    if override:
        y, m = override.split("-")
        return date(int(y), int(m), 1)
    today = date.today()
    return (today.replace(day=1) - relativedelta(months=1))


def fmt_currency(n, currency="GBP"):
    if n is None:
        return "—"
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, "")
    return f"{sym}{float(n):,.2f}"


def fmt_pct(n):
    if n is None:
        return "—"
    return f"{float(n) * 100:.1f}%" if abs(float(n)) <= 1 else f"{float(n):.1f}%"


def delta(now, then):
    """Return signed percent change string or '—'."""
    if now is None or then in (None, 0):
        return "—"
    pct = ((float(now) - float(then)) / float(then)) * 100
    return f"{pct:+.1f}%"


def chart_svg(trend: list[dict], metric: str, ylabel: str, color: str = "#1F3A5F") -> str:
    """Render a small trend chart as inline SVG."""
    if not trend:
        return ""
    fig, ax = plt.subplots(figsize=(5.5, 2.2))
    months = [r["month"].strftime("%b %y") for r in trend]
    values = [float(r[metric] or 0) for r in trend]
    ax.plot(months, values, marker="o", color=color, linewidth=2)
    ax.fill_between(range(len(months)), values, alpha=0.1, color=color)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.tick_params(axis="x", labelsize=8, rotation=45)
    ax.tick_params(axis="y", labelsize=8)
    ax.grid(True, alpha=0.3)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format="svg", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue().decode("utf-8")


def upload_pdf(supa, property_id: str, month: date, pdf_bytes: bytes) -> str:
    """Upload PDF to Supabase Storage, return signed URL valid 90 days."""
    bucket = "reports"
    path = f"{property_id}/{month.strftime('%Y-%m')}.pdf"

    # Upsert (overwrite if already exists for this property+month)
    try:
        supa.storage.from_(bucket).upload(
            path, pdf_bytes,
            file_options={"content-type": "application/pdf", "upsert": "true"},
        )
    except Exception as exc:
        log.warning("Upload exception (may be benign upsert): %s", exc)

    signed = supa.storage.from_(bucket).create_signed_url(path, 60 * 60 * 24 * 90)
    return signed.get("signedURL") or signed.get("signed_url") or ""


def save_report_audit(property_id: str, month: date, pdf_url: str,
                      commentary: str, kpis_snapshot: dict, emailed_to: str) -> None:
    with conn() as c:
        c.execute(
            """
            insert into monthly_reports (
              property_id, month, pdf_url, ai_commentary, kpis_snapshot,
              emailed_at, emailed_to
            ) values (%s, %s, %s, %s, %s::jsonb, now(), %s)
            on conflict (property_id, month) do update set
              pdf_url = excluded.pdf_url,
              ai_commentary = excluded.ai_commentary,
              kpis_snapshot = excluded.kpis_snapshot,
              emailed_at = excluded.emailed_at,
              emailed_to = excluded.emailed_to
            """,
            (property_id, month, pdf_url, commentary,
             json.dumps(kpis_snapshot, default=str), emailed_to),
        )
        c.commit()


def main() -> int:
    month = target_month()
    month_label = month.strftime("%B %Y")
    log.info("Generating reports for %s", month_label)

    test_mode = os.environ.get("TEST_MODE", "").lower() == "true"
    test_recipient = os.environ.get("TEST_RECIPIENT")
    if test_mode and not test_recipient:
        log.error("TEST_MODE=true but TEST_RECIPIENT not set")
        return 1

    supa = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["currency"] = fmt_currency
    env.filters["pct"] = fmt_pct
    env.filters["delta"] = delta
    template = env.get_template("monthly.html")

    properties = fetch_active_properties()
    log.info("%d active properties", len(properties))

    failures = 0
    for p in properties:
        try:
            log.info("→ %s", p["name"])
            pack = comparison_pack(p["id"], month, float(p["mgmt_fee_pct"]))

            # Skip properties with zero revenue this month — likely not active yet
            if (pack["this"]["kpis"]["room_revenue"] or 0) == 0 and not test_mode:
                log.info("  zero revenue, skipping")
                continue

            commentary = generate_commentary(
                p["name"], p["location"] or "", month_label,
                p["currency"], pack,
            )

            html_str = template.render(
                property=p,
                month_label=month_label,
                month=month,
                pack=pack,
                commentary=commentary,
                revenue_chart=chart_svg(pack["trend"], "room_revenue", "Revenue"),
                occupancy_chart=chart_svg(pack["trend"], "occupancy_pct", "Occupancy %", color="#2E8B57"),
                dashboard_url=DASHBOARD_URL,
            )

            pdf_bytes = HTML(string=html_str).write_pdf()
            log.info("  pdf size: %d KB", len(pdf_bytes) // 1024)

            pdf_url = upload_pdf(supa, str(p["id"]), month, pdf_bytes)

            recipient = test_recipient if test_mode else p["owner_email"]
            send_monthly_report(
                to_email=recipient,
                owner_name=p["owner_name"] or "",
                property_name=p["name"],
                month_label=month_label,
                pdf_bytes=pdf_bytes,
                dashboard_url=DASHBOARD_URL,
            )

            save_report_audit(str(p["id"]), month, pdf_url, commentary,
                              pack["this"], recipient)
            log.info("  ✓ sent to %s", recipient)

        except Exception:
            log.exception("  ✗ failed for %s", p["name"])
            failures += 1

    log.info("Done. Failures: %d", failures)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
