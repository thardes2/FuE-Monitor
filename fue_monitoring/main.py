# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

from __future__ import annotations

import argparse
import logging
import sys

from . import matching, pipeline, profile_report, profiles as profiles_module, review
from .config import load_config
from .export import write_excel
from .models import FundingCall
from .pipeline import _review_state_path, apply_filters

log = logging.getLogger(__name__)


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

    log.info(f"\n{len(filtered)} of {len(entries)} found funding calls match the filter criteria.")
    if hidden:
        log.info(f"({hidden} already marked as dealt with and left out — see the 'Review' column.)")
    unreviewed = sum(1 for e in filtered if not e.review)
    log.info(f"{unreviewed} of them not looked at yet (empty 'Review' cell).")
    log.info(f"Output written to: {config['output']['path']}")
    return filtered


def run_profiles(config: dict, only: str | None) -> None:
    """The CLI's profile run: drive the pipeline, then write the reports.

    All of the deciding lives in pipeline.py; what is left here is the part a
    library consumer would do differently — putting files on disk and saying where.
    """
    profiles_config = config["profiles"]
    loaded, skipped = pipeline.running_profiles(config, only=only)

    backend = config["llm"]["backend"]
    if skipped:
        log.info(
            f"\nSkipping {len(skipped)} profile(s) — backend is '{backend}' and they don't set "
            f"cloud_erlaubt: true: {', '.join(p.id for p in skipped)}"
        )
    if not loaded:
        raise matching.BackendNotLocalError(
            f"no profile may run against the '{backend}' backend — set cloud_erlaubt: true "
            "for the ones whose data may leave the machine, or switch llm.backend to ollama"
        )

    cloud_ids = [p.id for p in loaded if p.cloud_erlaubt]
    log.info(f"\nMatching {len(loaded)} profile(s) with the '{backend}' backend.")
    if cloud_ids and backend != "ollama":
        log.info(f"  Data of {', '.join(cloud_ids)} is sent to the hosted API (cloud_erlaubt: true).")

    # The match cache belongs to the pipeline, which builds and saves it; the page
    # cache is collect()'s own business. Nothing for the CLI to hold here.
    collection = pipeline.collect(pipeline.search_config_for(config, loaded))
    for result in pipeline.match_profiles(config, collection, loaded):
        md_path, html_path = profile_report.write_reports(
            result.profile, result.matches, profiles_config["output_dir"],
            frameworks=result.frameworks,
        )
        log.info(f"  {result.profile.name}: {len(result.matches)} match(es) -> {md_path}")
        log.info(f"  {' ' * len(result.profile.name)}  {html_path}")


def mark(config: dict, profile_id: str, link: str, status: str) -> None:
    """Sets a per-profile review mark, the counterpart to the Excel's Review column."""
    profiles_module.load_profiles(config["profiles"]["directory"], only=profile_id)  # validates the id

    state = review.ReviewState(_review_state_path(config, profile_id))
    state.set(link, status)
    state.save()

    if status.strip():
        hidden = review.is_dismissed(status, config["review"]["dismissed_values"])
        suffix = " — will be left out of the report" if hidden else " — stays in the report as a note"
        log.info(f"Marked as {status.strip()!r} for profile {profile_id!r}{suffix}.")
    else:
        log.info(f"Cleared the mark for profile {profile_id!r}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fue-monitor",
        description="Collect R&D funding calls and, optionally, match them against company profiles.",
    )
    # load_config() has always taken a path; the CLI simply never offered it. A
    # library consumer passes its own, and so should anyone running two
    # configurations side by side.
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="configuration file to use (default: config.yaml next to the package)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also show debug-level messages")
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


def configure_cli_logging(verbose: bool = False) -> None:
    """Routes the library's log records to stdout as bare messages.

    The CLI's output is deliberately unchanged from when these were print() calls,
    so the format is the message and nothing else — no level, no timestamp, no
    logger name. Only the CLI configures logging: a library that configures the
    root logger on import takes over its host application's output, which is why
    the modules themselves only ever call getLogger(__name__).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger("fue_monitoring")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Nothing above us should see these again — otherwise a host that did configure
    # the root logger would print every line twice.
    root.propagate = False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_cli_logging(verbose=args.verbose)
    config = load_config(args.config) if args.config else load_config()

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

    collection = pipeline.collect(config)
    write_global_excel(collection.calls, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
