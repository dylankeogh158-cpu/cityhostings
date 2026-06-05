"""KPI + P&L queries.

All read-only — these functions query the materialised views and return dicts
ready to be rendered in PDFs and Streamlit.
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from .db import conn


def month_kpis(property_id: str, month: date) -> dict:
    """Single month's KPI snapshot for a property."""
    with conn() as c:
        row = c.execute(
            """
            select bookings, room_nights_sold, available_room_nights,
                   room_revenue, ota_fees, adr, occupancy_rate, revpar
              from monthly_kpis
             where property_id = %s and month = %s
            """,
            (property_id, month),
        ).fetchone()
    return dict(row) if row else _empty_kpis()


def _empty_kpis() -> dict:
    return dict(
        bookings=0, room_nights_sold=0, available_room_nights=0,
        room_revenue=0, ota_fees=0, adr=0, occupancy_rate=0, revpar=0,
    )


def alos(property_id: str, month: date) -> Optional[float]:
    """Average length of stay for reservations checking in during the month."""
    with conn() as c:
        row = c.execute(
            """
            select avg(nights)::numeric(6,2) as alos
              from reservations
             where property_id = %s
               and status in ('confirmed', 'checked_out')
               and date_trunc('month', check_in) = %s
            """,
            (property_id, month),
        ).fetchone()
    return float(row["alos"]) if row and row["alos"] is not None else None


def source_mix(property_id: str, month: date) -> list[dict]:
    """Revenue by booking source for the month."""
    with conn() as c:
        rows = c.execute(
            """
            select source,
                   round(revenue::numeric, 2) as revenue,
                   nights
              from monthly_source_mix
             where property_id = %s and month = %s
             order by revenue desc nulls last
            """,
            (property_id, month),
        ).fetchall()
    return [dict(r) for r in rows]


def revenue_trend(property_id: str, end_month: date, months_back: int = 13) -> list[dict]:
    """Last N months of revenue / occupancy for the trend charts."""
    with conn() as c:
        rows = c.execute(
            """
            select month,
                   round(room_revenue::numeric, 2) as room_revenue,
                   round((occupancy_rate * 100)::numeric, 1) as occupancy_pct,
                   round(adr::numeric, 2) as adr
              from monthly_kpis
             where property_id = %s
               and month <= %s
               and month > (%s::date - interval '%s months')
             order by month asc
            """,
            (property_id, end_month, end_month, months_back),
        ).fetchall()
    return [dict(r) for r in rows]

DEFAULT_OTA_RATES = {
    'booking.com': 0.15,
    'expedia': 0.15,
    'airbnb': 0.15,
    'airbnb (api)': 0.15,
    'vrbo': 0.08,
    'agoda': 0.15,
}

def pnl(property_id: str, month: date, mgmt_fee_pct: float) -> dict:
    """Full P&L for a property for a given month.

    - OTA fees auto-calculated from source mix × industry-standard rates
      (Cloudbeds doesn't pass commission data)
    - Cleaning + linen auto-calculated from bookings × per-turnover rates
      stored on the property
    - Manual expense entries added ON TOP for one-off costs.
    """
    kpis = month_kpis(property_id, month)
    gross = float(kpis["room_revenue"] or 0)
    bookings = int(kpis["bookings"] or 0)

    # OTA fees = source revenue × default commission rate per source
    sources = source_mix(property_id, month)
    ota = sum(
        float(s["revenue"] or 0) * DEFAULT_OTA_RATES.get((s["source"] or "").strip().lower(), 0)
        for s in sources
    )
    ota = round(ota, 2)

    # Property's per-turnover rates
    with conn() as c:
        rate_row = c.execute(
            """
            select coalesce(cleaning_fee_per_turnover, 0) as cf,
                   coalesce(linen_fee_per_turnover, 0) as lf
              from properties where id = %s
            """,
            (property_id,),
        ).fetchone()

    cf_rate = float(rate_row["cf"]) if rate_row else 0
    lf_rate = float(rate_row["lf"]) if rate_row else 0

    cleaning_auto = round(bookings * cf_rate, 2)
    linen_auto = round(bookings * lf_rate, 2)

    # Manual expense additions (added ON TOP of auto-calc)
    with conn() as c:
        rows = c.execute(
            """
            select category, sum(amount)::numeric as amount
              from expenses
             where property_id = %s and month = %s
             group by category
            """,
            (property_id, month),
        ).fetchall()

    expense_map = {r["category"]: float(r["amount"]) for r in rows}
    cleaning = round(cleaning_auto + expense_map.get("cleaning", 0), 2)
    linen = round(linen_auto + expense_map.get("linen", 0), 2)
    maintenance = expense_map.get("maintenance", 0)
    other = sum(v for k, v in expense_map.items() if k not in ("cleaning", "linen", "maintenance"))

    mgmt_fee = round(gross * (mgmt_fee_pct / 100), 2)
    net_profit = round(gross - ota - cleaning - linen - maintenance - other - mgmt_fee, 2)

    return dict(
        gross_revenue=round(gross, 2),
        ota_fees=ota,
        cleaning=cleaning,
        linen=linen,
        maintenance=maintenance,
        other_expenses=other,
        mgmt_fee=mgmt_fee,
        mgmt_fee_pct=mgmt_fee_pct,
        net_profit_owner=net_profit,
    )



def comparison_pack(property_id: str, current: date, mgmt_fee_pct: float) -> dict:
    """Bundle this month + prior month + same month last year for the report."""
    from dateutil.relativedelta import relativedelta  # imported here to keep top-level imports light
    prev_month = current - relativedelta(months=1)
    last_year = current - relativedelta(years=1)
    return dict(
        property_id=property_id,
        month=current,
        this=dict(kpis=month_kpis(property_id, current),
                  pnl=pnl(property_id, current, mgmt_fee_pct),
                  alos=alos(property_id, current)),
        prev=dict(kpis=month_kpis(property_id, prev_month),
                  pnl=pnl(property_id, prev_month, mgmt_fee_pct),
                  alos=alos(property_id, prev_month)),
        yoy=dict(kpis=month_kpis(property_id, last_year),
                 pnl=pnl(property_id, last_year, mgmt_fee_pct),
                 alos=alos(property_id, last_year)),
        source_mix=source_mix(property_id, current),
        trend=revenue_trend(property_id, current),
    )
