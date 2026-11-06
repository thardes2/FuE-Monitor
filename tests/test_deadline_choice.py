# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Which of several dates is "the" deadline.

Both cases below come from a real company profile run on 2026-10-28 that reported them
wrongly, and both came from the same earlier prompt line: "if several recurring
submission dates are listed, return the latest one mentioned".
"""

from __future__ import annotations

from datetime import date

import pytest

from fue_monitoring import llm_extract
from fue_monitoring.cache import PageCache

TODAY = date(2026, 10, 29)


def test_the_next_open_deadline_wins_not_the_last_one():
    """Measured: "bis 30. November 2026 beziehungsweise 31. Mai 2028" was reported
    as 2028 — hiding a deadline four weeks out from every urgency marker."""
    dates = [date(2026, 11, 30), date(2028, 5, 31)]
    assert llm_extract.pick_deadline(dates, TODAY) == date(2026, 11, 30)


def test_recurring_stichtage_advance_to_the_next_one():
    half_yearly = [date(2026, 3, 15), date(2026, 9, 15), date(2027, 3, 15), date(2029, 9, 15)]
    assert llm_extract.pick_deadline(half_yearly, TODAY) == date(2027, 3, 15)


def test_a_deadline_today_still_counts():
    assert llm_extract.pick_deadline([TODAY], TODAY) == TODAY


def test_an_entirely_past_call_reports_its_last_date_not_nothing():
    """None would read as "deadline unknown", which passes open_deadlines_only — so
    an expired call would keep surfacing. The last date lets the filter drop it."""
    past = [date(2026, 8, 31), date(2026, 9, 30)]
    assert llm_extract.pick_deadline(past, TODAY) == date(2026, 9, 30)


def test_no_dates_at_all_stays_unknown():
    assert llm_extract.pick_deadline([], TODAY) is None


@pytest.mark.parametrize("raw,expected", [
    (["2026-11-30", "2028-05-31"], [date(2026, 11, 30), date(2028, 5, 31)]),
    (["30.11.2026"], [date(2026, 11, 30)]),          # the model sometimes answers German
    (["2026-09-10"], [date(2026, 9, 10)]),           # ISO must not be read as 9 October
    (["2026-11-30", "2026-11-30"], [date(2026, 11, 30)]),
    (["not a date", "2026-11-30"], [date(2026, 11, 30)]),
    ([], []),
    (None, []),
    ("2026-11-30", [date(2026, 11, 30)]),            # a bare string, not a list
])
def test_parsing_the_models_date_list(raw, expected):
    assert llm_extract.parse_deadlines(raw) == expected


def test_the_prompt_rules_out_the_dates_that_went_wrong():
    """The 31.12.2036 reported for one call was its Geltungsdauer — "ist bis zum
    Ablauf des 31. Dezember 2036 gültig" — while the real deadline, 31 August 2026,
    had already passed. open_deadlines_only kept it, and would have for ten years."""
    prompt = llm_extract.PROMPT_TEMPLATE
    for phrase in ("Geltungsdauer", "in Kraft", "BAnz vom", "Laufzeit", "Förderantrag"):
        assert phrase.lower() in prompt.lower(), f"prompt no longer rules out {phrase!r}"


def test_the_prompt_states_todays_date(monkeypatch):
    """Without it the model can't tell a past date from a future one, and the
    instruction to report past dates anyway has nothing to anchor on."""
    seen = {}
    monkeypatch.setattr(llm_extract, "_extract_gemini", lambda p, c: seen.setdefault("p", p) and None)
    llm_extract.extract("text", "t", {"backend": "gemini", "gemini": {"model": "m"}})
    assert date.today().isoformat() in seen["p"]


# --- the cache must not freeze the choice ----------------------------------


def test_a_cached_entry_re_picks_its_deadline_as_dates_pass(tmp_path, monkeypatch):
    """Picking at extraction time would make a recurring call vanish once its next
    Stichtag passed, for as long as the entry stayed cached."""
    path = tmp_path / "pages.json"
    cache = PageCache(path)
    cache.put("http://x", "Stichtage: 30. November 2026 und 15. März 2027", "T", "", None,
              date(2026, 11, 30), deadlines=[date(2026, 11, 30), date(2027, 3, 15)])
    cache.save()

    class Later(date):
        @classmethod
        def today(cls):
            return date(2026, 12, 1)

    monkeypatch.setattr(llm_extract, "date", Later)
    assert PageCache(path).get("http://x")["deadline"] == date(2027, 3, 15)


def test_an_entry_from_before_the_list_keeps_its_single_deadline(tmp_path):
    """Old entries hold one date and no list; they must still be readable until the
    prompt-version mismatch gets them re-extracted."""
    path = tmp_path / "pages.json"
    path.write_text(
        '{"http://x": {"fetched_at": "2026-10-28T10:00:00", "text": "t", "title": "T",'
        ' "funding_rate": "", "funding_rate_percent": null, "deadline": "2026-11-30"}}',
        encoding="utf-8",
    )
    entry = PageCache(path).get("http://x")
    assert entry["deadline"] == date(2026, 11, 30) and entry["deadlines"] == []
