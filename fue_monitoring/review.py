# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Remembers which calls you've already assessed, so runs don't re-present them.

The Excel file is the editing surface: you write into its "Review" column (e.g.
"not relevant") and the next run picks that up, keyed by the call's link. Marks
are also mirrored into a small JSON file, because the Excel is overwritten on
every run — if it's deleted, moved, or a write fails, the JSON still has them.

Reading happens before the new Excel is written, so the sequence is:
  load JSON -> read marks out of the existing Excel -> merge -> save JSON -> write Excel
"""

from __future__ import annotations

import json
from pathlib import Path

from openpyxl import load_workbook

from .models import FundingCall

REVIEW_HEADER = "Review"
LINK_HEADER = "Link"


def is_dismissed(review: str, dismissed_values: list[str]) -> bool:
    """Whether this mark means "don't show me this again".

    Any other non-empty text (e.g. "applied", "asking the department") is a note
    to yourself and keeps the row visible.
    """
    return bool(review) and review.strip().casefold() in {v.strip().casefold() for v in dismissed_values}


def _read_marks_from_excel(xlsx_path: Path) -> dict[str, str]:
    """Pulls {link: review} out of a previously written Excel file.

    Columns are located by header name rather than index, so the file survives
    the column order changing between versions.
    """
    if not xlsx_path.exists():
        return {}

    try:
        ws = load_workbook(xlsx_path, read_only=True).active
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            return {}

        headers = [str(h) if h is not None else "" for h in header]
        review_idx = headers.index(REVIEW_HEADER) if REVIEW_HEADER in headers else None
        link_idx = headers.index(LINK_HEADER) if LINK_HEADER in headers else None
        if review_idx is None or link_idx is None:
            return {}

        marks = {}
        for row in rows:
            if len(row) <= max(review_idx, link_idx):
                continue
            link, review = row[link_idx], row[review_idx]
            if link and review and str(review).strip():
                marks[str(link)] = str(review).strip()
        return marks
    except (OSError, ValueError, KeyError) as exc:
        print(f"  (could not read review marks from {xlsx_path}: {exc})")
        return {}


class ReviewState:
    def __init__(self, state_path: str | Path, xlsx_path: str | Path | None = None, enabled: bool = True):
        """`xlsx_path` is optional: per-profile reports are Markdown/HTML and have
        no editable column, so there marks come from the CLI and live only in JSON."""
        self.path = Path(state_path)
        self.enabled = enabled
        self._marks: dict[str, str] = {}

        if not self.enabled:
            return

        if self.path.exists():
            try:
                self._marks = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                print(f"  (review state at {self.path} unreadable, starting empty: {exc})")

        # Whatever is in the Excel right now is what you last typed, so it wins.
        if xlsx_path is not None:
            self._marks.update(_read_marks_from_excel(Path(xlsx_path)))

    def get(self, link: str) -> str:
        return self._marks.get(link, "") if self.enabled else ""

    def set(self, link: str, value: str) -> None:
        if not value.strip():
            self._marks.pop(link, None)
        else:
            self._marks[link] = value.strip()

    def apply(self, entries: list[FundingCall]) -> None:
        """Copies remembered marks onto this run's entries."""
        if not self.enabled:
            return
        for entry in entries:
            entry.review = self._marks.get(entry.link, "")

    def save(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._marks, ensure_ascii=False, indent=1), encoding="utf-8")

    @property
    def count(self) -> int:
        return len(self._marks)
