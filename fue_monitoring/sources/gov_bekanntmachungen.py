# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Generic connector for German government funding-call announcement pages.

Each configured source (see config.yaml -> gov_bekanntmachungen.sources) uses one
of two discovery modes, chosen per-site based on what its own robots.txt allows:

- "listing": the site's own announcement listing page is not disallowed by
  robots.txt (verified individually per source before adding it here), so we
  crawl that listing page directly to find links to individual announcements.
- "brave": the site's listing/search page IS disallowed (like bmftr.bund.de's
  /SiteGlobals/ path), but individual announcement pages are allowed. We use the
  Brave Search API (site: query) to discover announcement URLs without ever
  crawling the disallowed page ourselves, then fetch each page directly.

Either way, each discovered announcement page is fetched directly and its main
content text runs through the same regex + optional Gemini extraction used
elsewhere in this project.
"""

from __future__ import annotations

import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .. import llm_extract
from ..filters import extract_deadline, extract_funding_rate, matched_keywords
from ..models import FundingCall

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (fueMonitoring; personal R&D funding monitoring)"}


def _main_content(soup: BeautifulSoup):
    return soup.find("main") or soup.find(id="content") or soup


def _canonical(url: str) -> str:
    """Drops the query string so render/session variants of the same announcement
    collapse into one entry.

    These government CMSs serve the identical announcement under extra query
    parameters (e.g. "...foo.html?view=renderNewsletterHtml", "...foo.html?nn=123"),
    which would otherwise be fetched and reported twice. The path alone identifies
    the announcement on every source configured here.
    """
    return url.split("?", 1)[0].split("#", 1)[0]


def _brave_urls(keyword: str, site: str, link_marker: str, api_key: str, max_results: int) -> list[str]:
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    params = {"q": f"site:{site} {keyword}", "count": min(max_results, 20)}
    resp = requests.get(BRAVE_SEARCH_URL, headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"    Brave search failed for '{keyword}' on {site}: {resp.text[:200]}")
        return []

    data = resp.json()
    return [
        item["url"]
        for item in data.get("web", {}).get("results", [])
        if link_marker in item.get("url", "")
    ]


def _listing_urls(listing_url: str, link_marker: str) -> list[str]:
    try:
        resp = requests.get(listing_url, headers=HEADERS, timeout=30)
    except requests.exceptions.RequestException as exc:
        print(f"    could not fetch listing page {listing_url}: {exc}")
        return []
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    # Some of these government CMS pages set a <base href> that changes how relative
    # links resolve (e.g. ble.de uses <base href="https://www.ble.de/">) — without
    # honouring it, urljoin resolves relative to the listing page's own path instead
    # of site root and builds a wrong, duplicated URL.
    base_tag = soup.find("base", href=True)
    base_url = base_tag["href"] if base_tag else listing_url

    main = _main_content(soup)
    urls = [urljoin(base_url, a["href"]) for a in main.find_all("a", href=True) if link_marker in a["href"]]
    return list(dict.fromkeys(urls))


def _fetch_announcement(url: str, keywords: list[str], llm_config: dict | None) -> FundingCall | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
    except requests.exceptions.RequestException as exc:
        print(f"    could not fetch {url}: {exc}")
        return None
    if resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    main = _main_content(soup)
    text = main.get_text(" ", strip=True)

    h1 = main.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else url)

    # No blind fallbacks when an LLM is available: these pages routinely print
    # percentages and dates that aren't the funding rate or the deadline
    # (subcontracting caps, publication dates, programme end dates), and a model
    # reading the whole document decides that far better than "first match wins".
    funding_rate, funding_rate_percent = extract_funding_rate(text, fallback_to_any_percentage=not llm_config)
    deadline = extract_deadline(text, fallback_to_any_date=False)

    if llm_config:
        llm_result = llm_extract.extract(text, title, llm_config)
        title, funding_rate, funding_rate_percent, deadline = llm_extract.merge(
            llm_result, title, funding_rate, funding_rate_percent, deadline
        )

    found_keywords = matched_keywords(f"{title} {text}", keywords)

    notes = []
    if not funding_rate:
        notes.append("funding rate not detected")
    if not deadline:
        notes.append("deadline not detected")

    return FundingCall(
        source="",  # filled in by fetch() with the configured source name
        title=title,
        funding_rate=funding_rate,
        funding_rate_percent=funding_rate_percent,
        deadline=deadline,
        link=url,
        matched_keywords=found_keywords,
        note="; ".join(notes) + (" -> please check manually" if notes else ""),
    )


def fetch(
    source_config: dict,
    keywords: list[str],
    brave_api_key: str,
    llm_config: dict | None,
) -> list[FundingCall]:
    name = source_config["name"]
    discovery = source_config["discovery"]
    link_marker = source_config["link_path_marker"]
    crawl_delay = source_config.get("crawl_delay", 5)

    urls: dict[str, None] = {}
    if discovery == "brave":
        if not brave_api_key:
            print(f"  ({name}: brave_api_key not set, skipping this source)")
            return []
        for keyword in keywords:
            for url in _brave_urls(
                keyword,
                source_config["search_site"],
                link_marker,
                brave_api_key,
                source_config.get("max_results_per_keyword", 10),
            ):
                urls.setdefault(_canonical(url), None)
    elif discovery == "listing":
        for url in _listing_urls(source_config["listing_url"], link_marker):
            urls.setdefault(_canonical(url), None)
    else:
        raise ValueError(f"Unknown discovery mode '{discovery}' for source '{name}'")

    entries: dict[str, FundingCall] = {}
    for i, url in enumerate(urls):
        if i > 0:
            time.sleep(crawl_delay)
        entry = _fetch_announcement(url, keywords, llm_config)
        if entry is not None:
            entry.source = name
            entries[entry.dedup_key] = entry

    return list(entries.values())
