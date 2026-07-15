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
    budget: str = ""
    link: str = ""
    matched_keywords: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def dedup_key(self) -> str:
        return self.link or self.title
