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

# Spacing between back-to-back calls to a hosted LLM. Re-extractions aren't spaced
# by the crawl delay the way downloads are, and an unthrottled sweep over a full
# cache tripped 429s after a few dozen requests, while ~4 s apart ran clean.
HOSTED_LLM_DELAY_SECONDS = 4
# How often a cached page may be re-extracted unsuccessfully before it is left
# alone. Its recorded model otherwise never catches up with the configured one,
# so it would queue for re-extraction on every future run.
MAX_REEXTRACT_ATTEMPTS = 3


def _main_content(soup: BeautifulSoup):
    return soup.find("main") or soup.find(id="content") or soup


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]


def _canonical(url: str) -> str:
    """Dedup key for an announcement, ignoring the ways the same page gets addressed.

    These government CMSs serve one announcement under several URLs: extra query
    parameters ("...foo.html?view=renderNewsletterHtml", "?nn=123") and, as search
    results show, both with and without the ".html" suffix. Each variant would
    otherwise be fetched, LLM-evaluated and reported as its own call — observed on
    the "KI in Wertschöpfungsketten" announcement, which appeared twice in a
    profile report with differing funding rates.
    """
    base = _strip_query(url)
    return base[:-5] if base.endswith(".html") else base


def _remember_url(urls: dict[str, str], url: str) -> None:
    """Records one URL per announcement, preferring the ".html" spelling.

    Both spellings resolve, but the suffixed one is the canonical page on these
    CMSs, so that's the one worth fetching when both turn up.
    """
    key = _canonical(url)
    stripped = _strip_query(url)
    if key not in urls or (stripped.endswith(".html") and not urls[key].endswith(".html")):
        urls[key] = stripped


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


def _extract_fields(text: str, title: str, url: str, llm_config: dict | None) -> dict:
    """Derives the fields from an announcement's text — no HTTP involved.

    Separate from downloading so a cached page whose extraction came from a
    different model can be re-derived from the stored text, instead of being
    re-crawled at 30 s a page for output that doesn't depend on the network.
    """
    # No blind fallbacks when an LLM is available: these pages routinely print
    # percentages and dates that aren't the funding rate or the deadline
    # (subcontracting caps, publication dates, programme end dates), and a model
    # reading the whole document decides that far better than "first match wins".
    funding_rate, funding_rate_percent = extract_funding_rate(text, fallback_to_any_percentage=not llm_config)
    deadline = extract_deadline(text, fallback_to_any_date=False)

    # Tracked so the caller can refuse to cache a degraded extraction: when the
    # backend is unreachable, merge() keeps the regex result, which misses written
    # dates like "30. April 2026" entirely. Caching that None would persist a
    # transient outage for cache.max_age_days — and a call whose deadline is
    # unknown passes the open_deadlines_only filter, so an expired call turns up
    # in a company report as "Frist unbekannt". Observed in practice.
    llm_ok = True
    deadlines: list = []
    if llm_config:
        llm_result = llm_extract.extract(text, title, llm_config)
        llm_ok = llm_result is not None
        title, funding_rate, funding_rate_percent, deadline, deadlines = llm_extract.merge(
            llm_result, title, funding_rate, funding_rate_percent, deadline
        )

    return {
        "text": text,
        "title": title,
        "funding_rate": funding_rate,
        "funding_rate_percent": funding_rate_percent,
        "deadline": deadline,
        "deadlines": deadlines,
        "llm_ok": llm_ok,
    }


def _download_and_extract(url: str, llm_config: dict | None) -> dict | None:
    """One HTTP request, then extraction over what came back."""
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
    return _extract_fields(text, title, url, llm_config)


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
        text=data["text"],
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

    urls: dict[str, str] = {}
    if discovery == "brave":
        if not brave_api_key:
            print(f"  ({name}: brave_api_key not set, skipping this source)")
            return []
        # Two complementary query sets, because neither alone is complete:
        # generic terms enumerate broadly but rank a niche call out of reach, while
        # the user's keywords surface exactly those niche calls. Verified case: a
        # "KI in Wertschöpfungsketten" call that only the keyword query found.
        
        for term in list(source_config.get("discovery_terms", [])) + list(keywords):
            print(f" Scanning {term}")
            for url in _brave_urls(
                term,
                source_config["search_site"],
                link_marker,
                brave_api_key,
                source_config.get("max_results_per_keyword", 20),
                source_config.get("search_pages", 1),
            ):
                _remember_url(urls, url)
    elif discovery == "listing":
        for url in _listing_urls(source_config["listing_url"], link_marker):
            _remember_url(urls, url)
    else:
        raise ValueError(f"Unknown discovery mode '{discovery}' for source '{name}'")

    def remember(data: dict) -> None:
        if cache and data["llm_ok"]:
            cache.put(
                url,
                data["text"],
                data["title"],
                data["funding_rate"],
                data["funding_rate_percent"],
                data["deadline"],
                model=model,
                deadlines=data.get("deadlines") or [],
            )

    model = llm_extract.extraction_id(llm_config)
    entries: dict[str, FundingCall] = {}
    downloaded = re_extracted = 0
    print(f"Got {len(urls)} URLs to scan")
    for url in urls.values():
        print(f"Checking {url}...")
        data = cache.get(url) if cache else None

        if (
            data is not None
            and data.get("llm_model") != model
            and data.get("llm_failures", 0) < MAX_REEXTRACT_ATTEMPTS
        ):
            # Cached by a different model. The stored text is still good — only the
            # derived fields are suspect — so re-derive from it rather than paying
            # the site's Crawl-delay again for a page we already have.
            # Throttled: unlike downloads these aren't spaced by the crawl delay,
            # and a hosted backend answers fast enough that an unthrottled sweep
            # over a full cache runs straight into per-minute rate limits.
            if re_extracted and llm_config and llm_config.get("backend") != "ollama":
                time.sleep(HOSTED_LLM_DELAY_SECONDS)
            fresh = _extract_fields(data["text"], data["title"], url, llm_config)
            re_extracted += 1
            if fresh["llm_ok"]:
                data = fresh
                remember(data)
            elif cache:
                # Keep the previous fields rather than the regex-only ones, and
                # count the attempt so a persistently failing page stops being
                # retried on every run.
                cache.note_failure(url)

        if data is None:
            # The crawl delay only has to separate actual requests to the site, so
            # cache hits mustn't be slowed down by it.
            if downloaded > 0:
                time.sleep(crawl_delay)
            data = _download_and_extract(url, llm_config)
            downloaded += 1
            if data is None:
                continue
            remember(data)

        entry = _build_call(url, data, keywords)
        entry.source = name
        entries[entry.dedup_key] = entry

    summary = (
        f"  ({name}: {len(urls)} announcements discovered, {downloaded} newly downloaded, "
        f"{len(urls) - downloaded} from cache"
    )
    if re_extracted:
        summary += f", {re_extracted} re-extracted for model '{model}' without re-crawling"
    print(summary + ")")
    return list(entries.values())
