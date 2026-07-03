"""Database helpers.

Uses psycopg3 against the Supabase Postgres connection string.
SUPABASE_URL is the project URL (https://xxx.supabase.co) — we derive the
connection string from it together with the service role key.

For Supabase you can also set DATABASE_URL directly in env if you prefer.
"""
from __future__ import annotations

import os
import json
import logging
from contextlib import contextmanager
from typing import Iterable, Optional
import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


def _connection_string() -> str:
    """Build the Postgres connection string.

    Order of preference:
      1. DATABASE_URL env var (full Postgres URL)
      2. SUPABASE_DB_URL env var (alias)
      3. Derived from SUPABASE_URL + SUPABASE_DB_PASSWORD
    """
    url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if url:
        return url

    supa = os.environ.get("SUPABASE_URL")
    pw = os.environ.get("SUPABASE_DB_PASSWORD")
    if not supa or not pw:
        raise RuntimeError(
            "Set DATABASE_URL, or set SUPABASE_URL + SUPABASE_DB_PASSWORD"
        )
    project_ref = supa.replace("https://", "").split(".")[0]
    return f"postgresql://postgres:{pw}@db.{project_ref}.supabase.co:5432/postgres"


@contextmanager
def conn():
    """Yield a psycopg connection."""
    with psycopg.connect(_connection_string(), row_factory=dict_row, prepare_threshold=None) as c:
        yield c


def fetch_active_properties() -> list[dict]:
    with conn() as c:
        return c.execute(
            "select id, cloudbeds_id, name, location, owner_name, owner_email, "
            "currency, mgmt_fee_pct from properties where active = true"
        ).fetchall()


def upsert_units(property_id: str, rooms: list[dict]) -> int:
    """Upsert all unit rows from Cloudbeds getRooms."""
    if not rooms:
        return 0
    with conn() as c:
        with c.cursor() as cur:
            for r in rooms:
                cur.execute(
                    """
                    insert into units (property_id, cloudbeds_room_id, name, max_occupancy, active)
                    values (%s, %s, %s, %s, true)
                    on conflict (cloudbeds_room_id) do update
                      set name = excluded.name,
                          max_occupancy = excluded.max_occupancy
                    """,
                    (
                        property_id,
                        str(r.get("roomID") or r.get("roomTypeID")),
                        r.get("roomName") or r.get("roomTypeName") or "Unit",
                        r.get("maxGuests") or r.get("maxOccupancy"),
                    ),
                )
        c.commit()
        return len(rooms)


def upsert_reservations(property_id: str, reservations: Iterable[dict]) -> int:
    """Upsert reservations. Returns count inserted/updated."""
    count = 0
    with conn() as c:
        with c.cursor() as cur:
            for r in reservations:
                # Look up our internal unit_id from cloudbeds_room_id
                room_id = str(r.get("roomID") or r.get("assigned", [{}])[0].get("roomID") or "")
                unit_id = None
                if room_id:
                    row = cur.execute(
                        "select id from units where cloudbeds_room_id = %s",
                        (room_id,)
                    ).fetchone()
                    if row:
                        unit_id = row["id"]

                check_in = r.get("startDate") or r.get("checkIn")
                check_out = r.get("endDate") or r.get("checkOut")
                if not check_in or not check_out:
                    log.warning(
                        "Skipping reservation %s (status=%s) - missing check-in/out date",
                        r.get("reservationID"), r.get("status"),
                    )
                    continue

                cur.execute(
                    """
                    insert into reservations (
                      cloudbeds_id, property_id, unit_id, source, status,
                      check_in, check_out, guest_name,
                      gross_amount, net_amount, ota_commission, cleaning_fee_charged,
                      currency, booked_at, modified_at, cloudbeds_payload, synced_at
                    ) values (
                      %s, %s, %s, %s, %s,
                      %s, %s, %s,
                      %s, %s, %s, %s,
                      %s, %s, %s, %s::jsonb, now()
                    )
                    on conflict (cloudbeds_id) do update set
                      status = excluded.status,
                      check_in = excluded.check_in,
                      check_out = excluded.check_out,
                      gross_amount = excluded.gross_amount,
                      net_amount = excluded.net_amount,
                      ota_commission = excluded.ota_commission,
                      modified_at = excluded.modified_at,
                      cloudbeds_payload = excluded.cloudbeds_payload,
                      synced_at = now()
                    """,
                    (
                        str(r.get("reservationID")),
                        property_id,
                        unit_id,
                        (r.get("sourceName") or r.get("source") or "direct").lower(),
                        (r.get("status") or "").lower(),
                        check_in,
                        check_out,
                        r.get("guestName") or r.get("firstName", "") + " " + r.get("lastName", ""),
                        r.get("grandTotal") or r.get("totalRevenue") or r.get("roomRevenue") or r.get("total") or 0,
                        r.get("grandTotal") or r.get("total") or r.get("balance") or 0,
                        r.get("commissions", {}).get("total", 0) if isinstance(r.get("commissions"), dict) else 0,
                        r.get("cleaningFee", 0),
                        r.get("currency"),
                        r.get("dateCreated") or r.get("bookedAt"),
                        r.get("dateModified") or r.get("modifiedAt"),
                        json.dumps(r, default=str),
                    ),
                )
                count += 1
        c.commit()
    return count


def start_sync_run() -> str:
    with conn() as c:
        row = c.execute(
            "insert into sync_runs (status) values ('running') returning id"
        ).fetchone()
        c.commit()
        return str(row["id"])


def finish_sync_run(run_id: str, records: int, error: Optional[str] = None) -> None:
    with conn() as c:
        c.execute(
            """
            update sync_runs
               set finished_at = now(),
                   status = %s,
                   records_upserted = %s,
                   error_message = %s
             where id = %s
            """,
            ("failed" if error else "success", records, error, run_id),
        )
        c.commit()


def last_successful_sync() -> Optional[str]:
    """ISO timestamp of the last successful sync, for delta queries."""
    with conn() as c:
        row = c.execute(
            "select max(finished_at) as ts from sync_runs where status = 'success'"
        ).fetchone()
        return row["ts"].isoformat() if row and row["ts"] else None


def refresh_kpi_views() -> None:
    with conn() as c:
        c.execute("select refresh_all_kpis()")
        c.commit()
