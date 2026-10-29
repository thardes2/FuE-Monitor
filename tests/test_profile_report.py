# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Report rendering, and the static programme catalogue that feeds it."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from fue_monitoring import profile_report
from fue_monitoring.matching import Match
from fue_monitoring.models import FundingCall
from fue_monitoring.profiles import Profile, Theme
from fue_monitoring.sources import static_programmes

GENERATED = datetime(2026, 10, 27, 9, 30)


def profile() -> Profile:
    return Profile(
        id="musterwerk", name="Musterwerk GmbH", groesse="kmu", rolle="Entwicklungspartner",
        themen=[Theme("KI", 3)], sicp_ansprechpartner="Max Mustermann",
    )


def match(**kwargs) -> Match:
    call = FundingCall(
        source="BMFTR", title="KI in der Produktion", link="https://example.test/call",
        funding_rate="50 %", deadline=date(2027, 3, 31), programme="ZIM",
    )
    defaults = dict(
        call=call, score=82, begruendung="Passt zum Kerngeschäft.", foerderfaehig="ja",
        foerderfaehig_grund="", fundstelle="Antragsberechtigt sind KMU",
        fundstelle_belegt=True, hits=[("KI", 3)],
    )
    return Match(**{**defaults, **kwargs})


# --- markdown --------------------------------------------------------------


def test_markdown_contains_the_fields_a_reader_needs():
    md = profile_report.render_markdown(profile(), [match()], GENERATED)
    for expected in ["Musterwerk GmbH", "Max Mustermann", "82", "KI in der Produktion",
                     "31.03.2027", "50 %", "förderfähig", "Passt zum Kerngeschäft.",
                     "https://example.test/call", "27.10.2026"]:
        assert expected in md


def test_markdown_reports_no_matches_without_pretending_otherwise():
    md = profile_report.render_markdown(profile(), [], GENERATED)
    assert "Keine Treffer" in md
    assert "**0 Treffer**" in md


def test_pipe_in_a_title_does_not_break_the_table():
    m = match()
    m.call.title = "KI | Produktion"
    row = [l for l in profile_report.render_markdown(profile(), [m], GENERATED).splitlines()
           if l.startswith("| 82 ")][0]
    assert r"\|" in row  # the title's own pipe is escaped
    # ... and once the escaped one is removed, only the 6 column delimiters remain
    assert row.replace(r"\|", "").count("|") == 6


# --- the honesty requirements ----------------------------------------------


def test_unverified_quote_is_labelled_in_both_formats():
    m = match(fundstelle="frei erfundenes Zitat", fundstelle_belegt=False)
    md = profile_report.render_markdown(profile(), [m], GENERATED)
    html = profile_report.render_html(profile(), [m], GENERATED)
    assert "unbelegt" in md and "unbelegt" in html
    # and the reader is warned up front, not only in the detail section
    assert "nicht im Ausschreibungstext wiedergefunden" in md
    assert "nicht im Ausschreibungstext wiedergefunden" in html


def test_verified_quote_is_not_labelled():
    md = profile_report.render_markdown(profile(), [match()], GENERATED)
    assert "unbelegt" not in md


def test_ineligibility_reason_is_shown():
    m = match(foerderfaehig="nein", foerderfaehig_grund="nur für Großunternehmen")
    md = profile_report.render_markdown(profile(), [m], GENERATED)
    html = profile_report.render_html(profile(), [m], GENERATED)
    assert "nicht förderfähig" in md and "nur für Großunternehmen" in md
    assert "nicht förderfähig" in html and "nur für Großunternehmen" in html


def test_summary_counts_eligibility():
    matches = [match(), match(foerderfaehig="unklar"), match(foerderfaehig="nein")]
    md = profile_report.render_markdown(profile(), matches, GENERATED)
    assert "**3 Treffer**" in md and "1 als förderfähig" in md and "1 unklar" in md


# --- html ------------------------------------------------------------------


def test_html_escapes_markup_from_call_data():
    m = match()
    m.call.title = '<script>alert("x")</script>'
    html = profile_report.render_html(profile(), [m], GENERATED)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_is_self_contained():
    html = profile_report.render_html(profile(), [match()], GENERATED)
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
    assert "<style>" in html  # no external stylesheet to lose when the file is mailed on


def test_write_reports_creates_both_files_under_the_profile_id(tmp_path):
    md_path, html_path = profile_report.write_reports(profile(), [match()], tmp_path, GENERATED)
    assert md_path == tmp_path / "musterwerk" / "report.md"
    assert html_path == tmp_path / "musterwerk" / "report.html"
    assert "Musterwerk" in md_path.read_text(encoding="utf-8")
    assert "Musterwerk" in html_path.read_text(encoding="utf-8")


# --- static catalogue ------------------------------------------------------


@pytest.mark.parametrize(
    "today, expected",
    [
        (date(2026, 10, 27), date(2027, 3, 31)),  # next cut-off is in March
        (date(2027, 2, 1), date(2027, 3, 31)),
        (date(2027, 3, 31), date(2027, 3, 31)),  # a cut-off falling today still counts
        (date(2026, 4, 1), date(2026, 9, 30)),  # March has passed, September is next
        # Past 31.03.2027 only 30.09.2027 would remain, which is after the guideline
        # ends — reporting that as a deadline would invite an application that can't be made.
        (date(2027, 4, 1), None),
    ],
)
def test_recurring_deadline_picks_the_next_one_inside_the_term(today, expected):
    assert static_programmes.next_recurring_deadline(
        ["31.03.", "30.09."], valid_until=date(2027, 6, 30), today=today
    ) == expected


def test_recurring_deadline_without_a_term_keeps_running():
    assert static_programmes.next_recurring_deadline(
        ["31.03."], valid_until=None, today=date(2027, 4, 1)
    ) == date(2028, 3, 31)


def test_static_catalogue_entry_becomes_a_matchable_call(tmp_path):
    path = tmp_path / "programmes_static.yaml"
    path.write_text(
        "programme:\n"
        "  - titel: 'DNS der zukunftsfähigen Mobilität'\n"
        "    programm: 'BMWE'\n"
        "    link: 'https://example.test/dns'\n"
        "    projekttraeger: 'TÜV Rheinland'\n"
        "    stichtage_wiederkehrend: ['31.03.', '30.09.']\n"
        "    gueltig_bis: '30.06.2027'\n"
        "    beschreibung: 'Projektskizzen zu den Stichtagen.'\n",
        encoding="utf-8",
    )
    calls = static_programmes.fetch(path, today=date(2026, 10, 27))
    assert len(calls) == 1
    call = calls[0]
    assert call.deadline == date(2027, 3, 31)
    assert "Mobilität" in call.text  # the prefilter reads this
    assert "TÜV Rheinland" in call.note
    assert "not crawlable" in call.source


def test_missing_catalogue_is_not_an_error(tmp_path):
    assert static_programmes.fetch(tmp_path / "absent.yaml") == []
