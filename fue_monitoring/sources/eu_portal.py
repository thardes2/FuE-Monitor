"""Connector for the public search API of the EU Funding & Tenders Portal.

The search API (api.tech.ec.europa.eu/search-api) is a shared full-text index over a
huge amount of EU content (calls, news, FAQs, closed procedures, etc.) and doesn't
support a reliably documented filter query. We therefore use plain full-text search
(verified to work) and do the filtering ourselves based on the metadata: only entries
with a 'deadlineDate' are treated as an actual call with a deadline.
"""

from __future__ import annotations

import requests
from dateutil import parser as dateparser

from ..models import FundingCall

SEARCH_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (fueMonitoring)", "Content-Type": "application/json"}


def _fetch_page(text: str, page_number: int, page_size: int) -> dict:
    params = {"apiKey": "SEDIA", "text": text, "pageSize": page_size, "pageNumber": page_number}
    resp = requests.post(SEARCH_URL, params=params, headers=HEADERS, json={}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _first(metadata: dict, *keys: str) -> str:
    for key in keys:
        values = metadata.get(key)
        if values:
            return str(values[0])
    return ""


def _to_funding_call(result: dict) -> FundingCall | None:
    metadata = result.get("metadata", {})
    deadline_raw = _first(metadata, "deadlineDate")
    if not deadline_raw:
        return None  # no call/no deadline -> news, FAQ, etc., not relevant

    try:
        deadline = dateparser.isoparse(deadline_raw).date()
    except (ValueError, TypeError):
        deadline = None

    title = result.get("summary") or _first(metadata, "title")
    programme = _first(metadata, "esST_programmes", "esIN_programDescription", "esST_programAbbreviation")
    rate_raw = _first(metadata, "esIN_euContributionRate")
    funding_rate = f"{rate_raw} %" if rate_raw else ""
    funding_rate_percent = float(rate_raw) if rate_raw else None
    budget = _first(metadata, "esIN_overallBudget", "budget")
    identifier = _first(metadata, "callIdentifier", "identifier")

    return FundingCall(
        source="EU Funding & Tenders Portal",
        title=title,
        programme=programme,
        funding_rate=funding_rate,
        funding_rate_percent=funding_rate_percent,
        deadline=deadline,
        budget=budget,
        link=result.get("url", ""),
        note=f"Call ID: {identifier}" if identifier else "",
    )


def fetch(keywords: list[str], page_size: int = 100, max_pages: int = 3) -> list[FundingCall]:
    seen: dict[str, FundingCall] = {}

    for keyword in keywords:
        for page in range(1, max_pages + 1):
            print(f"  (EU Portal: Searching for '{keyword}' on page {page}...)")
            data = _fetch_page(keyword, page, page_size)
            results = data.get("results", [])
            if not results:
                break

            for raw in results:
                entry = _to_funding_call(raw)
                if entry is None:
                    continue
                entry.matched_keywords = list(dict.fromkeys(entry.matched_keywords + [keyword]))
                if entry.dedup_key in seen:
                    existing = seen[entry.dedup_key]
                    existing.matched_keywords = list(dict.fromkeys(existing.matched_keywords + [keyword]))
                else:
                    seen[entry.dedup_key] = entry

            if len(results) < page_size:
                break

    return list(seen.values())
