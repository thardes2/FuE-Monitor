# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class FundingCall:
    source: str
    title: str
    programme: str = ""
    funding_rate: str = ""
    funding_rate_percent: float | None = None
    deadline: date | None = None
    deadline_status: str = ""
    # "dated" / "continuous" / "framework", or "" when not determined — see
    # llm_extract.SUBMISSION_MODES. A framework document announces separate
    # Förderaufrufe instead of taking applications itself, so a missing deadline
    # means something different for it than for a continuously open programme.
    submission_mode: str = ""
    budget: str = ""
    link: str = ""
    matched_keywords: list[str] = field(default_factory=list)
    note: str = ""
    # Yours to fill in inside the Excel; carried across runs by review.py.
    review: str = ""
    # The call's own description text, as far as the source provides one. Profile
    # matching reads this (and quotes from it), so without it a call would be
    # judged on its title alone. Never written to the Excel — it's far too long.
    text: str = ""

    @property
    def dedup_key(self) -> str:
        return self.link or self.title
