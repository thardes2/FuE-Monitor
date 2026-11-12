# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""The pipeline: collecting calls and matching them against profiles.

Separate from main.py on purpose. main.py is the command-line interface — it parses
arguments, configures logging and writes files. Everything here returns data and
writes nothing, so a second application (a web service, say) can drive the same
pipeline and do its own rendering, storing and reporting.

Progress goes through `logging`, never print(): a library that writes to stdout or
configures the root logger takes over its host application's output. The CLI is the
only place that attaches a handler — see main.configure_cli_logging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import dedup, llm_extract, matching, profiles as profiles_module, review
from .cache import PageCache, SearchCache
from .filters import deadline_ok, deadline_status, funding_rate_ok, matched_keywords
from .models import FundingCall
from .profiles import Profile
from .sources import (
    eu_portal,
    foerderinfo_newsletter,
    gov_bekanntmachungen,
    manual_import,
    static_programmes,
)

log = logging.getLogger(__name__)


@dataclass
class CollectionResult:
    """Everything one collection pass found, as data.

    `open_calls` is what profile matching works on; `frameworks` are kept apart
    because nobody can apply to them (see filters.deadline_ok) and `unreachable`
    names the sources that could not be read, so a consumer can tell an incomplete
    run from a quiet week.
    """

    calls: list[FundingCall] = field(default_factory=list)
    open_calls: list[FundingCall] = field(default_factory=list)
    frameworks: list[FundingCall] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)


@dataclass
class ProfileResult:
    """One profile's outcome. Nothing here has been written anywhere."""

    profile: Profile
    matches: list[matching.Match] = field(default_factory=list)
    # Framework announcements that fit this profile's themes — worth watching for
    # the Förderaufrufe published under them, but not scored.
    frameworks: list[FundingCall] = field(default_factory=list)
    min_score: int = 0
    # Calls that passed the prefilter but whose LLM evaluation failed this run (rate
    # limit, timeout, malformed answer). Not in `matches`, not cached. Kept apart for
    # the same reason as CollectionResult.unreachable: a caller that stores results
    # must not read "missing" as "no longer a match" — these simply weren't scored.
    failed: list[FundingCall] = field(default_factory=list)


def collect(config: dict) -> CollectionResult:
    keywords = config["keywords"]
    entries: list[FundingCall] = []
    # Sources we could not read at all this run. Kept apart from sources that simply
    # had nothing, because the two look identical in a per-source count and only one
    # of them means the results are incomplete.
    unreachable: list[str] = []

    if config["eu_portal"]["enabled"]:
        log.info("Querying EU Funding & Tenders Portal ...")
        eu = eu_portal.fetch(
            keywords,
            page_size=config["eu_portal"]["page_size"],
            max_pages=config["eu_portal"]["max_pages"],
        )
        entries += eu.calls
        # Partly read counts as unreachable: the calls behind a failed keyword are
        # missing only from this run.
        unreachable += [f"EU Funding & Tenders Portal ({problem})" for problem in eu.problems]

    # Resolved once so a misconfigured backend reports itself here, rather than
    # once per scraped page.
    llm_config = config["llm"] if llm_extract.is_configured(config["llm"]) else None
    if llm_config:
        log.info(f"LLM extraction: using '{llm_config['backend']}' backend.")
    elif config["llm"]["enabled"]:
        log.info(f"LLM extraction: '{config['llm'].get('backend')}' backend not usable, using regex only.")

    if config["manual_import"]["enabled"]:
        log.info("Reading manually saved funding-database pages ...")
        entries += manual_import.fetch(config["manual_import"]["input_dir"], keywords, llm_config)

    if config.get("foerderinfo_newsletter", {}).get("enabled"):
        log.info("Reading the Förderinfo newsletter ...")
        newsletter_cache = PageCache(
            config["cache"]["path"],
            max_age_days=config["cache"]["max_age_days"],
            enabled=config["cache"]["enabled"],
        )
        try:
            entries += foerderinfo_newsletter.fetch(
                config["foerderinfo_newsletter"], keywords, llm_config, newsletter_cache
            )
        finally:
            newsletter_cache.save()

    if config["gov_bekanntmachungen"]["enabled"]:
        brave_api_key = config["gov_bekanntmachungen"]["brave_api_key"]
        cache = PageCache(
            config["cache"]["path"],
            max_age_days=config["cache"]["max_age_days"],
            enabled=config["cache"]["enabled"],
        )
        # Defaults apply to config files written before the search cache existed
        # (a deployed config.yaml is copied once): next to the page cache, 12 hours.
        search_cache = SearchCache(
            config["cache"].get("search_path")
            or Path(config["cache"]["path"]).parent / "brave_searches.json",
            max_age_hours=config["cache"].get("search_max_age_hours", 12),
            enabled=config["cache"]["enabled"],
        )
        try:
            for source_config in config["gov_bekanntmachungen"]["sources"]:
                log.info(f"Searching {source_config['name']} ...")
                try:
                    entries += gov_bekanntmachungen.fetch(
                        source_config, keywords, brave_api_key, llm_config, cache, search_cache
                    )
                except gov_bekanntmachungen.ListingUnavailable as exc:
                    # Not fatal for the run, but it must not pass as "this ministry
                    # published nothing" — collected and reported at the end.
                    unreachable.append(f"{source_config['name']} ({exc})")
        finally:
            # Saved even if a source blows up, so a long run's downloads aren't lost.
            cache.save()

    profiles_config = config.get("profiles")
    if profiles_config:
        static = static_programmes.fetch(profiles_config["static_catalogue"])
        if static:
            log.info(f"Read {len(static)} hand-maintained programme(s) from the static catalogue.")
            entries += static

    # Sources overlap: the Förderinfo newsletter links pages the ministry connectors
    # also crawl (measured: 15 of 25). Merged here, once, so every consumer —
    # the Excel and every profile report — sees one entry per announcement.
    if unreachable:
        log.warning(f"\n!! {len(unreachable)} source(s) could not be read this run — "
              "the results below are incomplete:")
        for name in unreachable:
            log.warning(f"   - {name}")
        log.warning("   Re-run to pick them up; nothing about them is cached.\n")

    before = len(entries)
    entries = dedup.merge_duplicates(entries)
    if before != len(entries):
        log.info(f"{before - len(entries)} announcement(s) found by more than one source, merged.")

    # Framework documents are kept out of the matching pool by apply_filters — nobody
    # can apply to them — but they are worth listing: they announce the Förderaufrufe
    # that will carry the real deadlines. Deduplicated by link, since the same
    # document can be discovered through several search terms.
    frameworks = list({
        entry.link: entry for entry in entries
        if entry.submission_mode == llm_extract.FRAMEWORK
    }.values())
    if frameworks:
        log.info(f"{len(frameworks)} framework announcement(s) take no direct application.")

    # Without the global keyword filter: a company's themes are its own, and gating
    # them through the shared list would hide calls that fit the company. The Excel
    # path applies that filter itself.
    open_calls = apply_filters(entries, config, require_keywords=False)

    return CollectionResult(
        calls=entries, open_calls=open_calls, frameworks=frameworks, unreachable=unreachable
    )


def apply_filters(entries: list[FundingCall], config: dict, require_keywords: bool = True) -> list[FundingCall]:
    """Applies deadline/funding-rate filters, and optionally the global keyword list.

    Profile matching passes require_keywords=False: a company's themes are its own,
    and gating them through the global keyword list would silently hide calls that
    fit the company but not the shared list.
    """
    keywords = config["keywords"]
    min_rate = config["min_funding_rate_percent"]
    open_only = config["open_deadlines_only"]
    warning_days = config["deadline_warning_days"]

    filtered = []
    for entry in entries:
        if require_keywords:
            if not entry.matched_keywords:
                entry.matched_keywords = matched_keywords(f"{entry.title} {entry.programme}", keywords)
            if not entry.matched_keywords:
                continue

        if not funding_rate_ok(entry.funding_rate_percent, min_rate):
            continue
        if not deadline_ok(entry.deadline, open_only, submission_mode=entry.submission_mode):
            continue

        entry.deadline_status = deadline_status(entry.deadline, warning_days)
        filtered.append(entry)

    filtered.sort(key=lambda e: (e.deadline is None, e.deadline))
    return filtered




def _review_state_path(config: dict, profile_id: str) -> str:
    return config["profiles"]["review_state_template"].format(profile=profile_id)


def running_profiles(config: dict, only: str | None = None) -> tuple[list[Profile], list[Profile]]:
    """Which profiles may run against the configured backend, and which may not.

    Consent is per profile and is decided before any company data is assembled into
    a prompt: one company withholding it must not stop the others. No usable backend
    at all is a configuration fault rather than a consent decision, and raises.
    """
    profiles_config = config["profiles"]
    llm_config = config["llm"] if llm_extract.is_configured(config["llm"]) else None
    loaded = profiles_module.load_profiles(profiles_config["directory"], only=only)

    if llm_config is None:
        matching.ensure_local_backend(None)

    return matching.permitted_profiles(loaded, llm_config)


def search_config_for(config: dict, profiles: list[Profile]) -> dict:
    """The collection config for one pooled pass over these profiles' terms.

    Searching per profile would repeat the metered source queries for every company;
    page downloads are already shared through the page cache. Only profiles that
    actually run contribute terms, so one skipped for lack of consent costs no
    queries.
    """
    search_config = dict(config)
    search_config["keywords"] = profiles_module.search_terms(profiles)
    log.info(
        f"Searching once with {len(search_config['keywords'])} term(s) pooled from the running "
        f"profile(s): {', '.join(search_config['keywords'])}"
    )
    return search_config


def match_profiles(
    config: dict,
    collection: CollectionResult,
    profiles: list[Profile],
    cache: matching.MatchCache | None = None,
) -> list[ProfileResult]:
    """Scores the collected calls for each profile. Writes no reports.

    The caller decides what to do with the result — render it, store it, serve it.

    `cache` holds the LLM evaluations, which are the expensive part: one inference
    per (call, profile) pair. Left as None, one is built from `profiles.cache_path`
    and saved at the end, so a caller gets the saving by default; pass your own to
    control where it lives, or an instance with enabled=False to do without. This is
    the *match* cache, not the page cache — the page cache is internal to collect().
    """
    profiles_config = config["profiles"]

    # Checked at the boundary because the two caches share a put() name and nothing
    # else: handing over a PageCache used to fail several frames deep with
    # "put() missing 4 required positional arguments", which says nothing about what
    # went wrong. Made that mistake twice while building this.
    if cache is not None and not isinstance(cache, matching.MatchCache):
        raise TypeError(
            f"match_profiles(cache=...) takes a matching.MatchCache, not "
            f"{type(cache).__name__}. The page cache is internal to collect(); this "
            f"one holds the LLM evaluations."
        )

    owns_cache = cache is None
    if owns_cache:
        cache = matching.MatchCache(
            profiles_config["cache_path"],
            max_age_days=profiles_config["cache_max_age_days"],
            enabled=config["cache"]["enabled"],
        )
    log.info(f"{len(collection.open_calls)} collected calls are open and available for matching.")

    results: list[ProfileResult] = []
    try:
        results = _match_each(config, collection, profiles, cache)
    finally:
        if owns_cache:
            # In a finally block on purpose: an evaluation costs an LLM call, and the
            # old code saved here too. Without it, one profile failing would throw
            # away everything the profiles before it paid for.
            cache.save()
    return results


def _match_each(
    config: dict,
    collection: CollectionResult,
    profiles: list[Profile],
    cache: matching.MatchCache,
) -> list[ProfileResult]:
    profiles_config = config["profiles"]
    review_config = config["review"]
    llm_config = config["llm"] if llm_extract.is_configured(config["llm"]) else None

    results: list[ProfileResult] = []
    for profile in profiles:
        log.info(f"\n--- {profile.name} ({profile.id}) ---")

        # The profile's own threshold wins where it sets one; config.yaml's is the
        # default for everyone who doesn't care.
        min_score = (
            profile.min_score if profile.min_score is not None
            else profiles_config["min_score"]
        )
        if profile.min_score is not None:
            log.info(f"  (min_score {min_score} from the profile)")

        failed: list[FundingCall] = []
        matches = matching.match_profile(
            collection.open_calls,
            profile,
            llm_config,
            cache=cache,
            min_score=min_score,
            max_evaluations=profiles_config["max_evaluations_per_profile"],
            max_text_chars=profiles_config["max_text_chars"],
            failed=failed,
        )

        state = review.ReviewState(
            _review_state_path(config, profile.id), enabled=review_config["enabled"]
        )
        for match in matches:
            match.review = state.get(match.call.link)
        if review_config["enabled"] and review_config["hide_dismissed"]:
            matches = [
                m for m in matches
                if not review.is_dismissed(m.review, review_config["dismissed_values"])
            ]

        # Only the thematically relevant frameworks, and by prefilter alone: these
        # are not scored, so they must not cost an LLM call each.
        relevant = [c for c in collection.frameworks if matching.prefilter(c, profile).passed]

        results.append(ProfileResult(
            profile=profile, matches=matches, frameworks=relevant, min_score=min_score,
            failed=failed,
        ))

    return results
