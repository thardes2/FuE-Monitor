"""Connector for BMFTR/BMBF "Bekanntmachungen" (German federal R&D call announcements).

bmftr.bund.de's own listing/search page lives under /SiteGlobals/, which its
robots.txt disallows for automated crawling. Individual call pages (under
/SharedDocs/Bekanntmachungen/) are NOT disallowed, so we're free to fetch those
directly. To discover current call URLs without crawling the disallowed search
page ourselves, we use the Brave Search API (site-restricted via a "site:" query)
— this only returns pages Brave's own crawler already indexed.

(We originally used Google's Custom Search API for this, but Google has closed
that API to new projects/customers, so it no longer works for new setups.)

Respects the site's "Crawl-delay: 30" by waiting between requests to bmftr.bund.de.
"""

from __future__ import annotations

import time

import requests
from bs4 import BeautifulSoup

from .. import llm_extract
from ..filters import extract_deadline, extract_funding_rate, matched_keywords
from ..models import FundingCall

SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
TARGET_SITE = "bmftr.bund.de"
CALL_PATH_MARKER = "/SharedDocs/Bekanntmachungen/"
CRAWL_DELAY_SECONDS = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (fueMonitoring; personal R&D funding monitoring)"}


def _search_call_urls(keyword: str, api_key: str, max_results: int) -> list[str]:
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    print(f"  (BMFTR-Bekanntmachungen: Searching for '{keyword}'...)")
    params = {"q": f"site:{TARGET_SITE} {keyword}", "count": min(max_results, 20)}
    resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        reason = resp.text[:200]
        print(f"  (BMFTR-Bekanntmachungen: Brave search failed for '{keyword}': {reason})")
        return []

    data = resp.json()
    return [
        item["url"]
        for item in data.get("web", {}).get("results", [])
        if CALL_PATH_MARKER in item.get("url", "")
    ]


def _fetch_call(url: str, keywords: list[str], gemini_api_key: str, gemini_model: str) -> FundingCall | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
    except requests.exceptions.RequestException as exc:
        print(f"  (BMFTR-Bekanntmachungen: could not fetch {url}: {exc})")
        return None

    if resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    main = soup.find("main") or soup
    text = main.get_text(" ", strip=True)

    h1 = main.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else url)

    funding_rate, funding_rate_percent = extract_funding_rate(text)
    # No blind fallback here: these pages print a publication date right at the
    # top, which the generic "first date in text" fallback would misread as a deadline.
    deadline = extract_deadline(text, fallback_to_any_date=False)

    if gemini_api_key:
        llm_result = llm_extract.extract(text, title, gemini_api_key, gemini_model)
        title, funding_rate, funding_rate_percent, deadline = llm_extract.merge(
            llm_result, title, funding_rate, funding_rate_percent, deadline
        )

    found_keywords = matched_keywords(f"{title} {text}", keywords)

    notes = []
    if not funding_rate:
        notes.append("funding rate not detected")
    if not deadline:
        pdf_link = next((a["href"] for a in main.find_all("a", href=True) if a["href"].lower().endswith(".pdf")), None)
        notes.append(f"deadline not detected, see PDF: {pdf_link}" if pdf_link else "deadline not detected")

    return FundingCall(
        source="BMFTR/BMBF Bekanntmachungen",
        title=title,
        funding_rate=funding_rate,
        funding_rate_percent=funding_rate_percent,
        deadline=deadline,
        link=url,
        matched_keywords=found_keywords,
        note="; ".join(notes) + (" -> please check manually" if notes else ""),
    )


def fetch(
    keywords: list[str],
    api_key: str,
    max_results_per_keyword: int = 10,
    gemini_api_key: str = "",
    gemini_model: str = "",
) -> list[FundingCall]:
    if not api_key:
        print("  (BMFTR-Bekanntmachungen: brave_api_key not set, skipping this source)")
        return []

    urls: dict[str, None] = {}
    for keyword in keywords:
        for url in _search_call_urls(keyword, api_key, max_results_per_keyword):
            urls.setdefault(url, None)

    entries: dict[str, FundingCall] = {}
    for i, url in enumerate(urls):
        if i > 0:
            time.sleep(CRAWL_DELAY_SECONDS)  # respect bmftr.bund.de's robots.txt crawl-delay
        entry = _fetch_call(url, keywords, gemini_api_key, gemini_model)
        if entry is not None:
            entries[entry.dedup_key] = entry

    return list(entries.values())
