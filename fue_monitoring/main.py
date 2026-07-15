from __future__ import annotations

from .config import load_config
from .export import write_excel
from .filters import deadline_ok, deadline_status, funding_rate_ok, matched_keywords
from .models import FundingCall
from .sources import bmftr_bekanntmachungen, eu_portal, manual_import


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

    if config["manual_import"]["enabled"]:
        print("Reading manually saved funding-database pages ...")
        entries += manual_import.fetch(config["manual_import"]["input_dir"], keywords)

    if config["bmftr_bekanntmachungen"]["enabled"]:
        print("Searching BMFTR/BMBF Bekanntmachungen ...")
        entries += bmftr_bekanntmachungen.fetch(
            keywords,
            api_key=config["bmftr_bekanntmachungen"]["brave_api_key"],
            max_results_per_keyword=config["bmftr_bekanntmachungen"]["max_results_per_keyword"],
        )

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
