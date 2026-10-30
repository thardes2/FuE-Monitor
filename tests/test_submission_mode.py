# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Telling a framework announcement apart from a continuously open programme.

Measured on one run: 35 % of scraped pages state no submission date at all, and
open_deadlines_only keeps every one of them, because a missing deadline normally
means "unknown". Some of those pages, though, name no date for a reason — no
application can be made on them at all, they announce separate Förderaufrufe that
carry the real deadlines. Those belong in a list to watch, not in a scored report.
"""

from __future__ import annotations

from datetime import date

import pytest

from fue_monitoring import llm_extract, main, matching, pipeline, profile_report
from fue_monitoring import profiles as profiles_module
from fue_monitoring.filters import deadline_ok
from fue_monitoring.models import FundingCall
from fue_monitoring.profiles import Profile, Theme

TODAY = date(2026, 10, 29)


def call(mode: str = "", deadline: date | None = None, title: str = "KI-Rahmen") -> FundingCall:
    return FundingCall(source="S", title=title, text="Thema künstliche Intelligenz",
                       deadline=deadline, submission_mode=mode, link=f"http://x/{mode or 'none'}")


# --- the filter ------------------------------------------------------------


@pytest.mark.parametrize("mode", ["continuous", "dated", ""])
def test_a_dateless_call_stays_open_unless_it_is_a_framework(mode):
    """An unknown date must not exclude a call — that would hide open programmes."""
    assert deadline_ok(None, open_only=True, today=TODAY, submission_mode=mode)


def test_a_dateless_framework_is_not_an_open_call():
    assert not deadline_ok(None, open_only=True, today=TODAY, submission_mode="framework")


def test_a_framework_that_does_name_a_date_is_judged_on_that_date():
    """The mode only decides what a *missing* date means."""
    assert deadline_ok(date(2027, 1, 1), open_only=True, today=TODAY, submission_mode="framework")
    assert not deadline_ok(date(2026, 1, 1), open_only=True, today=TODAY, submission_mode="framework")


def test_switching_the_open_filter_off_keeps_frameworks_too():
    assert deadline_ok(None, open_only=False, today=TODAY, submission_mode="framework")


# --- extraction ------------------------------------------------------------


def test_the_prompt_defines_all_three_modes():
    for mode in llm_extract.SUBMISSION_MODES:
        assert f'"{mode}"' in llm_extract.PROMPT_TEMPLATE


def test_merge_passes_the_mode_through():
    *_, mode = llm_extract.merge(
        {"funding_rate_percent": None, "deadlines": [], "submission_mode": "framework", "title": None},
        "T", "", None, None,
    )
    assert mode == "framework"


@pytest.mark.parametrize("raw", ["Rahmenbekanntmachung", "", None, "FRAMEWORK", 7])
def test_an_unrecognised_mode_becomes_empty_rather_than_being_trusted(raw):
    """A value outside the enum must not reach the filter as if it were meaningful."""
    *_, mode = llm_extract.merge(
        {"funding_rate_percent": None, "deadlines": [], "submission_mode": raw, "title": None},
        "T", "", None, None,
    )
    assert mode == ""


def test_a_failed_extraction_reports_no_mode():
    *_, mode = llm_extract.merge(None, "T", "", None, None)
    assert mode == ""


# --- the report ------------------------------------------------------------


PROFILE = Profile(id="p", name="P", groesse="kmu", themen=[Theme("künstliche Intelligenz", 3)])


def _collecting(calls):
    """A stub collect() that classifies like the real one: frameworks apart, the
    rest filtered for open deadlines."""
    def collect(config):
        frameworks = [c for c in calls if c.submission_mode == llm_extract.FRAMEWORK]
        return pipeline.CollectionResult(
            calls=list(calls),
            open_calls=main.apply_filters(list(calls), config, require_keywords=False),
            frameworks=frameworks,
        )
    return collect


def test_frameworks_are_listed_even_when_nothing_matched():
    """Silently dropping them would be worse than the noise they caused: these are
    exactly the documents whose follow-up calls are worth watching."""
    md = profile_report.render_markdown(PROFILE, [], frameworks=[call("framework")])
    assert profile_report.FRAMEWORK_HEADING in md
    assert "http://x/framework" in md
    assert "Keine Treffer" in md


def test_a_report_without_frameworks_gets_no_such_section():
    md = profile_report.render_markdown(PROFILE, [], frameworks=[])
    assert profile_report.FRAMEWORK_HEADING not in md


def test_the_html_report_lists_them_too():
    html = profile_report.render_html(PROFILE, [], frameworks=[call("framework")])
    assert profile_report.FRAMEWORK_HEADING in html
    assert 'href="http://x/framework"' in html


# --- orchestration ---------------------------------------------------------


def test_only_thematically_relevant_frameworks_reach_a_profile(tmp_path, monkeypatch, config):
    """Every profile listing all of them would just move the noise, not remove it —
    and the prefilter is used on purpose, so none of this costs an LLM call."""
    fitting = call("framework", title="Rahmen künstliche Intelligenz")
    unrelated = FundingCall(source="S", title="Rahmen Meeresbiologie", text="Korallen",
                            submission_mode="framework", link="http://x/other")

    monkeypatch.setattr(pipeline, "collect", _collecting([fitting, unrelated]))
    monkeypatch.setattr(matching, "match_profile", lambda *a, **k: [])
    monkeypatch.setattr(profiles_module, "load_profiles", lambda d, only=None: [
        Profile(id="p", name="P", groesse="kmu", themen=[Theme("künstliche Intelligenz", 3)],
                cloud_erlaubt=True)
    ])
    seen = {}
    monkeypatch.setattr(profile_report, "write_reports",
                        lambda p, m, d, *a, **k: seen.update(k) or (tmp_path / "md", tmp_path / "html"))

    main.run_profiles(config, only=None)

    assert [c.link for c in seen["frameworks"]] == ["http://x/framework"]


def test_a_framework_is_not_offered_for_matching(config, monkeypatch, tmp_path):
    framework = call("framework", title="Rahmen künstliche Intelligenz")
    open_call = FundingCall(source="S", title="Aufruf künstliche Intelligenz", text="KI",
                            submission_mode="continuous", link="http://x/open")

    monkeypatch.setattr(pipeline, "collect", _collecting([framework, open_call]))
    monkeypatch.setattr(profiles_module, "load_profiles", lambda d, only=None: [
        Profile(id="p", name="P", groesse="kmu", themen=[Theme("künstliche Intelligenz", 3)],
                cloud_erlaubt=True)
    ])
    offered = {}
    monkeypatch.setattr(matching, "match_profile",
                        lambda calls, *a, **k: offered.setdefault("links", [c.link for c in calls]) and [])
    monkeypatch.setattr(profile_report, "write_reports",
                        lambda p, m, d, *a, **k: (tmp_path / "md", tmp_path / "html"))

    main.run_profiles(config, only=None)

    assert offered["links"] == ["http://x/open"], "a framework must not cost an evaluation"
