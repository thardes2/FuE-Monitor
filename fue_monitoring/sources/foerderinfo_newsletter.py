# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Announcements from the Förderinfo newsletter of the Förderberatung des Bundes.

This exists to reach the ministries no connector can crawl. BMWE's own site
(bundeswirtschaftsministerium.de) sits behind a Radware bot-manager challenge, and
BMI publishes no announcement listing at all — but both appear in this newsletter,
which the Förderberatung „Forschung und Innovation" des Bundes publishes roughly
fortnightly and, crucially, also puts on the web under a path robots.txt allows.

Deliberately NOT used, although it would be more convenient:
  - the newsletter as e-mail. The web issues carry the same content, so a mailbox,
    IMAP credentials and MIME parsing would buy nothing.
  - foerderinfo's RSS feeds (`.../services/rss/bekanntmachungen-alle/...`) and its
    HTML announcement lists (`/SiteGlobals/Forms/...`). Both would be a better
    interface, and both sit under paths this site's robots.txt disallows. A site
    offering a feed for subscription doesn't lift its own Disallow.

What an entry gives us is a short teaser, not a Förderrichtlinie: title, the
submission window, the ministry, and two sentences. The deadline comes from the
markup's own ISO `datetime` attribute, so it needs neither regex nor an LLM — but
there is no funding rate to be had, and profile matching has to judge these on far
less text than a full announcement.
"""

from __future__ import annotations

import logging
import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .. import http, llm_extract, pdf_text
from ..filters import extract_deadline, extract_funding_rate, matched_keywords
from ..models import FundingCall

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (fueMonitoring; personal R&D funding monitoring)"}

ISSUE_MARKER = "/shareddocs/newsletter/"

# Which teaser types are a funding opportunity. Measured over three issues (40
# teasers): "Bekanntmachung" (18) and "Förderaufruf" (6) carry the calls,
# "Ausschreibung" (1) an international one. "Termin" (11) is events and prizes, and
# the public-procurement teasers carry no type at all. Filtering on the type rather
# than the newsletter's section headings, because the same type means the same thing
# wherever it appears, while sections get renamed.
FUNDING_KINDS = frozenset({"Bekanntmachung", "Förderaufruf", "Ausschreibung"})


@dataclass
class Entry:
    """One teaser, as stored. Kept as plain data so it survives in the state file."""

    title: str
    link: str
    ministry: str
    kind: str
    summary: str
    deadline: str | None  # ISO, straight from the markup's datetime attribute

    def as_dict(self) -> dict:
        return dict(title=self.title, link=self.link, ministry=self.ministry,
                    kind=self.kind, summary=self.summary, deadline=self.deadline)


class IssueStore:
    """Remembers which issues were read, and what was in them.

    Needed because the archive page lists only the last three issues (~6 weeks).
    Without remembering, an announcement would silently drop out of the reports as
    soon as its issue rotated off that page, even with its deadline months away.
    Storing the parsed entries rather than just the URLs also means a known issue is
    never fetched twice — each fetch costs the site's 30 s crawl delay.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._issues: dict[str, dict] = {}
        self._dirty = False
        if self.path.exists():
            try:
                self._issues = json.loads(self.path.read_text(encoding="utf-8")).get("issues", {})
            except (json.JSONDecodeError, OSError) as exc:
                log.warning(f"  (newsletter state at {self.path} unreadable, starting empty: {exc})")

    def known(self, issue_url: str) -> bool:
        return issue_url in self._issues

    def remember(self, issue_url: str, entries: list[Entry]) -> None:
        self._issues[issue_url] = {
            "read_at": datetime.now().isoformat(timespec="seconds"),
            "entries": [e.as_dict() for e in entries],
        }
        self._dirty = True

    @property
    def issue_count(self) -> int:
        return len(self._issues)

    def entries(self) -> list[Entry]:
        return [
            Entry(**raw)
            for issue in self._issues.values()
            for raw in issue.get("entries", [])
        ]

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Merged against what's on disk now, and written through a temp file, for the
        # same reasons as the page cache — see cache.PageCache.save().
        merged = {}
        if self.path.exists():
            try:
                merged = json.loads(self.path.read_text(encoding="utf-8")).get("issues", {})
            except (json.JSONDecodeError, OSError):
                merged = {}
        merged.update(self._issues)

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"issues": merged}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
        self._issues = merged
        self._dirty = False


def _issue_urls(archive_url: str) -> list[str]:
    resp = http.get(archive_url, headers=HEADERS, timeout=30, label="newsletter archive")
    if resp is None or resp.status_code != 200:
        # Not fatal: issues already read are still in the store, so the run keeps the
        # calls it knows and only misses whatever was published since.
        log.warning("  (newsletter archive unreadable — using the issues already read)")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    urls = [
        urljoin(archive_url, a["href"])
        for a in soup.find_all("a", href=True)
        if ISSUE_MARKER in a["href"]
    ]
    return list(dict.fromkeys(urls))


def parse_issue(html: str) -> tuple[list[Entry], list[str]]:
    """Returns the funding entries of one issue, plus the types it skipped.

    The skipped types are reported rather than dropped quietly: a type this
    connector doesn't know would otherwise make announcements disappear without a
    trace, which is the one failure mode a monitor must not have.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("main") or soup

    entries: list[Entry] = []
    skipped: list[str] = []
    for teaser in root.select("div.newsletter-teaser"):
        heading = teaser.find("a", href=True)
        if not heading:
            continue

        topline = teaser.select_one("small")
        ministry = kind = ""
        labels: list[str] = []
        deadline = None
        if topline:
            category = topline.select_one(".c-topline__category")
            ministry = category.get_text(" ", strip=True) if category else ""

            stop = topline.select_one(".c-topline__item--stopdate time[datetime]")
            deadline = stop["datetime"] if stop else None

            # Every topline item that is neither a date nor the ministry is a
            # candidate for the type, and the first one that is a known funding type
            # wins. Taking merely the first candidate would lose a call whose topline
            # happens to carry something else first — event teasers start with
            # "Ort: Berlin" — and taking the last would yield the ministry whenever
            # an entry has no type at all, as the procurement teasers don't.
            labels = [
                text for item in topline.select(".c-topline__item")
                if not (item.find("time") or item.select_one(".c-topline__category"))
                and (text := item.get_text(" ", strip=True))
            ]
            kind = next((l for l in labels if l in FUNDING_KINDS), "")

        if not kind:
            # Reported with every label the teaser carried, not just one: the point
            # of this line is to notice a funding type we don't know yet, and
            # printing "Ort: Berlin" alone would hide the "Termin" next to it.
            skipped.append(" / ".join(labels) if labels else "(ohne Typ)")
            continue

        paragraph = teaser.find("p")
        entries.append(Entry(
            title=heading.get_text(" ", strip=True),
            link=heading["href"],
            ministry=ministry,
            kind=kind,
            summary=paragraph.get_text(" ", strip=True) if paragraph else "",
            deadline=deadline,
        ))

    return entries, skipped


def _host_allowed(url: str, allowed_hosts: list[str]) -> bool:
    """Whether we may fetch the document a teaser links to.

    An allowlist rather than "follow every link": the teasers point at a dozen
    different domains, and this project checks a site's robots.txt before fetching
    from it. A host belongs here only once that check has been done — see
    full_text_hosts in config.yaml.
    """
    return urlparse(url).netloc in allowed_hosts


def _follow_to_document(resp, allowed_hosts: list[str]):
    """Follows a wrapper page to the PDF it links to. One hop, same host, or None."""
    soup = BeautifulSoup(resp.text, "html.parser")
    for anchor in soup.find_all("a", href=True):
        target = urljoin(resp.url, anchor["href"])
        if "/content/" not in target or not target.lower().endswith(".pdf"):
            continue
        if not _host_allowed(target, allowed_hosts):
            continue
        document = http.get(target, headers=HEADERS, timeout=60)
        if document is None:
            return None
        if document.status_code == 200 and pdf_text.looks_like_pdf(
            document.content, document.headers.get("content-type", "")
        ):
            return document
        return None
    return None


def _full_text(url: str, llm_config: dict | None, cache, allowed_hosts: list[str]) -> dict | None:
    """Fetches and extracts the linked announcement, or returns None.

    The result is the page-cache entry shape, so the text and the fields derived
    from it are stored exactly like a scraped HTML announcement: downloaded once,
    and re-derived from the stored text rather than re-downloaded when the model or
    the extraction prompt changes.
    """
    model = llm_extract.extraction_id(llm_config)
    cached = cache.get(url) if cache else None

    if cached and cached.get("llm_model") == model:
        return cached

    if cached and cached.get("text"):
        text = cached["text"]            # re-derive, don't re-download
    else:
        resp = http.get(url, headers=HEADERS, timeout=60)
        if resp is None or resp.status_code != 200:
            return cached
        if not pdf_text.looks_like_pdf(resp.content, resp.headers.get("content-type", "")):
            # Not the document itself but a wrapper page: the Bundesanzeiger serves
            # the PDF directly under /content/..., while the newsletter sometimes
            # links an older bookmark URL that redirects to an HTML page carrying
            # that link. One hop, and only on the same allowed host — the PDF URL is
            # read from the page rather than guessed from the id.
            resp = _follow_to_document(resp, allowed_hosts)
            if resp is None:
                return cached
        text = pdf_text.extract_text(resp.content)
        if not text:
            return cached

    funding_rate, funding_rate_percent = extract_funding_rate(text, fallback_to_any_percentage=not llm_config)
    deadline = extract_deadline(text, fallback_to_any_date=False)
    deadlines: list = []
    mode = ""
    if llm_config:
        result = llm_extract.extract(text, "", llm_config)
        if result is None:
            # Same rule as the page cache everywhere else: a failed extraction is
            # never stored, so a backend outage doesn't freeze a degraded result.
            log.warning("    (extraction failed, keeping the newsletter teaser's fields)")
            return cached
        _, funding_rate, funding_rate_percent, deadline, deadlines, mode = llm_extract.merge(
            result, "", funding_rate, funding_rate_percent, deadline
        )

    if cache:
        cache.put(url, text, "", funding_rate, funding_rate_percent, deadline,
                  model=model, deadlines=deadlines, submission_mode=mode)
    return {"text": text, "funding_rate": funding_rate,
            "funding_rate_percent": funding_rate_percent, "deadline": deadline}


def _enrich(call: FundingCall, document: dict | None) -> FundingCall:
    """Folds the linked announcement's own text and funding rate into the call.

    The deadline deliberately stays the newsletter's: it comes from a structured
    `datetime` attribute naming the submission window, which is exact, while a date
    picked out of the full text has been measured mistaking a Geltungsdauer for a
    deadline. The document's deadline is used only where the teaser had none.
    """
    if not document or not document.get("text"):
        return call

    call.text = document["text"]
    call.funding_rate = document.get("funding_rate") or ""
    call.funding_rate_percent = document.get("funding_rate_percent")
    if call.deadline is None and document.get("deadline"):
        call.deadline = document["deadline"]
        call.submission_mode = "dated"
    call.note = "Volltext aus der verlinkten Veröffentlichung"
    return call


def _as_call(entry: Entry, keywords: list[str]) -> FundingCall:
    text = f"{entry.title}. {entry.summary}"
    return FundingCall(
        source=f"Förderinfo-Newsletter{f' ({entry.ministry})' if entry.ministry else ''}",
        title=entry.title,
        programme=entry.ministry,
        deadline=date.fromisoformat(entry.deadline) if entry.deadline else None,
        # The newsletter names a concrete submission window, so this is never a
        # framework document in the sense submission_mode is about.
        submission_mode="dated" if entry.deadline else "",
        link=entry.link,
        matched_keywords=matched_keywords(f"{entry.title} {entry.summary}", keywords),
        text=text,
        note="Kurzmeldung aus dem Förderinfo-Newsletter — keine Förderquote angegeben",
    )


def fetch(source_config: dict, keywords: list[str], llm_config: dict | None = None, cache=None) -> list[FundingCall]:
    store = IssueStore(source_config["state_path"])
    crawl_delay = source_config.get("crawl_delay", 30)

    fresh = [u for u in _issue_urls(source_config["archive_url"]) if not store.known(u)]
    log.info(f"  {len(fresh)} new newsletter issue(s) to read")

    for number, url in enumerate(fresh):
        if number:
            time.sleep(crawl_delay)
        resp = http.get(url, headers=HEADERS, timeout=30)
        if resp is None or resp.status_code != 200:
            continue

        entries, skipped = parse_issue(resp.text)
        store.remember(url, entries)
        note = f", skipped {len(skipped)} non-funding ({', '.join(sorted(set(skipped)))})" if skipped else ""
        log.info(f"    {url.rsplit('/', 1)[-1][:52]}: {len(entries)} call(s){note}")

    store.save()

    # Keyed by link, because the same publication is announced again in a later
    # issue's "Aktuelle Bewerbungsfristen" section. Titles are deliberately not the
    # key: two announcements can share one, differing only in their model region.
    unique: dict[str, FundingCall] = {}
    for entry in store.entries():
        call = _as_call(entry, keywords)
        unique[call.dedup_key] = call

    allowed_hosts = source_config.get("full_text_hosts") or []
    enriched = 0
    for call in unique.values():
        if not _host_allowed(call.link, allowed_hosts):
            continue
        before = call.text
        _enrich(call, _full_text(call.link, llm_config, cache, allowed_hosts))
        if call.text is not before:
            enriched += 1
            call.matched_keywords = matched_keywords(f"{call.title} {call.text}", keywords)
            if enriched < len(unique):
                time.sleep(source_config.get("full_text_delay", 5))

    log.info(f"  (Förderinfo-Newsletter: {len(unique)} call(s) from {store.issue_count} "
          f"remembered issue(s), {enriched} with the full announcement text)")
    return list(unique.values())
