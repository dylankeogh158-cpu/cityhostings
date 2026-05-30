"""Daily sync job.

Runs once a day from GitHub Actions. For each active property in the database:
  1. Refresh the units list
  2. Pull reservations modified since the last successful sync (or last 7 days
     on first run)
  3. Upsert into Postgres
  4. Refresh the materialised KPI views

Exit code 0 = success, 1 = failed. GitHub Actions surfaces this in the UI.
"""
from __future__ import annotations

import sys
import logging
from datetime import datetime, timedelta, timezone

from cityhostings.cloudbeds import CloudbedsClient
from cityhostings.db import (
    fetch_active_properties,
    upsert_units,
    upsert_reservations,
    start_sync_run,
    finish_sync_run,
    last_successful_sync,
    refresh_kpi_views,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sync")


def main() -> int:
    run_id = start_sync_run()
    total_upserts = 0

    try:
        client = CloudbedsClient()
        properties = fetch_active_properties()
        log.info("Syncing %d active properties", len(properties))

        # Delta window: from last successful sync minus 1 hour, or 7 days back on first run
        since = last_successful_sync()
        if since:
            modified_from = (datetime.fromisoformat(since) - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        else:
            modified_from = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        log.info("Pulling reservations modified since %s", modified_from)

        for p in properties:
            cb_id = p["cloudbeds_id"]
            log.info("→ %s (cloudbeds %s)", p["name"], cb_id)

            # Units (idempotent — small list, refresh every run)
            rooms = client.get_rooms(cb_id)
            upsert_units(p["id"], rooms)
            log.info("  units: %d", len(rooms))

            # Reservations
            n = upsert_reservations(p["id"], client.get_reservations(cb_id, modified_from=modified_from))
            total_upserts += n
            log.info("  reservations upserted: %d", n)

        # Refresh aggregates
        log.info("Refreshing KPI views…")
        refresh_kpi_views()

        finish_sync_run(run_id, total_upserts)
        log.info("✓ Sync complete (%d total upserts)", total_upserts)
        return 0

    except Exception as exc:  # noqa: BLE001
        log.exception("Sync failed")
        finish_sync_run(run_id, total_upserts, error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())
