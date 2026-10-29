# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

from __future__ import annotations

import argparse
import sys

from . import llm_extract, matching, profile_report, profiles as profiles_module, review
from .cache import PageCache
from .config import load_config
from .export import write_excel
from .filters import deadline_ok, deadline_status, funding_rate_ok, matched_keywords
from .models import FundingCall
from .sources import eu_portal, gov_bekanntmachungen, manual_import, static_programmes


def collect(config: dict) -> list[FundingCall]:
    keywords = config["keywords"]
    entries: list[FundingCall] = []

    if config["eu_portal"]["enabled"]:
        print("Querying EU Funding & Tenders Portal ...")
        entries += eu_portal.fetch(
            keywords,
            page_size=config["eu_portal"]["page_size"],
            max_pages=config["eu_portal"]["max_pages"],
        )

    # Resolved once so a misconfigured backend reports itself here, rather than
    # once per scraped page.
    llm_config = config["llm"] if llm_extract.is_configured(config["llm"]) else None
    if llm_config:
        print(f"LLM extraction: using '{llm_config['backend']}' backend.")
    elif config["llm"]["enabled"]:
        print(f"LLM extraction: '{config['llm'].get('backend')}' backend not usable, using regex only.")

    if config["manual_import"]["enabled"]:
        print("Reading manually saved funding-database pages ...")
        entries += manual_import.fetch(config["manual_import"]["input_dir"], keywords, llm_config)

    if config["gov_bekanntmachungen"]["enabled"]:
        brave_api_key = config["gov_bekanntmachungen"]["brave_api_key"]
        cache = PageCache(
            config["cache"]["path"],
            max_age_days=config["cache"]["max_age_days"],
            enabled=config["cache"]["enabled"],
        )
        try:
            for source_config in config["gov_bekanntmachungen"]["sources"]:
                print(f"Searching {source_config['name']} ...")
                entries += gov_bekanntmachungen.fetch(
                    source_config, keywords, brave_api_key, llm_config, cache
                )
        finally:
            # Saved even if a source blows up, so a long run's downloads aren't lost.
            cache.save()

    profiles_config = config.get("profiles")
    if profiles_config:
        static = static_programmes.fetch(profiles_config["static_catalogue"])
        if static:
            print(f"Read {len(static)} hand-maintained programme(s) from the static catalogue.")
            entries += static

    return entries


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
        if not deadline_ok(entry.deadline, open_only):
            continue

        entry.deadline_status = deadline_status(entry.deadline, warning_days)
        filtered.append(entry)

    filtered.sort(key=lambda e: (e.deadline is None, e.deadline))
    return filtered


def write_global_excel(entries: list[FundingCall], config: dict) -> list[FundingCall]:
    filtered = apply_filters(entries, config)

    review_config = config["review"]
    # Reads your marks out of the existing Excel before write_excel overwrites it,
    # and persists them before writing, so nothing is lost if that write fails.
    state = review.ReviewState(
        review_config["state_path"], config["output"]["path"], enabled=review_config["enabled"]
    )
    state.apply(filtered)
    state.save()

    hidden = 0
    if review_config["enabled"] and review_config["hide_dismissed"]:
        kept = [e for e in filtered if not review.is_dismissed(e.review, review_config["dismissed_values"])]
        hidden = len(filtered) - len(kept)
        filtered = kept

    write_excel(
        filtered,
        config["output"]["path"],
        review_options=review_config["dismissed_values"] if review_config["enabled"] else None,
    )

    print(f"\n{len(filtered)} of {len(entries)} found funding calls match the filter criteria.")
    if hidden:
        print(f"({hidden} already marked as dealt with and left out — see the 'Review' column.)")
    unreviewed = sum(1 for e in filtered if not e.review)
    print(f"{unreviewed} of them not looked at yet (empty 'Review' cell).")
    print(f"Output written to: {config['output']['path']}")
    return filtered


def _review_state_path(config: dict, profile_id: str) -> str:
    return config["profiles"]["review_state_template"].format(profile=profile_id)


def run_profiles(config: dict, only: str | None) -> None:
    """Collects once with the union of the running profiles' terms, then matches each.

    Searching per profile would repeat the source queries for every company, and
    those queries — Brave in particular — are the metered part of a run; page
    downloads are already shared via the page cache. One pass over the union is
    therefore markedly cheaper, and no company sees calls it shouldn't: the
    prefilter admits a call only if that profile's own themes match it, whichever
    search term happened to surface it.

    Only profiles that actually run contribute terms, so a profile skipped for
    lack of consent costs no queries. One consequence worth knowing: because the
    corpus depends on which profiles run together, a company's report can differ
    between `--profile X` alone and `--profiles`.
    """
    profiles_config = config["profiles"]
    llm_config = config["llm"] if llm_extract.is_configured(config["llm"]) else None

    loaded = profiles_module.load_profiles(profiles_config["directory"], only=only)

    # No usable backend at all is a configuration fault, not a consent decision —
    # raise with that message rather than reporting every profile as "skipped".
    if llm_config is None:
        matching.ensure_local_backend(None)

    # Consent is per profile: one company withholding it must not stop the others.
    # Decided before any company data is assembled into a prompt.
    loaded, skipped = matching.permitted_profiles(loaded, llm_config)

    backend = llm_config["backend"]
    if skipped:
        print(
            f"\nSkipping {len(skipped)} profile(s) — backend is '{backend}' and they don't set "
            f"cloud_erlaubt: true: {', '.join(p.id for p in skipped)}"
        )
    if not loaded:
        raise matching.BackendNotLocalError(
            f"no selected profile permits the '{backend}' backend; set cloud_erlaubt: true "
            f"for the ones whose data may go to a hosted API, or switch llm.backend to 'ollama'"
        )

    cloud_ids = [p.id for p in loaded if p.cloud_erlaubt and backend != "ollama"]
    print(f"\nMatching {len(loaded)} profile(s) with the '{backend}' backend.")
    if cloud_ids:
        print(f"  Data of {', '.join(cloud_ids)} is sent to the hosted API (cloud_erlaubt: true).")

    cache = matching.MatchCache(
        profiles_config["cache_path"],
        max_age_days=profiles_config["cache_max_age_days"],
        enabled=config["cache"]["enabled"],
    )
    review_config = config["review"]

    # One search pass for everyone who is actually running, using the union of
    # their terms in place of the global keyword list.
    search_config = dict(config)
    search_config["keywords"] = profiles_module.search_terms(loaded)
    print(
        f"Searching once with {len(search_config['keywords'])} term(s) pooled from the running "
        f"profile(s): {', '.join(search_config['keywords'])}"
    )
    entries = collect(search_config)

    # Deliberately without the global keyword filter — see apply_filters. Each
    # profile's own prefilter decides what it gets to see from this pool.
    candidates = apply_filters(entries, search_config, require_keywords=False)
    print(f"{len(candidates)} collected calls are open and available for matching.")

    try:
        for profile in loaded:
            print(f"\n--- {profile.name} ({profile.id}) ---")
            matches = matching.match_profile(
                candidates,
                profile,
                llm_config,
                cache=cache,
                min_score=profiles_config["min_score"],
                max_evaluations=profiles_config["max_evaluations_per_profile"],
                max_text_chars=profiles_config["max_text_chars"],
            )

            state = review.ReviewState(_review_state_path(config, profile.id), enabled=review_config["enabled"])
            for match in matches:
                match.review = state.get(match.call.link)
            if review_config["enabled"] and review_config["hide_dismissed"]:
                matches = [
                    m for m in matches
                    if not review.is_dismissed(m.review, review_config["dismissed_values"])
                ]

            md_path, html_path = profile_report.write_reports(profile, matches, profiles_config["output_dir"])
            print(f"  {profile.name}: {len(matches)} match(es) -> {md_path}")
            print(f"  {' ' * len(profile.name)}  {html_path}")
    finally:
        cache.save()


def mark(config: dict, profile_id: str, link: str, status: str) -> None:
    """Sets a per-profile review mark, the counterpart to the Excel's Review column."""
    profiles_module.load_profiles(config["profiles"]["directory"], only=profile_id)  # validates the id

    state = review.ReviewState(_review_state_path(config, profile_id))
    state.set(link, status)
    state.save()

    if status.strip():
        hidden = review.is_dismissed(status, config["review"]["dismissed_values"])
        suffix = " — will be left out of the report" if hidden else " — stays in the report as a note"
        print(f"Marked as {status.strip()!r} for profile {profile_id!r}{suffix}.")
    else:
        print(f"Cleared the mark for profile {profile_id!r}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fue_monitoring.main",
        description="Collect R&D funding calls and, optionally, match them against company profiles.",
    )
    parser.add_argument("--profiles", action="store_true", help="also build a report for every company profile")
    parser.add_argument("--profile", metavar="ID", help="also build a report for just this profile id")
    parser.add_argument(
        "--mark",
        nargs=3,
        metavar=("PROFILE", "LINK", "STATUS"),
        help='set a per-profile review mark, e.g. --mark musterwerk https://... "not relevant"; '
        'pass an empty STATUS to clear it. Does not collect anything.',
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()

    if args.mark:
        mark(config, *args.mark)
        return 0

    wants_profiles = args.profiles or bool(args.profile)
    if wants_profiles and not config.get("profiles", {}).get("enabled", False):
        print("Profile matching is switched off (profiles.enabled in config.yaml).", file=sys.stderr)
        return 1

    if wants_profiles:
        # Collecting happens inside run_profiles, once per profile with that
        # profile's own search terms. The global Excel is skipped here: it would
        # be built from profile-specific searches and overwrite the real one.
        try:
            run_profiles(config, only=args.profile)
        except (matching.BackendNotLocalError, profiles_module.ProfileError) as exc:
            print(f"\nProfile matching aborted: {exc}", file=sys.stderr)
            return 1
        return 0

    entries = collect(config)
    write_global_excel(entries, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
