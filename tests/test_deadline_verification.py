# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""A deadline the model returns has to be written in the source text.

Same idea as the funding-rate check in test_quote_verification: an invented date
is worse than none. The phrasings below are taken from cached Bekanntmachungen.
"""

from __future__ import annotations

from datetime import date

import pytest

from fue_monitoring import llm_extract
from fue_monitoring.cache import PageCache

D = date(2026, 4, 15)


@pytest.mark.parametrize(
    "text",
    [
        "Projektskizzen sind bis zum 15.04.2026 einzureichen.",
        "bis zum 15.4.2026",
        "Stichtag: 15. 04. 2026",
        "bis zum 15.04.26 um 12 Uhr",
        "bis spätestens zum 15. April 2026 um 23.59 Uhr",
        "Stichtag 15. Apr. 2026",
        "deadline 2026-04-15",
        "bis zum 15. April\n2026",
        # recurring Stichtage without a year
        "Einreichungsstichtage sind jeweils der 15. April und der 15. Oktober eines Jahres.",
        "zu zwei jährlichen Stichtagen (15. April und 15. Oktober)",
        "jeweils zum 15.04. eines Jahres",
    ],
)
def test_dates_written_in_the_text_are_kept(text):
    assert llm_extract.drop_unsupported_deadlines([D], text) == [D]


@pytest.mark.parametrize(
    "text",
    [
        "Projektskizzen sind bis zum 16.04.2026 einzureichen.",
        "bis zum 15.05.2026",
        "bis zum 115.04.2026",
        # same day and month, but a different year is stated
        "bis zum 15.04.2025",
        "bis spätestens zum 15. April 2025",
        "Skizzen können jederzeit eingereicht werden.",
        "",
    ],
)
def test_dates_not_in_the_text_are_dropped(text):
    assert llm_extract.drop_unsupported_deadlines([D], text) == []


def test_pdf_hyphenation_is_rejoined():
    """Measured in the Bundesanzeiger: "endet am 6. De- zember 2026 (Ausschlussfrist)"."""
    text = "Die Frist zur Einreichung der Skizzen endet am 6. De- zember 2026 (Ausschlussfrist)."
    assert llm_extract.drop_unsupported_deadlines([date(2026, 12, 6)], text) == [date(2026, 12, 6)]


def test_maerz_spelled_with_ae():
    assert llm_extract.deadline_in_text(date(2026, 3, 15), "bis zum 15. Maerz 2026")


def test_only_the_invented_date_goes():
    text = "Stichtage sind der 15. März 2026 und der 15. September 2026."
    got = llm_extract.drop_unsupported_deadlines(
        [date(2026, 3, 15), date(2026, 6, 1), date(2026, 9, 15)], text
    )
    assert got == [date(2026, 3, 15), date(2026, 9, 15)]


def _fake_backend(monkeypatch, answer: dict):
    monkeypatch.setattr(llm_extract, "_extract_gemini", lambda prompt, config: dict(answer))


def test_extract_drops_an_invented_deadline(monkeypatch):
    _fake_backend(monkeypatch, {
        "funding_rate_percent": None,
        "deadlines": ["2026-04-15", "2026-07-01"],
        "submission_mode": "dated",
        "title": None,
    })
    result = llm_extract.extract(
        "Projektskizzen sind bis zum 15. April 2026 einzureichen.", "t",
        {"backend": "gemini", "gemini": {"model": "m"}},
    )
    assert result["deadlines"] == ["2026-04-15"]


def test_extract_keeps_the_mode_when_every_date_is_dropped(monkeypatch):
    """The mode is the model's judgement of the text, not of a particular date;
    with no date left the call shows up as "deadline unknown" for review."""
    _fake_backend(monkeypatch, {
        "funding_rate_percent": None,
        "deadlines": ["2026-07-01"],
        "submission_mode": "dated",
        "title": None,
    })
    result = llm_extract.extract("Kein Datum hier.", "t", {"backend": "gemini", "gemini": {"model": "m"}})
    assert result["deadlines"] == []
    assert result["submission_mode"] == "dated"


def _cache_with(tmp_path, text: str, deadlines: list[date]) -> PageCache:
    cache = PageCache(str(tmp_path / "pages.json"), max_age_days=30)
    cache.put("http://x", text, "T", "", None, max(deadlines), model="m", deadlines=deadlines)
    return cache


def test_cache_read_drops_dates_stored_before_the_check_existed(tmp_path):
    cache = _cache_with(tmp_path, "Stichtag ist der 15. April 2027.", [date(2027, 4, 15), date(2027, 6, 1)])
    data = cache.get("http://x")
    assert data["deadlines"] == [date(2027, 4, 15)]
    assert data["deadline"] == date(2027, 4, 15)


def test_cache_read_clears_the_deadline_when_no_date_is_supported(tmp_path):
    cache = _cache_with(tmp_path, "Kein Datum hier.", [date(2027, 6, 1)])
    data = cache.get("http://x")
    assert data["deadlines"] == []
    assert data["deadline"] is None
