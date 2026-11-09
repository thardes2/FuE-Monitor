# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Connector for the public search API of the EU Funding & Tenders Portal.

The search API (api.tech.ec.europa.eu/search-api) is a shared full-text index over a
huge amount of EU content (calls, news, FAQs, closed procedures, etc.) and doesn't
support a reliably documented filter query. We therefore use plain full-text search
(verified to work) and do the filtering ourselves based on the metadata: only entries
with a 'deadlineDate' are treated as an actual call with a deadline.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .. import http
from ..models import FundingCall

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (fueMonitoring)", "Content-Type": "application/json"}


class SourceUnavailable(Exception):
    """A search request failed for a reason other than the response size."""


class ResponseTooLarge(Exception):
    """The API refused a page because the records on it are too large together.

    Measured on 09.11.2026 with "Beamforming" (149 hits): pageSize 100 failed on
    page 2, 50 on page 3, 25 on page 6, 10 on none. The API answers HTTP 400 with
    {"type": "businessError", "message": "Result size limit 100mb has been reached,
    try decrease page size."}. A few records carry huge metadata; any page holding
    them is refused, so the remedy is to ask for the same positions in smaller pages.
    """


def _is_size_limit(response) -> bool:
    if response.status_code != 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "size limit" in str(body.get("message", "")).lower()


def _fetch_page(text: str, page_number: int, page_size: int) -> dict:
    params = {"apiKey": "SEDIA", "text": text, "pageSize": page_size, "pageNumber": page_number}
    resp = http.post(SEARCH_URL, params=params, headers=HEADERS, json={}, timeout=30,
                     label=f"EU Portal '{text}' page {page_number}")
    if resp is None:
        raise SourceUnavailable("not reachable")
    if _is_size_limit(resp):
        raise ResponseTooLarge()
    if resp.status_code != 200:
        raise SourceUnavailable(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceUnavailable("no JSON in the response") from exc


def _smaller_size(size: int) -> int:
    """The largest divisor of `size` below it. Positions that start a page of `size`
    then start whole pages of the smaller size too, so the API's page numbers still
    line up with the positions we want."""
    for candidate in range(size // 2, 0, -1):
        if size % candidate == 0:
            return candidate
    return 1


def _fetch_range(text: str, offset: int, size: int, skipped: list[int]) -> tuple[list[dict], bool]:
    """The results at positions offset .. offset+size-1 (0-based; offset is a multiple
    of size). The flag says the result list ended within this range.

    A range refused as too large is split into smaller pages. A single record that
    is refused even on its own is skipped and its position (1-based) noted in
    `skipped`, so the caller can report the result as incomplete.
    """
    try:
        data = _fetch_page(text, offset // size + 1, size)
    except ResponseTooLarge:
        if size == 1:
            log.warning(f"    (EU Portal: '{text}' result {offset + 1} is too large even on its own, skipped)")
            skipped.append(offset + 1)
            return [], False
        step = _smaller_size(size)
        log.info(f"    (EU Portal: '{text}' results {offset + 1}-{offset + size} too large together, "
                 f"retrying in pages of {step})")
        results: list[dict] = []
        for sub_offset in range(offset, offset + size, step):
            part, ended = _fetch_range(text, sub_offset, step, skipped)
            results += part
            if ended:
                return results, True
        return results, False

    results = data.get("results", [])
    return results, len(results) < size


def _first(metadata: dict, *keys: str) -> str:
    for key in keys:
        values = metadata.get(key)
        if values:
            return str(values[0])
    return ""


def _budget_from_overview(metadata: dict, identifier: str) -> str:
    """Open Horizon Europe "topics" don't carry a flat esIN_overallBudget; the actual
    per-topic budget is nested in the budgetOverview JSON blob, keyed by an internal
    topic ID rather than the public identifier, so we find our topic by matching the
    identifier against each action's description text instead."""
    raw = _first(metadata, "budgetOverview")
    if not raw or not identifier:
        return ""

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""

    for actions in data.get("budgetTopicActionMap", {}).values():
        for action in actions:
            if identifier in action.get("action", ""):
                max_contribution = action.get("maxContribution")
                expected_grants = action.get("expectedGrants")
                if max_contribution:
                    text = f"{max_contribution} EUR"
                    if expected_grants:
                        text += f" ({expected_grants} erwartete Förderungen)"
                    return text
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
    identifier = _first(metadata, "identifier", "callIdentifier")
    # Open Horizon Europe "topics" (the majority of relevant results) don't expose
    # esST_programmes/esIN_euContributionRate at all — those only show up on a
    # different record type (awarded/ended projects). typesOfAction is what's
    # actually populated for topics. A flat funding-rate percentage often plain
    # doesn't exist for topics (lump-sum or "up to 100%" grants), so funding_rate
    # stays blank rather than guessing — that's accurate, not a gap.
    programme = _first(metadata, "typesOfAction", "esST_programmes", "esIN_programDescription", "esST_programAbbreviation")
    rate_raw = _first(metadata, "esIN_euContributionRate")
    funding_rate = f"{rate_raw} %" if rate_raw else ""
    funding_rate_percent = float(rate_raw) if rate_raw else None
    budget = _budget_from_overview(metadata, identifier) or _first(metadata, "esIN_overallBudget", "budget")

    # Topics carry their description as an HTML blob in descriptionByte, older
    # procurement records as plain text in description. Profile matching needs
    # this: without it the EU entries — the bulk of every run — would be matched
    # and quoted on their title alone.
    description = _first(metadata, "descriptionByte", "description")
    text = BeautifulSoup(description, "html.parser").get_text(" ", strip=True) if description else ""

    return FundingCall(
        source="EU Funding & Tenders Portal",
        text=f"{title} {text}".strip(),
        title=title,
        programme=programme,
        funding_rate=funding_rate,
        funding_rate_percent=funding_rate_percent,
        deadline=deadline,
        budget=budget,
        link=result.get("url", ""),
        note=f"Call ID: {identifier}" if identifier else "",
    )


@dataclass
class FetchResult:
    calls: list[FundingCall] = field(default_factory=list)
    # One line per keyword whose results are incomplete. Not empty means: calls
    # missing from `calls` may still exist, the run must not read them as gone.
    problems: list[str] = field(default_factory=list)


def fetch(keywords: list[str], page_size: int = 100, max_pages: int = 3) -> FetchResult:
    """Searches the portal for every keyword. A keyword that fails is reported in
    `problems` and the search goes on with the next one: one bad keyword must not
    cost the whole run, nor pass as "nothing found"."""
    seen: dict[str, FundingCall] = {}
    problems: list[str] = []

    for keyword in keywords:
        skipped: list[int] = []
        try:
            for page in range(1, max_pages + 1):
                log.info(f"  (EU Portal: Searching for '{keyword}' on page {page}...)")
                results, ended = _fetch_range(keyword, (page - 1) * page_size, page_size, skipped)

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

                if ended:
                    break
        except SourceUnavailable as exc:
            log.warning(f"    (EU Portal: search for '{keyword}' failed: {exc}; continuing with the next keyword)")
            problems.append(f"'{keyword}': {exc}")
        if skipped:
            problems.append(f"'{keyword}': result(s) {', '.join(map(str, skipped))} too large, skipped")

    return FetchResult(calls=list(seen.values()), problems=problems)
