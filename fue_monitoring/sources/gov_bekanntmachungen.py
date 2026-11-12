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

Three optional settings make "listing" fit a site without a listing page of its
own, such as bmv.de, whose announcements are only findable through its site search:

- `listing_terms`: the listing URL contains "{term}" and is fetched once per term,
  so a search page can serve as the listing.
- `link_text_include` / `link_text_exclude`: a link is only followed if its text
  contains one of the first and none of the second. Search results mix the calls
  with factsheets, templates and English versions, and each one followed costs
  downloads plus an LLM call.
- `document_pdf`: the discovered page is a wrapper for a PDF attachment, and the
  text comes from that PDF (see pdf_text.is_same_document_pdf).
"""

from __future__ import annotations

import logging
import random
import time
from urllib.parse import quote_plus, urlencode, urljoin

from bs4 import BeautifulSoup

from .. import http, llm_extract, pdf_text
from ..cache import PageCache, SearchCache
from ..dedup import canonical_link
from ..filters import extract_deadline, extract_funding_rate, matched_keywords
from ..models import FundingCall

log = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (fueMonitoring; personal R&D funding monitoring)"}

# Spacing between back-to-back calls to a hosted LLM. Re-extractions aren't spaced
# by the crawl delay the way downloads are, and an unthrottled sweep over a full
# cache tripped 429s after a few dozen requests, while ~4 s apart ran clean.
class ListingUnavailable(Exception):
    """A source's listing page could not be read, so we know nothing about it.

    Raised rather than returning an empty list, because "no announcements" and "we
    could not look" must not reach the run summary as the same thing.
    """


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


def brave_keys(value) -> list[str]:
    """`brave_api_key` from the config as a list of usable keys.

    One key as a string, or any number of them as a list; blanks are dropped, so
    an empty string or an empty list means "not configured".
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"gov_bekanntmachungen.brave_api_key: expected a string or a list, got {type(value).__name__}")
    return [str(key).strip() for key in value if str(key or "").strip()]


# Answers that say "this key is no good", not "try later": invalid key (401),
# no credit left (402, "Prepaid credit balance is insufficient"), not allowed (403).
# Retrying with the same key cannot help, so the key is set aside for the run.
BRAVE_KEY_REJECTED = {401, 402, 403}


def _brave_error(resp, api_keys: list[str]) -> str:
    """Status and Brave's own error text, with any key masked."""
    try:
        detail = resp.json()["error"]["detail"]
    except Exception:  # noqa: BLE001 — not Brave's error JSON: fall back to the raw text
        detail = resp.text[:200]
    for key in api_keys:
        detail = detail.replace(key, "<key>")
    return f"HTTP {resp.status_code}: {detail}"


def _brave_urls(term: str, site: str, link_marker: str, api_keys: list[str], max_results: int,
                pages: int = 1, usable: list[int] | None = None, stats: dict | None = None,
                search_cache: SearchCache | None = None) -> list[str]:
    """Announcement URLs Brave knows for `term` on `site`, across `pages` result pages.

    Brave caps a page at 20 results and the offset at 9, and only some results on
    any page are announcements, so a single page is a thin sample — that thinness
    is why an announcement can silently drop out of a run when the ranking shifts.

    Every request, retries included, goes out with a key drawn at random from the
    usable ones, spreading the load over all of them; a retry after a 429 thus
    usually lands on another key. A key Brave rejects (BRAVE_KEY_REJECTED, e.g. no
    credit left) is removed from `usable` and the same page is asked again with
    another key. `usable` holds positions in `api_keys`; pass the same list for
    every term of a run so a rejected key is dropped once, not drawn again per
    term. Keys are never logged, only their position.

    A search that got answered logs one INFO line (results, announcement links, key),
    so a run shows what worked and not only what failed. `stats`, if given, counts
    answered and failed searches under "ok" and "failed".

    With a `search_cache`, a result page answered within its max age is taken from
    there instead of being bought again; each newly answered page goes into it.
    """
    if usable is None:
        usable = list(range(len(api_keys)))
    chosen: list[int] = []

    def headers() -> dict:
        index = usable[random.randrange(len(usable))]
        chosen.append(index)
        return {"Accept": "application/json", "X-Subscription-Token": api_keys[index]}

    def position() -> str:
        return f" (key {chosen[-1] + 1} of {len(api_keys)})" if chosen and len(api_keys) > 1 else ""

    urls: list[str] = []
    answered = 0  # pages answered with 200, from Brave or from the search cache
    from_cache = 0
    results_seen = 0
    failed = False

    offset = 0
    while offset < max(pages, 1):
        params = {"q": f"site:{site} {term}", "count": min(max_results, 20), "offset": offset}
        cache_key = f"{params['q']}|{params['offset']}|{params['count']}"
        hit = search_cache.get(cache_key) if search_cache else None
        if hit is not None:
            results = hit[0]
            from_cache += 1
            offset += 1
            answered += 1
            results_seen += len(results)
            urls += [item["url"] for item in results if link_marker in item.get("url", "")]
            if not results:
                break
            continue
        if not usable:
            log.warning(f"    Brave search skipped for '{term}' on {site}: every API key was rejected")
            failed = True
            break
        # Brave rate-limits by plan, and a 429 here would otherwise quietly shrink
        # how much of a ministry we discover — the retry honours its Retry-After.
        resp = http.get(
            f"{BRAVE_SEARCH_URL}?{urlencode(params)}",
            headers=headers, timeout=30, label=f"Brave search '{term}' on {site}",
        )
        if resp is not None and resp.status_code in BRAVE_KEY_REJECTED:
            rejected = chosen[-1]
            if rejected in usable:
                usable.remove(rejected)
            log.warning(f"    Brave rejected{position()}, not using it again this run "
                        f"({len(usable)} left): {_brave_error(resp, api_keys)}")
            continue  # same page, another key
        if resp is None or resp.status_code != 200:
            detail = f": {_brave_error(resp, api_keys)}" if resp is not None else ""
            log.warning(f"    Brave search failed for '{term}' on {site}{position()}{detail}")
            failed = True
            break
        offset += 1
        answered += 1

        results = resp.json().get("web", {}).get("results", [])
        if search_cache:
            search_cache.put(cache_key, [{"url": item.get("url", "")} for item in results])
        results_seen += len(results)
        urls += [item["url"] for item in results if link_marker in item.get("url", "")]
        if not results:
            break

    if answered:
        pages_text = f"{answered} page" + ("s" if answered > 1 else "")
        partly = ", then failed" if failed else ""
        if from_cache == answered:
            via = ", from cache"
        elif from_cache:
            via = f", {from_cache} from cache{position()}"
        else:
            via = position()
        log.info(f"    Brave '{term}' on {site}: {results_seen} results on {pages_text}{partly}, "
                 f"{len(urls)} announcement link(s){via}")
    if stats is not None:
        stats["failed" if failed else "ok"] = stats.get("failed" if failed else "ok", 0) + 1
        if answered and from_cache == answered:
            stats["cached"] = stats.get("cached", 0) + 1
    return urls


def _listing_pages(source_config: dict) -> list[str]:
    """The listing URL, or one per term where the listing is a search page."""
    listing_url = source_config["listing_url"]
    terms = source_config.get("listing_terms") or []
    if not terms:
        return [listing_url]
    if "{term}" not in listing_url:
        raise ValueError(
            f"Source '{source_config['name']}' sets listing_terms, but its listing_url "
            "has no {term} placeholder to put them in"
        )
    return [listing_url.replace("{term}", quote_plus(term)) for term in terms]


def _link_text_admitted(text: str, include: list[str], exclude: list[str]) -> bool:
    """Case-insensitive substring test on a link's text. No include list admits all."""
    lowered = text.lower()
    if include and not any(word.lower() in lowered for word in include):
        return False
    return not any(word.lower() in lowered for word in exclude)


def _listing_links(listing_url: str, link_marker: str) -> list[tuple[str, str]]:
    """(url, link text) for every announcement link on a listing page.

    The text is the longest one any anchor to that URL carries: a search result
    often links the same document from a thumbnail with no text at all.
    """
    # A failure here costs the whole source for this run, so it is worth the wait —
    # measured: one reset on the first contact with ble.de reported the ministry as
    # having published nothing at all.
    resp = http.get(listing_url, headers=HEADERS, timeout=30, label=f"listing page {listing_url}")
    if resp is None:
        raise ListingUnavailable(listing_url)
    if resp.status_code != 200:
        raise ListingUnavailable(f"{listing_url} (HTTP {resp.status_code})")

    soup = BeautifulSoup(resp.text, "html.parser")
    # Some of these government CMS pages set a <base href> that changes how relative
    # links resolve (e.g. ble.de uses <base href="https://www.ble.de/">) — without
    # honouring it, urljoin resolves relative to the listing page's own path instead
    # of site root and builds a wrong, duplicated URL.
    base_tag = soup.find("base", href=True)
    base_url = base_tag["href"] if base_tag else listing_url

    main = _main_content(soup)

    # Drop the listing page itself. Where the marker is a path the listing lives
    # under — bmds.bund.de's "/service/bekanntmachungen/" is both — its own
    # in-page anchors ("#", "#top") reduce to the listing URL and it would be
    # fetched, LLM-evaluated and reported as an announcement of its own, carrying
    # every real announcement's teaser text as its content.
    own = _canonical(listing_url)
    links: dict[str, str] = {}
    for anchor in main.find_all("a", href=True):
        if link_marker not in anchor["href"]:
            continue
        url = urljoin(base_url, anchor["href"])
        if _canonical(url) == own:
            continue
        text = anchor.get_text(" ", strip=True)
        if len(text) > len(links.get(url, "")):
            links[url] = text
        else:
            links.setdefault(url, text)
    return list(links.items())


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
    submission_mode = ""
    if llm_config:
        llm_result = llm_extract.extract(text, title, llm_config)
        llm_ok = llm_result is not None
        (
            title, funding_rate, funding_rate_percent, deadline, deadlines, submission_mode
        ) = llm_extract.merge(llm_result, title, funding_rate, funding_rate_percent, deadline)

    return {
        "text": text,
        "title": title,
        "funding_rate": funding_rate,
        "funding_rate_percent": funding_rate_percent,
        "deadline": deadline,
        "deadlines": deadlines,
        "submission_mode": submission_mode,
        "llm_ok": llm_ok,
    }


def _attached_pdf_text(url: str, main, base_url: str, delay: float) -> str | None:
    """The text of the PDF a wrapper page at `url` is the download page for, or None.

    Only the PDF with the wrapper's own file stem is taken, never "the first PDF on
    the page": a call's page also links forms and English versions.
    """
    target = next(
        (
            urljoin(base_url, anchor["href"])
            for anchor in main.find_all("a", href=True)
            if pdf_text.is_same_document_pdf(url, urljoin(base_url, anchor["href"]))
        ),
        None,
    )
    if target is None:
        log.warning(f"    (no PDF download on wrapper page {url}, skipped)")
        return None

    # A second request to the same site, so spaced like any other.
    time.sleep(delay)
    resp = http.get(target, headers=HEADERS, timeout=60)
    if resp is None or resp.status_code != 200:
        return None
    if not pdf_text.looks_like_pdf(resp.content, resp.headers.get("content-type", "")):
        log.warning(f"    ({target} is not a PDF, skipped)")
        return None
    return pdf_text.extract_text(resp.content)


def _download_and_extract(
    url: str, llm_config: dict | None, document_pdf: bool = False, delay: float = 0
) -> dict | None:
    """Fetches the page — and, for a PDF wrapper, its PDF — then extracts the fields.

    With document_pdf, a wrapper whose PDF can't be read yields None rather than
    falling back to the wrapper's own text: that text is the title and a download
    button, and an entry built on it would read "deadline not detected" for a call
    that has one. None is not cached, so the next run tries again.
    """
    resp = http.get(url, headers=HEADERS, timeout=30)
    if resp is None:
        return None  # http.get already logged that it gave up
    if resp.status_code != 200:
        log.warning(f"    ({url}: HTTP {resp.status_code})")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    main = _main_content(soup)
    h1 = main.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else url)

    if document_pdf:
        base_tag = soup.find("base", href=True)
        text = _attached_pdf_text(url, main, base_tag["href"] if base_tag else url, delay)
        if not text:
            return None
    else:
        text = main.get_text(" ", strip=True)

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
        submission_mode=data.get("submission_mode") or "",
        link=url,
        matched_keywords=found_keywords,
        note="; ".join(notes) + (" -> please check manually" if notes else ""),
    )


def fetch(
    source_config: dict,
    keywords: list[str],
    brave_api_key: str | list[str],
    llm_config: dict | None,
    cache: PageCache | None = None,
    search_cache: SearchCache | None = None,
) -> list[FundingCall]:
    name = source_config["name"]
    discovery = source_config["discovery"]
    link_marker = source_config["link_path_marker"]
    crawl_delay = source_config.get("crawl_delay", 5)

    urls: dict[str, str] = {}
    if discovery == "brave":
        api_keys = brave_keys(brave_api_key)
        if not api_keys:
            log.warning(f"  ({name}: brave_api_key not set, skipping this source)")
            return []
        # Two complementary query sets, because neither alone is complete:
        # generic terms enumerate broadly but rank a niche call out of reach, while
        # the user's keywords surface exactly those niche calls. Verified case: a
        # "KI in Wertschöpfungsketten" call that only the keyword query found.
        
        # Shared by all terms: a key Brave rejects (e.g. out of credit) is dropped
        # once for the whole source instead of failing a search per term.
        usable = list(range(len(api_keys)))
        stats: dict = {}
        for term in list(source_config.get("discovery_terms", [])) + list(keywords):
            for url in _brave_urls(
                term,
                source_config["search_site"],
                link_marker,
                api_keys,
                source_config.get("max_results_per_keyword", 20),
                source_config.get("search_pages", 1),
                usable,
                stats,
                search_cache,
            ):
                _remember_url(urls, url)
        rejected = len(api_keys) - len(usable)
        log.info(
            f"  ({name}: Brave answered {stats.get('ok', 0)} of "
            f"{stats.get('ok', 0) + stats.get('failed', 0)} searches"
            + (f" ({stats['cached']} from cache, not billed)" if stats.get("cached") else "")
            + f", {len(urls)} distinct announcement URLs"
            + (f"; {rejected} of {len(api_keys)} keys rejected" if rejected else "") + ")"
        )
    elif discovery == "listing":
        include = source_config.get("link_text_include") or []
        exclude = source_config.get("link_text_exclude") or []
        left_out: dict[str, str] = {}
        for page_number, listing_page in enumerate(_listing_pages(source_config)):
            if page_number:
                time.sleep(crawl_delay)
            # One unreadable search term raises like an unreadable listing: the
            # source's results would be incomplete without saying so otherwise.
            for url, text in _listing_links(listing_page, link_marker):
                if _link_text_admitted(text, include, exclude):
                    _remember_url(urls, url)
                else:
                    # Keyed per document, not per URL: a search result links both a
                    # wrapper page and its PDF ("Download"), which are one document.
                    key = canonical_link(url)
                    if len(text) > len(left_out.get(key, "")):
                        left_out[key] = text
        # Named, not just counted, so a filter that starts eating real calls
        # (after a ministry renames a document type) gets noticed.
        admitted = {canonical_link(u) for u in urls.values()}
        left_out = {k: t for k, t in left_out.items() if k not in admitted}
        if left_out:
            log.info(f"  ({name}: {len(left_out)} link(s) left out by link_text_include/exclude)")
            for text in left_out.values():
                log.debug(f"    left out: {text or '(no link text)'}")
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
                submission_mode=data.get("submission_mode") or "",
            )

    model = llm_extract.extraction_id(llm_config)
    entries: dict[str, FundingCall] = {}
    downloaded = re_extracted = 0
    log.info(f"Got {len(urls)} URLs to scan")
    for url in urls.values():
        log.info(f"Checking {url}...")
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
            data = _download_and_extract(
                url, llm_config,
                document_pdf=bool(source_config.get("document_pdf")), delay=crawl_delay,
            )
            downloaded += 1
            if data is None:
                log.warning("    -> skipped in this run; not cached, so the next run tries it again")
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
    log.info(summary + ")")
    return list(entries.values())
