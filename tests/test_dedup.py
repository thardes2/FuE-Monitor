# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Merging the same announcement found by more than one source.

Measured on one run: 15 of 25 Förderinfo-newsletter entries point at pages the
ministry connectors already crawl, so a call reached the reports twice — once with
the full text, once as a teaser. Everything here is source-agnostic on purpose:
nothing may depend on which connector found a call, or on any one profile.
"""

from __future__ import annotations

from datetime import date

import pytest

from fue_monitoring.dedup import canonical_link, merge_duplicates, merge_pair
from fue_monitoring.models import FundingCall

LINK = "https://www.bmftr.bund.de/SharedDocs/Bekanntmachungen/DE/2026/09/x.html"


def crawled(**over) -> FundingCall:
    """How a ministry connector sees it: full text, funding rate from the document."""
    fields = dict(source="BMFTR", title="Richtlinie X", text="Volltext " * 500,
                  funding_rate="50 %", funding_rate_percent=50.0,
                  deadline=date(2026, 12, 1), submission_mode="dated",
                  link=LINK, matched_keywords=["ki"])
    return FundingCall(**{**fields, **over})


def teased(**over) -> FundingCall:
    """How the newsletter sees it: two sentences, but the ministry and an exact date."""
    fields = dict(source="Förderinfo-Newsletter (BMFTR)", title="Richtlinie X",
                  text="Zwei Sätze.", programme="BMFTR",
                  deadline=date(2026, 12, 1), submission_mode="dated",
                  link=LINK, matched_keywords=["simulation"], note="Kurzmeldung")
    return FundingCall(**{**fields, **over})


# --- canonical links -------------------------------------------------------


@pytest.mark.parametrize("variant", [
    LINK,
    LINK.removesuffix(".html"),
    LINK + "?nn=407088",
    LINK + "#top",
    LINK.replace("https://www.", "https://"),
    LINK.removesuffix(".html") + "/",
])
def test_the_spellings_of_one_url_share_a_key(variant):
    """Measured: one announcement appeared twice in a report, with differing funding
    rates, purely because two sources spelled its URL differently."""
    assert canonical_link(variant) == canonical_link(LINK)


BAZ = "https://www.bundesanzeiger.de/ebanzwww/wexsservlet?page.navid=to_bookmark_official"


def test_a_query_string_that_identifies_the_document_is_kept():
    """Regression: stripping the whole query merged two distinct BMWE Förderaufrufe —
    Modul 1 and Modul 2 of one programme — because their URLs differ only in the
    bookmark id. Found by a live run, not by the unit tests."""
    modul1 = canonical_link(f"{BAZ}&bookmark_id=AAA")
    modul2 = canonical_link(f"{BAZ}&bookmark_id=BBB")
    assert modul1 != modul2


def test_a_session_id_in_the_path_is_still_ignored():
    assert canonical_link(LINK.replace(".html", ".html;jsessionid=ABC123")) == canonical_link(LINK)


@pytest.mark.parametrize("decorative", ["?nn=407088", "?view=renderNewsletterHtml", "?0", "?cHash=ab12"])
def test_decorative_parameters_are_ignored(decorative):
    assert canonical_link(LINK + decorative) == canonical_link(LINK)


def test_the_order_of_kept_parameters_does_not_matter():
    a = canonical_link(f"{BAZ}&bookmark_id=AAA&lang=de")
    b = canonical_link("https://www.bundesanzeiger.de/ebanzwww/wexsservlet?lang=de"
                       "&bookmark_id=AAA&page.navid=to_bookmark_official")
    assert a == b


def test_different_announcements_keep_different_keys():
    other = LINK.replace("/x.html", "/y.html")
    assert canonical_link(other) != canonical_link(LINK)


def test_a_different_host_is_a_different_announcement():
    assert canonical_link("https://bmds.bund.de/a") != canonical_link("https://bmftr.bund.de/a")


# --- merging ---------------------------------------------------------------


def test_the_entry_with_the_full_document_becomes_the_base():
    merged = merge_pair(teased(), crawled())
    assert "Volltext" in merged.text
    assert merged.funding_rate == "50 %"


def test_the_order_of_the_two_does_not_matter():
    a, b = merge_pair(teased(), crawled()), merge_pair(crawled(), teased())
    assert (a.text, a.funding_rate, a.deadline) == (b.text, b.funding_rate, b.deadline)


def test_a_field_only_the_teaser_has_is_kept():
    """The newsletter names the ministry; a crawled page usually doesn't."""
    merged = merge_pair(crawled(), teased())
    assert merged.programme == "BMFTR"


def test_a_funding_rate_only_one_side_found_survives():
    merged = merge_pair(crawled(funding_rate="", funding_rate_percent=None),
                        teased(funding_rate="40 %", funding_rate_percent=40.0))
    assert merged.funding_rate == "40 %" and merged.funding_rate_percent == 40.0


def test_a_missing_deadline_is_filled_from_the_other_side():
    merged = merge_pair(crawled(deadline=None, submission_mode=""),
                        teased(deadline=date(2027, 3, 15)))
    assert merged.deadline == date(2027, 3, 15) and merged.submission_mode == "dated"


def test_where_both_know_a_deadline_the_fuller_document_decides():
    """Not a coin toss: the base's date was read from the whole announcement, the
    other's from a summary of it."""
    merged = merge_pair(crawled(deadline=date(2026, 12, 1)),
                        teased(deadline=date(2026, 11, 1)))
    assert merged.deadline == date(2026, 12, 1)


def test_the_keywords_of_both_are_kept():
    """Each side matched on the text it had, and the report should show both."""
    assert merge_pair(crawled(), teased()).matched_keywords == ["ki", "simulation"]


def test_both_sources_are_named():
    merged = merge_pair(crawled(), teased())
    assert "BMFTR" in merged.source and "Newsletter" in merged.source


def test_a_review_mark_is_never_lost():
    """It is the user's own judgement, and must not depend on which copy is richer."""
    assert merge_pair(crawled(), teased(review="not relevant")).review == "not relevant"
    assert merge_pair(crawled(review="done"), teased()).review == "done"


def test_notes_from_both_sides_are_kept_without_repeating():
    merged = merge_pair(crawled(note="bitte prüfen"), teased(note="bitte prüfen"))
    assert merged.note == "bitte prüfen"


# --- the whole list --------------------------------------------------------


def test_duplicates_collapse_and_the_rest_is_untouched():
    other = crawled(link=LINK.replace("/x.html", "/y.html"), title="Richtlinie Y")
    merged = merge_duplicates([teased(), other, crawled()])

    assert len(merged) == 2
    assert [c.title for c in merged] == ["Richtlinie X", "Richtlinie Y"], "order is preserved"


def test_three_findings_of_one_call_become_one():
    merged = merge_duplicates([teased(), crawled(), teased(link=LINK + "?nn=1")])
    assert len(merged) == 1 and "Volltext" in merged[0].text


def test_nothing_is_merged_that_should_not_be():
    """Two calls can share a title, differing only in their model region — so the
    title must never key a merge while a URL is available."""
    a = crawled(title="Modell- und Demonstrationsvorhaben Wasser", link="https://x/a")
    b = crawled(title="Modell- und Demonstrationsvorhaben Wasser", link="https://x/b")
    assert len(merge_duplicates([a, b])) == 2


def test_entries_without_a_link_fall_back_to_the_title():
    """Manually imported and static-catalogue entries may have no URL."""
    a = FundingCall(source="S", title="Programm A", link="")
    b = FundingCall(source="T", title="Programm A", link="", funding_rate="30 %")
    merged = merge_duplicates([a, b])
    assert len(merged) == 1 and merged[0].funding_rate == "30 %"


def test_differently_titled_linkless_entries_stay_apart():
    a = FundingCall(source="S", title="Programm A", link="")
    b = FundingCall(source="S", title="Programm B", link="")
    assert len(merge_duplicates([a, b])) == 2


def test_an_empty_list_is_fine():
    assert merge_duplicates([]) == []


def test_a_linkless_entry_is_not_merged_with_a_linked_one():
    """A title match alone is too weak to override a URL."""
    a = FundingCall(source="S", title="Programm A", link="")
    b = FundingCall(source="T", title="Programm A", link="https://x/a")
    assert len(merge_duplicates([a, b])) == 2


# --- the wiring ------------------------------------------------------------


def test_collect_merges_across_sources(tmp_path, monkeypatch, capsys):
    """Through collect(), not just the module: an untested wiring is no wiring. This
    test fails when the merge call is removed from collect, which the unit tests
    above do not."""
    from fue_monitoring import main
    from fue_monitoring.sources import foerderinfo_newsletter, manual_import

    config = {
        "keywords": [],
        "eu_portal": {"enabled": False},
        "llm": {"enabled": False},
        "manual_import": {"enabled": True, "input_dir": str(tmp_path)},
        "foerderinfo_newsletter": {"enabled": True},
        "gov_bekanntmachungen": {"enabled": False},
        "cache": {"enabled": False, "path": str(tmp_path / "pages.json"), "max_age_days": 30},
        "profiles": None,
    }
    monkeypatch.setattr(manual_import, "fetch", lambda *a, **k: [teased()])
    monkeypatch.setattr(foerderinfo_newsletter, "fetch", lambda *a, **k: [crawled()])

    entries = main.collect(config)

    assert len(entries) == 1, "the same announcement from two sources must collapse"
    assert "Volltext" in entries[0].text
    assert "merged" in capsys.readouterr().out, "and the run should say so"


# --- the framework classification must survive a merge ---------------------


def test_a_framework_document_is_not_turned_back_into_a_dated_call():
    """Found by a live run. A BMDS Förderrichtlinie, correctly classified as a
    framework from its full text, met its own newsletter teaser — whose date range is
    the Richtlinie's Geltungsdauer (to 2027-06-30), not a submission deadline.
    Filling the "missing" deadline from it reinstated exactly the defect
    submission_mode exists to prevent."""
    richtlinie = crawled(deadline=None, submission_mode="framework")
    teaser = teased(deadline=date(2027, 6, 30), submission_mode="dated")

    merged = merge_pair(richtlinie, teaser)

    assert merged.submission_mode == "framework"
    assert merged.deadline is None, "a Geltungsdauer must not become a deadline"


def test_the_framework_rule_does_not_block_an_ordinary_fill():
    """Only a framework base is protected; an undetermined one still gets the date."""
    merged = merge_pair(crawled(deadline=None, submission_mode=""),
                        teased(deadline=date(2027, 3, 15)))
    assert merged.deadline == date(2027, 3, 15)


def test_a_framework_teaser_does_not_demote_a_dated_call():
    """The other direction: the richer side keeps its own deadline either way."""
    merged = merge_pair(crawled(deadline=date(2026, 12, 1), submission_mode="dated"),
                        teased(deadline=None, submission_mode="framework"))
    assert merged.submission_mode == "dated" and merged.deadline == date(2026, 12, 1)
