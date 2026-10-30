# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""The Förderinfo newsletter connector — the only route by which BMWE reaches us.

The markup below is the real structure, taken from issue 21/26 of 20 October 2026.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
import requests

from fue_monitoring import http
from fue_monitoring.sources import foerderinfo_newsletter as nl

TEASER = """
<main>
  <h2>Neue Förderbekanntmachungen des Bundes</h2>
  <div class="newsletter-teaser">
    <h3 class="newsletter-teaser__heading">
      <a href="https://www.bundesanzeiger.de/pub/publication/abc/content/abc/B" target="_blank">
        Dritter Förderaufruf - Dekarbonisierung der Industrie: Modul 1
      </a>
      <small class="c-topline c-teaser__topline">
        <span class="c-topline__item"><span class="aural">Datum:</span>
          <time class="c-topline__time" datetime="2026-10-19">19.10.2026</time></span>
        <span class="c-topline__item c-topline__item--stopdate">
          <time class="c-topline__time" datetime="2026-12-06">- 06.12.2026</time></span>
        <span class="c-topline__item"><span class="c-topline__category">BMWE</span></span>
        <span class="c-topline__item">Förderaufruf</span>
      </small>
    </h3>
    <p>Bekanntmachung - Dritter Förderaufruf zum Modul 1. BAnz vom 19.10.2026.</p>
  </div>
  <h2>Veranstaltungen</h2>
  <div class="newsletter-teaser">
    <h3 class="newsletter-teaser__heading">
      <a href="https://example.org/termin">Innovationstag Mittelstand</a>
      <small class="c-topline">
        <span class="c-topline__item">
          <time class="c-topline__time" datetime="2026-11-05">05.11.2026</time></span>
        <span class="c-topline__item">Ort: Berlin</span>
        <span class="c-topline__item">Termin</span>
      </small>
    </h3>
    <p>Eine Veranstaltung, keine Ausschreibung.</p>
  </div>
</main>
"""


def test_a_funding_entry_is_parsed_with_its_deadline_and_ministry():
    entries, _ = nl.parse_issue(TEASER)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.ministry == "BMWE"
    assert entry.kind == "Förderaufruf"
    # The *end* of the submission window, from the markup's own ISO attribute —
    # no regex, no LLM, no German-date ambiguity.
    assert entry.deadline == "2026-12-06"
    assert entry.title.startswith("Dritter Förderaufruf")
    assert entry.link.endswith("/content/abc/B")


def test_an_event_is_not_collected_as_a_call():
    """Events and prizes share the teaser markup and even carry an end date."""
    entries, skipped = nl.parse_issue(TEASER)

    assert [e.title for e in entries] == [entries[0].title]
    assert any("Termin" in s for s in skipped), "the skip report must name the type"


def test_the_type_is_found_even_when_something_else_comes_first():
    """Regression: taking the first non-date item yielded "Ort: Berlin" for events.
    A call whose topline carried an extra label first would have been dropped."""
    html = TEASER.replace(
        '<span class="c-topline__item">Förderaufruf</span>',
        '<span class="c-topline__item">Ort: Bonn</span>'
        '<span class="c-topline__item">Förderaufruf</span>',
    )
    entries, _ = nl.parse_issue(html)
    assert [e.kind for e in entries] == ["Förderaufruf"]


def test_an_entry_without_any_type_is_skipped_but_reported():
    """The public-procurement teasers have no type. Skipping quietly is the one
    failure mode a monitor must not have, so the type is named in the output."""
    html = TEASER.replace('<span class="c-topline__item">Förderaufruf</span>', "")
    entries, skipped = nl.parse_issue(html)

    assert not any(e.ministry == "BMWE" for e in entries)
    assert "(ohne Typ)" in skipped


def test_the_ministry_is_never_mistaken_for_the_type():
    """Taking the last topline item would do exactly this."""
    html = TEASER.replace('<span class="c-topline__item">Förderaufruf</span>', "")
    entries, _ = nl.parse_issue(html)
    assert all(e.kind != "BMWE" for e in entries)


@pytest.mark.parametrize("kind", sorted(nl.FUNDING_KINDS))
def test_every_funding_type_is_accepted(kind):
    html = TEASER.replace(">Förderaufruf<", f">{kind}<")
    assert len(nl.parse_issue(html)[0]) == 1


def test_a_missing_end_date_leaves_the_deadline_open():
    html = TEASER.replace(
        '<span class="c-topline__item c-topline__item--stopdate">\n'
        '          <time class="c-topline__time" datetime="2026-12-06">- 06.12.2026</time></span>', "")
    entries, _ = nl.parse_issue(html)
    assert entries[0].deadline is None


# --- turning entries into calls -------------------------------------------


def test_a_call_carries_the_deadline_the_ministry_and_a_warning_about_the_rate():
    entry = nl.Entry(title="T", link="http://x/1", ministry="BMWE", kind="Bekanntmachung",
                     summary="Zusammenfassung", deadline="2026-12-06")
    call = nl._as_call(entry, keywords=["Zusammenfassung"])

    assert call.deadline == date(2026, 12, 6)
    assert call.programme == "BMWE" and "BMWE" in call.source
    assert call.submission_mode == "dated"
    assert call.funding_rate == "" and call.funding_rate_percent is None
    assert "Förderquote" in call.note, "the missing rate must be visible in the report"
    assert call.matched_keywords == ["Zusammenfassung"]
    assert "Zusammenfassung" in call.text, "profile matching quotes from this"


def test_an_entry_without_an_end_date_is_not_called_dated():
    """submission_mode drives the open-deadline filter — see filters.deadline_ok."""
    entry = nl.Entry(title="T", link="http://x/1", ministry="BMWE", kind="Bekanntmachung",
                     summary="s", deadline=None)
    assert nl._as_call(entry, []).submission_mode == ""


# --- remembering issues ----------------------------------------------------


def entry(link: str) -> nl.Entry:
    return nl.Entry(title="T", link=link, ministry="BMWE", kind="Bekanntmachung",
                    summary="s", deadline="2026-12-06")


def test_a_read_issue_is_remembered_across_runs(tmp_path):
    """The archive page lists only three issues (~6 weeks). Without this an
    announcement would drop out of the reports as soon as its issue rotated off,
    deadline or not."""
    path = tmp_path / "state.json"
    store = nl.IssueStore(path)
    store.remember("http://x/issue-1", [entry("http://x/call-1")])
    store.save()

    again = nl.IssueStore(path)
    assert again.known("http://x/issue-1")
    assert [e.link for e in again.entries()] == ["http://x/call-1"]


def test_a_concurrent_save_does_not_lose_the_other_run_s_issues(tmp_path):
    path = tmp_path / "state.json"
    first, second = nl.IssueStore(path), nl.IssueStore(path)
    first.remember("http://x/issue-1", [entry("http://x/a")])
    first.save()
    second.remember("http://x/issue-2", [entry("http://x/b")])
    second.save()

    assert set(json.loads(path.read_text(encoding="utf-8"))["issues"]) == {
        "http://x/issue-1", "http://x/issue-2"}


def test_an_unreadable_state_file_starts_empty_instead_of_failing(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    assert nl.IssueStore(path).entries() == []


# --- the whole fetch -------------------------------------------------------


@pytest.fixture
def source(tmp_path) -> dict:
    return {"archive_url": "http://x/archive", "state_path": str(tmp_path / "state.json"),
            "crawl_delay": 0}


class Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


def test_a_known_issue_is_never_fetched_twice(source, monkeypatch):
    """Each fetch costs the site's 30 s crawl delay."""
    archive = '<a href="/foerderinfo/shareddocs/newsletter/i1/i1.html">Ausgabe</a>'
    fetched: list[str] = []

    def fake_get(url, **kwargs):
        fetched.append(url)
        return Resp(archive if "archive" in url else TEASER)

    monkeypatch.setattr(http.requests, "get", fake_get)
    monkeypatch.setattr(nl.time, "sleep", lambda s: None)

    first = nl.fetch(source, ["Dekarbonisierung"])
    issue_fetches = [u for u in fetched if "newsletter" in u]
    assert len(issue_fetches) == 1 and len(first) == 1

    fetched.clear()
    second = nl.fetch(source, ["Dekarbonisierung"])
    assert [u for u in fetched if "newsletter" in u] == [], "the issue was already read"
    assert len(second) == 1, "its calls still come back"


def test_the_same_publication_in_two_issues_is_reported_once(source, monkeypatch):
    """A call reappears in a later issue's "Aktuelle Bewerbungsfristen" section."""
    archive = ('<a href="/foerderinfo/shareddocs/newsletter/i1/i1.html">1</a>'
               '<a href="/foerderinfo/shareddocs/newsletter/i2/i2.html">2</a>')
    monkeypatch.setattr(http.requests, "get",
                        lambda url, **k: Resp(archive if "archive" in url else TEASER))
    monkeypatch.setattr(nl.time, "sleep", lambda s: None)

    calls = nl.fetch(source, [])
    assert len(calls) == 1


def test_an_unreachable_archive_yields_nothing_rather_than_raising(source, monkeypatch):
    def boom(url, **kwargs):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(http.requests, "get", boom)
    assert nl.fetch(source, []) == []


def test_one_bad_issue_does_not_cost_the_others(source, monkeypatch):
    archive = ('<a href="/foerderinfo/shareddocs/newsletter/bad/bad.html">1</a>'
               '<a href="/foerderinfo/shareddocs/newsletter/good/good.html">2</a>')

    def fake_get(url, **kwargs):
        if "archive" in url:
            return Resp(archive)
        return Resp("", 500) if "bad" in url else Resp(TEASER)

    monkeypatch.setattr(http.requests, "get", fake_get)
    monkeypatch.setattr(nl.time, "sleep", lambda s: None)
    assert len(nl.fetch(source, [])) == 1
