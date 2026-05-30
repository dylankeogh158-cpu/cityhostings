"""Claude API wrapper for monthly performance commentary.

Designed to be token-light:
  - Model: claude-haiku-4-5 (cheapest, plenty good for 250-word summaries)
  - Prompt caching: the system prompt is identical across all 25 properties,
    so we mark it as ephemeral cacheable. After the first call in a batch,
    subsequent calls effectively skip the system tokens.
  - Compact user payload: numbers only, no narrative. ~250 input tokens.
  - max_tokens=500 caps output cost.
  - temperature=0.3 keeps things consistent.

Expected cost at 25 properties × 12 months/year = 300 calls/year:
  Input  (cached after 1st):  ~75k tokens/yr × $1/M  = ~$0.08
  Output: ~110k tokens/yr     × $5/M               = ~$0.55
  TOTAL:                                              ~$0.63/year
"""
from __future__ import annotations

import os
import json
import logging
from typing import Optional
from anthropic import Anthropic

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"

# Cached across all calls in a batch — kept short to save tokens
SYSTEM_PROMPT = (
    "You are an analyst writing a 3-paragraph monthly summary for the owner "
    "of a short-term rental property. Be specific — cite numbers, not adjectives. "
    "Avoid filler phrases like 'great month' or 'room for improvement'. "
    "If a metric declined, say so plainly. End with one concrete suggestion. "
    "Target 220-280 words total. No headings, no bullet lists, no markdown — just prose."
)

_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _fmt_currency(n, currency: str = "GBP") -> str:
    if n is None:
        return "n/a"
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, "")
    return f"{sym}{float(n):,.0f}"


def _fmt_pct(n) -> str:
    if n is None:
        return "n/a"
    return f"{float(n) * 100:.1f}%" if abs(float(n)) <= 1 else f"{float(n):.1f}%"


def _pct_change(now, then) -> str:
    if now is None or then in (None, 0):
        return "n/a"
    return f"{((float(now) - float(then)) / float(then)) * 100:+.1f}%"


def build_user_payload(property_name: str, location: str, month_label: str,
                       currency: str, pack: dict) -> str:
    """Serialise the comparison pack as a compact text block.

    Every line is one metric. Claude is great at reading structured key-value
    text — we don't need JSON or XML, and they would cost more tokens.
    """
    this = pack["this"]
    prev = pack["prev"]
    yoy = pack["yoy"]

    def kr(d):  # kpis row shortcut
        return d["kpis"]

    lines = [
        f"Property: {property_name}, {location}",
        f"Month: {month_label}",
        "",
        "Metric | This | Prev month (Δ) | Last year (Δ)",
        f"Revenue | {_fmt_currency(kr(this)['room_revenue'], currency)} | "
        f"{_fmt_currency(kr(prev)['room_revenue'], currency)} ({_pct_change(kr(this)['room_revenue'], kr(prev)['room_revenue'])}) | "
        f"{_fmt_currency(kr(yoy)['room_revenue'], currency)} ({_pct_change(kr(this)['room_revenue'], kr(yoy)['room_revenue'])})",
        f"Occupancy | {_fmt_pct(kr(this)['occupancy_rate'])} | "
        f"{_fmt_pct(kr(prev)['occupancy_rate'])} | {_fmt_pct(kr(yoy)['occupancy_rate'])}",
        f"ADR | {_fmt_currency(kr(this)['adr'], currency)} | "
        f"{_fmt_currency(kr(prev)['adr'], currency)} | {_fmt_currency(kr(yoy)['adr'], currency)}",
        f"RevPAR | {_fmt_currency(kr(this)['revpar'], currency)} | "
        f"{_fmt_currency(kr(prev)['revpar'], currency)} | {_fmt_currency(kr(yoy)['revpar'], currency)}",
        f"Bookings | {kr(this)['bookings']} | {kr(prev)['bookings']} | {kr(yoy)['bookings']}",
        f"ALOS nights | {this['alos'] or 'n/a'} | {prev['alos'] or 'n/a'} | {yoy['alos'] or 'n/a'}",
        "",
        "Source mix (revenue):",
    ]
    for s in pack["source_mix"][:5]:  # top 5 sources only — keeps payload small
        lines.append(f"  {s['source']}: {_fmt_currency(s['revenue'], currency)}")

    lines.append("")
    lines.append("Write the 3 paragraphs now.")
    return "\n".join(lines)


def generate_commentary(property_name: str, location: str, month_label: str,
                        currency: str, pack: dict) -> str:
    """Call Claude and return the 3-paragraph commentary.

    On any failure, returns a graceful fallback so the report still ships.
    """
    user_msg = build_user_payload(property_name, location, month_label, currency, pack)
    try:
        resp = _get_client().messages.create(
            model=MODEL,
            max_tokens=500,
            temperature=0.3,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # share across the batch
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001 — we always want a report to ship
        log.error("Claude commentary failed for %s %s: %s", property_name, month_label, exc)
        return (
            f"Performance commentary for {property_name} was unavailable for "
            f"{month_label}. Please refer to the KPI and P&L tables above."
        )
