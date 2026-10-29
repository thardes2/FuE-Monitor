# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Merging the same announcement found by more than one source.

Each source deduplicates within itself, which left the overlap *between* sources
untouched. Measured on one run: 15 of 25 Förderinfo-newsletter entries point at
pages the ministry connectors already crawl, so the same call reached a report
twice — once with the full announcement text, once as a two-sentence teaser, and
sometimes with different deadlines.

Merging rather than picking one: the sources know different things. A crawled page
has the full text and the funding rate; a newsletter teaser has an exact submission
window from structured markup and names the ministry. Dropping either loses
information that no source has on its own.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse

from . import llm_extract
from .models import FundingCall


# Query parameters that decorate a URL without identifying the document: the
# navigation id these government CMSs append, a rendering variant, a session id, and
# the usual campaign tags. Everything else is kept, because some query strings *are*
# the identifier — "?bookmark_id=..." is how the Bundesanzeiger addresses a single
# publication, and dropping it merged two distinct BMWE Förderaufrufe (Modul 1 and
# Modul 2 of one programme) into one entry.
_DECORATIVE_PARAMS = frozenset({"nn", "view", "jsessionid", "cHash", "sessionid"})
_SESSION_IN_PATH = re.compile(r";jsessionid=[^/;?]*", re.I)


def canonical_link(link: str) -> str:
    """One key for the several URLs these CMSs serve a single announcement under.

    Fragments, a trailing slash, the optional ".html" suffix, a "www." prefix and
    the decorative query parameters above all address the same page — measured: one
    announcement appeared twice in a report, with differing funding rates, purely
    because two sources spelled its URL differently.
    """
    if not link:
        return ""
    parsed = urlparse(link.strip())
    host = parsed.netloc.lower().removeprefix("www.")

    path = _SESSION_IN_PATH.sub("", parsed.path).rstrip("/")
    if path.lower().endswith(".html"):
        path = path[:-5]

    kept = sorted(
        (name, value) for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        # A purely numeric name is a framework artefact, such as Wicket's "?0".
        if name not in _DECORATIVE_PARAMS and not name.isdigit()
    )
    query = f"?{urlencode(kept)}" if kept else ""

    return f"{host}{path}{query}" if host else link.strip()


def _richer(a: FundingCall, b: FundingCall) -> tuple[FundingCall, FundingCall]:
    """Returns (base, other), the base being the one that saw more of the document.

    Length of the extracted text, because that is what the fields were derived from:
    an entry built from the full Richtlinie is a better starting point than one built
    from a teaser, whatever else the other one happens to carry.
    """
    return (a, b) if len(a.text) >= len(b.text) else (b, a)


def merge_pair(a: FundingCall, b: FundingCall) -> FundingCall:
    """Folds two findings of one announcement into the richer of them."""
    base, other = _richer(a, b)

    if not base.funding_rate and other.funding_rate:
        base.funding_rate = other.funding_rate
        base.funding_rate_percent = other.funding_rate_percent

    # A deadline the base lacks is worth taking; where both have one the base's
    # stands, since it was read from the fuller document.
    #
    # Except when the base is a framework document, which states no deadline for a
    # reason — no application can be made on it, the Förderaufrufe published under it
    # carry the dates. Measured: a BMDS Förderrichtlinie, correctly classified from
    # its full text, was turned back into a dated call by the newsletter teaser's
    # date range, which is that Richtlinie's Geltungsdauer (to 2027-06-30). Taking it
    # would reinstate exactly the defect submission_mode exists to prevent.
    framework = base.submission_mode == llm_extract.FRAMEWORK
    if base.deadline is None and other.deadline is not None and not framework:
        base.deadline = other.deadline
        base.submission_mode = other.submission_mode or base.submission_mode

    if not base.submission_mode and other.submission_mode:
        base.submission_mode = other.submission_mode
    if not base.programme and other.programme:
        base.programme = other.programme
    if not base.budget and other.budget:
        base.budget = other.budget
    # A review mark is the user's own judgement and must survive whichever copy
    # happens to be richer.
    if not base.review and other.review:
        base.review = other.review

    base.matched_keywords = sorted(set(base.matched_keywords) | set(other.matched_keywords))

    # Both sources are named, so a report says where a call was actually seen.
    names = [n for n in (base.source, other.source) if n]
    base.source = " + ".join(dict.fromkeys(names))

    notes = [n for n in (base.note, other.note) if n]
    base.note = " · ".join(dict.fromkeys(notes))
    return base


def merge_duplicates(entries: list[FundingCall]) -> list[FundingCall]:
    """Collapses announcements that several sources found, preserving order."""
    merged: dict[str, FundingCall] = {}
    for entry in entries:
        # Falls back to the title only when there is no link at all — two different
        # calls can share a title, differing only in their model region, so the
        # title is too weak to key on whenever a URL is available.
        key = canonical_link(entry.link) or f"title:{entry.title.strip().casefold()}"
        if key in merged:
            merged[key] = merge_pair(merged[key], entry)
        else:
            merged[key] = entry
    return list(merged.values())
