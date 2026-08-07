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
from ..cache import PageCache
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


def _brave_urls(term: str, site: str, link_marker: str, api_key: str, max_results: int, pages: int = 1) -> list[str]:
    """Announcement URLs Brave knows for `term` on `site`, across `pages` result pages.

    Brave caps a page at 20 results and the offset at 9, and only some results on
    any page are announcements, so a single page is a thin sample — that thinness
    is why an announcement can silently drop out of a run when the ranking shifts.
    """
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    urls: list[str] = []

    for offset in range(max(pages, 1)):
        params = {"q": f"site:{site} {term}", "count": min(max_results, 20), "offset": offset}
        resp = requests.get(BRAVE_SEARCH_URL, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"    Brave search failed for '{term}' on {site}: {resp.text[:200]}")
            break

        results = resp.json().get("web", {}).get("results", [])
        urls += [item["url"] for item in results if link_marker in item.get("url", "")]
        if not results:
            break

    return urls


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


def _download_and_extract(url: str, llm_config: dict | None) -> dict | None:
    """The expensive half: one HTTP request plus (optionally) one LLM call.

    Split out from building the FundingCall so the cache can skip exactly this
    part while keyword matching still runs fresh on every run.
    """
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

    return {
        "text": text,
        "title": title,
        "funding_rate": funding_rate,
        "funding_rate_percent": funding_rate_percent,
        "deadline": deadline,
    }


def _build_call(url: str, data: dict, keywords: list[str]) -> FundingCall:
    title = data["title"]
    funding_rate = data["funding_rate"]
    deadline = data["deadline"]
    funding_rate_percent = data["funding_rate_percent"]

    found_keywords = matched_keywords(f"{title} {data['text']}", keywords)

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
    cache: PageCache | None = None,
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
        # Two complementary query sets, because neither alone is complete:
        # generic terms enumerate broadly but rank a niche call out of reach, while
        # the user's keywords surface exactly those niche calls. Verified case: a
        # "KI in Wertschöpfungsketten" call that only the keyword query found.
        for term in list(source_config.get("discovery_terms", [])) + list(keywords):
            for url in _brave_urls(
                term,
                source_config["search_site"],
                link_marker,
                brave_api_key,
                source_config.get("max_results_per_keyword", 20),
                source_config.get("search_pages", 1),
            ):
                urls.setdefault(_canonical(url), None)
    elif discovery == "listing":
        for url in _listing_urls(source_config["listing_url"], link_marker):
            urls.setdefault(_canonical(url), None)
    else:
        raise ValueError(f"Unknown discovery mode '{discovery}' for source '{name}'")

    entries: dict[str, FundingCall] = {}
    downloaded = 0
    for url in urls:
        data = cache.get(url) if cache else None

        if data is None:
            # The crawl delay only has to separate actual requests to the site, so
            # cache hits mustn't be slowed down by it.
            if downloaded > 0:
                time.sleep(crawl_delay)
            data = _download_and_extract(url, llm_config)
            downloaded += 1
            if data is None:
                continue
            if cache:
                cache.put(
                    url,
                    data["text"],
                    data["title"],
                    data["funding_rate"],
                    data["funding_rate_percent"],
                    data["deadline"],
                )

        entry = _build_call(url, data, keywords)
        entry.source = name
        entries[entry.dedup_key] = entry

    print(f"  ({name}: {len(urls)} announcements discovered, {downloaded} newly downloaded, {len(urls) - downloaded} from cache)")
    return list(entries.values())
