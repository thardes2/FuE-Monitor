# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .models import FundingCall

OPTIONS_SHEET = "ReviewOptions"

COLUMNS = [
    # First on purpose: this is the one column you edit. Pick e.g. "not relevant"
    # and the next run remembers it (see review.py) instead of showing the row again.
    ("review", "Review"),
    ("source", "Source"),
    ("title", "Title"),
    ("programme", "Programme"),
    ("funding_rate", "Funding Rate"),
    ("deadline", "Deadline"),
    ("deadline_status", "Deadline Status"),
    ("budget", "Budget"),
    ("matched_keywords", "Matched Keywords"),
    ("note", "Note"),
    ("link", "Link"),
]

STATUS_FILL = {
    "expired": PatternFill(start_color="F4CCCC", end_color="F4CCCC", fill_type="solid"),
    "urgent": PatternFill(start_color="FCE8B2", end_color="FCE8B2", fill_type="solid"),
    "open": PatternFill(start_color="D9EAD3", end_color="D9EAD3", fill_type="solid"),
    "unknown": PatternFill(start_color="EFEFEF", end_color="EFEFEF", fill_type="solid"),
}


def _add_review_dropdown(wb, ws, options: list[str], row_count: int) -> None:
    """Puts a dropdown of the configured values on the Review column.

    Two deliberate choices:
      - The options live on a hidden sheet and the validation points at that
        range, rather than being inlined into the formula. Excel caps an inline
        list at 255 characters and splits it on commas, so a long or
        comma-containing value in dismissed_values would silently corrupt it.
      - showErrorMessage is off, so the dropdown only *offers* the values without
        rejecting anything else. Free text in this column is a feature (it doubles
        as a notes field), and a strict list would take that away.
    """
    if not options or row_count < 1:
        return

    sheet = wb.create_sheet(OPTIONS_SHEET)
    for i, option in enumerate(options, start=1):
        sheet.cell(row=i, column=1, value=option)
    sheet.sheet_state = "hidden"

    dv = DataValidation(
        type="list",
        formula1=f"{OPTIONS_SHEET}!$A$1:$A${len(options)}",
        allow_blank=True,
        showErrorMessage=False,
    )
    ws.add_data_validation(dv)
    dv.add(f"A2:A{row_count + 1}")


def write_excel(entries: list[FundingCall], output_path: str, review_options: list[str] | None = None) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Funding Calls"

    for col_idx, (_, header) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = Font(bold=True)

    for row_idx, entry in enumerate(entries, start=2):
        for col_idx, (field_name, _) in enumerate(COLUMNS, start=1):
            value = getattr(entry, field_name)
            if field_name == "matched_keywords":
                value = ", ".join(value)
            elif field_name == "deadline" and value is not None:
                value = value.strftime("%d.%m.%Y")
            ws.cell(row=row_idx, column=col_idx, value=value)

        fill = STATUS_FILL.get(entry.deadline_status)
        if fill:
            for col_idx in range(1, len(COLUMNS) + 1):
                ws.cell(row=row_idx, column=col_idx).fill = fill

    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(len(entries) + 1, 1)}"
    # Split after the Review column too, so it stays put while scrolling right —
    # otherwise the column you type into scrolls off on a table this wide.
    ws.freeze_panes = "B2"

    widths = [16, 18, 55, 20, 14, 12, 13, 14, 25, 30, 45]
    for col_idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    _add_review_dropdown(wb, ws, review_options or [], len(entries))

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
