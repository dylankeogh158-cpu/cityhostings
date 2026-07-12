"""Cloudbeds API wrapper.

Thin layer over Cloudbeds v1.2 endpoints with:
  - Bearer auth from CLOUDBEDS_API_KEY env var
  - Exponential backoff on 5xx errors
  - Generator-based pagination so we don't load 10,000 reservations into memory
"""
from __future__ import annotations

import os
import time
import logging
from typing import Iterator, Optional
import requests

log = logging.getLogger(__name__)

BASE_URL = "https://hotels.cloudbeds.com/api/v1.2"
DEFAULT_PAGE_SIZE = 100
MAX_RETRIES = 3


class CloudbedsError(RuntimeError):
    """Raised when Cloudbeds returns a non-recoverable error."""


class CloudbedsClient:
    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.environ.get("CLOUDBEDS_API_KEY")
        if not key:
            raise CloudbedsError("CLOUDBEDS_API_KEY is not set")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        })

    # ---- internal ----
    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{BASE_URL}/{path.lstrip('/')}"
        backoff = 1.0
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise CloudbedsError(f"Network error after {MAX_RETRIES} retries: {exc}") from exc
                log.warning("Network error %s, retry %d in %.1fs", exc, attempt, backoff)
                time.sleep(backoff)
                backoff *= 4
                continue

            if r.status_code >= 500:
                if attempt == MAX_RETRIES:
                    raise CloudbedsError(f"5xx after {MAX_RETRIES} retries: {r.status_code} {r.text[:200]}")
                log.warning("5xx %s, retry %d in %.1fs", r.status_code, attempt, backoff)
                time.sleep(backoff)
                backoff *= 4
                continue

            if r.status_code == 429:
                # Rate limited — Cloudbeds returns Retry-After in seconds
                wait = int(r.headers.get("Retry-After", 5))
                log.warning("Rate limited, sleeping %ds", wait)
                time.sleep(wait)
                continue

            if r.status_code >= 400:
                raise CloudbedsError(f"{r.status_code} {r.text[:300]}")

            payload = r.json()
            if not payload.get("success", True):
                raise CloudbedsError(f"API said failure: {payload.get('message')}")
            return payload

        raise CloudbedsError("Exhausted retries")

    # ---- public ----
    def get_hotels(self) -> list[dict]:
        """All properties on this account."""
        return self._get("getHotels").get("data", [])

    def get_rooms(self, property_id: str) -> list[dict]:
        """All rooms/units for a property."""
        return self._get("getRooms", {"propertyID": property_id}).get("data", [])

    def get_reservations(
        self,
        property_id: str,
        modified_from: Optional[str] = None,
        check_in_from: Optional[str] = None,
        check_in_to: Optional[str] = None,
    ) -> Iterator[dict]:
        """Stream reservations matching the filters.

        Pass `modified_from` as an ISO datetime string for delta syncs.
        Yields one reservation dict at a time so we don't blow up memory.

        For reservations where the list endpoint doesn't include a total
        (common for OTA "channel collect" bookings like Booking.com/Expedia),
        we fall back to a per-reservation detail call to fetch the real total.
        """
        page_number = 1
        while True:
            params = {
                "propertyID": property_id,
                "pageNumber": page_number,
                "pageSize": DEFAULT_PAGE_SIZE,
                "includeGuestsDetails": "true",
            }
            if modified_from:
                params["modifiedFrom"] = modified_from
            if check_in_from:
                params["checkInFrom"] = check_in_from
            if check_in_to:
                params["checkInTo"] = check_in_to

            payload = self._get("getReservationsWithRateDetails", params)
            rows = payload.get("data", [])
            for row in rows:
                has_total = row.get("grandTotal") or row.get("total") or row.get("totalRevenue") or row.get("roomRevenue")
                if not has_total:
                    try:
                        detail = self.get_reservation_detail(row.get("reservationID"), property_id)
                        if detail.get("total") is not None:
                            row["total"] = detail["total"]
                        bd = detail.get("balanceDetailed") or {}
                        if bd.get("grandTotal") is not None:
                            row.setdefault("grandTotal", bd["grandTotal"])
                    except CloudbedsError as exc:
                        log.warning(
                            "Could not fetch detail for reservation %s: %s",
                            row.get("reservationID"), exc,
                        )
                yield row

            if len(rows) < DEFAULT_PAGE_SIZE:
                return
            page_number += 1

def get_reservation_detail(self, reservation_id: str, property_id: str = None) -> dict:
    params = {"reservationID": reservation_id}
    if property_id:
        params["propertyID"] = property_id
    return self._get("getReservation", params).get("data", {})
