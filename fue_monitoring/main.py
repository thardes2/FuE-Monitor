# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

from __future__ import annotations

from . import llm_extract
from .config import load_config
from .export import write_excel
from .filters import deadline_ok, deadline_status, funding_rate_ok, matched_keywords
from .models import FundingCall
from .sources import eu_portal, gov_bekanntmachungen, manual_import


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
        for source_config in config["gov_bekanntmachungen"]["sources"]:
            print(f"Searching {source_config['name']} ...")
            entries += gov_bekanntmachungen.fetch(source_config, keywords, brave_api_key, llm_config)

    return entries


def apply_filters(entries: list[FundingCall], config: dict) -> list[FundingCall]:
    keywords = config["keywords"]
    min_rate = config["min_funding_rate_percent"]
    open_only = config["open_deadlines_only"]
    warning_days = config["deadline_warning_days"]

    filtered = []
    for entry in entries:
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


def main() -> None:
    config = load_config()
    entries = collect(config)
    filtered = apply_filters(entries, config)
    write_excel(filtered, config["output"]["path"])

    print(f"\n{len(filtered)} of {len(entries)} found funding calls match the filter criteria.")
    print(f"Output written to: {config['output']['path']}")


if __name__ == "__main__":
    main()
