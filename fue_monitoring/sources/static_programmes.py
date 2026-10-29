# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Hand-maintained catalogue of programmes that can't be collected automatically.

Some relevant programmes sit on sites we're unable to crawl — bundeswirtschafts-
ministerium.de is behind Radware bot-detection, so neither discovery mode works
there. Rather than leaving those out of company reports entirely, they're kept in
programmes_static.yaml and turned into ordinary FundingCall objects here, so they
flow through prefiltering, scoring and reporting like everything else.

Recurring cut-off dates ("31.03." and "30.09." every year until the guideline
expires) are resolved to the next one still ahead, which is what a report needs
to show.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import yaml

from ..models import FundingCall

SOURCE_NAME = "Static catalogue (not crawlable)"


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(str(value).strip(), "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def next_recurring_deadline(
    recurring: list[str], valid_until: date | None, today: date | None = None
) -> date | None:
    """The next occurrence of any "DD.MM." cut-off, still inside the guideline's term.

    Returns None once every remaining occurrence would fall after valid_until —
    at that point the programme is effectively closed and shouldn't be reported
    with a deadline that can't be met.
    """
    today = today or date.today()
    candidates: list[date] = []

    for raw in recurring:
        parts = str(raw).strip().strip(".").split(".")
        if len(parts) < 2:
            continue
        try:
            day, month = int(parts[0]), int(parts[1])
        except ValueError:
            continue

        # This year's occurrence if it hasn't passed, otherwise next year's.
        for year in (today.year, today.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:  # e.g. 29.02. in a non-leap year
                continue
            if candidate >= today:
                candidates.append(candidate)
                break

    if valid_until:
        candidates = [c for c in candidates if c <= valid_until]

    return min(candidates) if candidates else None


def _to_funding_call(raw: dict, today: date | None = None) -> FundingCall | None:
    titel = str(raw.get("titel", "")).strip()
    if not titel:
        return None

    valid_until = _parse_date(raw.get("gueltig_bis", ""))
    deadline = _parse_date(raw.get("frist", "")) or next_recurring_deadline(
        raw.get("stichtage_wiederkehrend") or [], valid_until, today
    )

    beschreibung = str(raw.get("beschreibung", "")).strip()
    traeger = str(raw.get("projekttraeger", "")).strip()

    notes = ["manually maintained entry — source not crawlable"]
    if traeger:
        notes.append(f"Projektträger: {traeger}")
    if valid_until:
        notes.append(f"Richtlinie bis {valid_until.strftime('%d.%m.%Y')}")

    return FundingCall(
        source=SOURCE_NAME,
        title=titel,
        text=f"{titel} {beschreibung}".strip(),
        programme=str(raw.get("programm", "")).strip(),
        funding_rate=str(raw.get("foerderquote", "")).strip(),
        deadline=deadline,
        link=str(raw.get("link", "")).strip(),
        note="; ".join(notes),
    )


def fetch(path: str | Path, today: date | None = None) -> list[FundingCall]:
    path = Path(path)
    if not path.exists():
        return []

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        print(f"  (static programme catalogue {path.name} is not valid YAML: {exc})")
        return []

    entries = [_to_funding_call(item, today) for item in (raw.get("programme") or [])]
    return [e for e in entries if e is not None]
