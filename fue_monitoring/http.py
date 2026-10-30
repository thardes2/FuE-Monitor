# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Fetching a page, with the patience these government servers occasionally need.

Measured in one run: three ConnectionResetError(54) — twice bmds.bund.de, once
ble.de — each on the *first and only* contact with that host, so our own request
rate wasn't the cause. The cost was out of all proportion: a reset while fetching a
listing page left the source reporting "0 announcements discovered", which reads
exactly like "this ministry has published nothing". A monitor whose failures look
like empty results is worse than one that stops.
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

# Statuses worth another attempt. 429 and 503 are the two a server uses to say
# "later"; 502/504 are a gateway briefly losing its backend.
RETRYABLE_STATUS = {429, 502, 503, 504}

# Longer than the LLM backoff, because a server that asked us to slow down needs
# more than a moment, and a listing page is fetched once per source per run — there
# is no volume here to protect.
BACKOFF_SECONDS = (3, 9, 27)

# A Retry-After this long means "not in this run"; waiting it out would stall
# everything else for nothing.
MAX_RETRY_AFTER_SECONDS = 60


def _retry_after(response) -> float | None:
    """The server's own waiting time, when it states one and it is short enough."""
    raw = response.headers.get("Retry-After", "").strip()
    if not raw.isdigit():
        return None
    seconds = int(raw)
    return float(seconds) if seconds <= MAX_RETRY_AFTER_SECONDS else None


def get(url: str, *, headers: dict | None = None, timeout: int = 30, label: str = ""):
    """GETs a URL, retrying transient failures. Returns the response, or None.

    None means every attempt failed — the caller should treat that as "unknown",
    not as "nothing there".
    """
    where = label or url
    last_error = ""

    for attempt, delay in enumerate((0,) + BACKOFF_SECONDS):
        if delay:
            time.sleep(delay)
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            # Connection reset, DNS hiccup, read timeout — all of them transient
            # often enough to be worth another attempt.
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code not in RETRYABLE_STATUS:
                return response
            last_error = f"HTTP {response.status_code}"
            stated = _retry_after(response)
            if stated is not None and attempt < len(BACKOFF_SECONDS):
                log.warning(f"    ({where}: {last_error}, server asked for {stated:g}s)")
                time.sleep(stated)
                continue

        if attempt < len(BACKOFF_SECONDS):
            log.warning(f"    ({where}: {last_error}, retrying in {BACKOFF_SECONDS[attempt]}s)")

    log.warning(f"    could not fetch {where} after {len(BACKOFF_SECONDS) + 1} attempts: {last_error}")
    return None
