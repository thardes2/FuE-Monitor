# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Prefilter, quote verification and match-cache behaviour."""

from __future__ import annotations

import pytest

from fue_monitoring import http, matching
from fue_monitoring.models import FundingCall
from fue_monitoring.profiles import Profile, Theme


def call(text: str = "", title: str = "Testaufruf", link: str = "http://example.test/a") -> FundingCall:
    return FundingCall(source="Test", title=title, text=text, link=link)


def profile(themen: list[Theme] | None = None, ausschluss: list[str] | None = None) -> Profile:
    return Profile(
        id="test",
        name="Test GmbH",
        groesse="kmu",
        themen=themen or [Theme("KI", 3)],
        ausschluss=ausschluss or [],
    )


# --- word boundaries: the whole point of the prefilter ---------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Förderung von KI in der Industrie", True),
        ("Einsatz KI-gestützter Verfahren", True),  # hyphen is a word boundary
        ("Verfahren (KI) im Einsatz", True),
        ("ki-basierte Systeme", True),  # case-insensitive
        ("Angebote für Kinder und Jugendliche", False),  # the bug this fixes
        ("Reichweite in Kilometern", False),
        ("Skizzen bis Kiel einreichen", False),
    ],
)
def test_short_acronym_matches_only_whole_word(text, expected):
    assert bool(matching.prefilter(call(text), profile()).passed) is expected


@pytest.mark.parametrize(
    "term, text, expected",
    [
        ("Mobilität*", "Ein Mobilitätskonzept für Städte", True),
        ("Mobilität*", "Neue Mobilitäten erschließen", True),
        ("Mobilität*", "Mobilität in Städten", True),
        ("Mobilität", "Ein Mobilitätskonzept für Städte", False),  # no star: standalone only
        ("Mobilität", "Mobilität in Städten", True),
        ("Mobilität*", "Elektromobilität ausbauen", False),  # star extends right, not left
    ],
)
def test_trailing_star_opts_into_german_compounds(term, text, expected):
    result = matching.prefilter(call(text), profile([Theme(term, 2)]))
    assert result.passed is expected


def test_title_is_searched_too():
    result = matching.prefilter(call(text="nichts hier", title="KI für die Produktion"), profile())
    assert result.passed


def test_hits_carry_their_weights():
    p = profile([Theme("KI", 3), Theme("Robotik*", 2), Theme("Wasserstoff", 1)])
    result = matching.prefilter(call("KI und Robotiksysteme"), p)
    assert sorted(result.hits) == [("KI", 3), ("Robotik*", 2)]
    assert result.weight == 5


# --- exclusions ------------------------------------------------------------


def test_exclusion_in_the_title_blocks_even_with_topic_hits():
    p = profile(ausschluss=["Medizinprodukt*"])
    result = matching.prefilter(call("KI", title="Förderung von KI für Medizinprodukte"), p)
    assert not result.passed
    assert result.excluded_by == "Medizinprodukt*"


def test_an_exclusion_in_the_programme_name_blocks_too():
    p = profile(ausschluss=["Wehrtechnik*"])
    entry = call("KI", title="Verbundforschung")
    entry.programme = "Wehrtechnik und zivile Sicherheit"
    assert not matching.prefilter(entry, p).passed


def test_a_calls_own_disclaimer_does_not_exclude_it():
    """The decisive case. German announcements routinely say in their boilerplate
    what is *not* funded — "Vorhaben der Wehrtechnik sind von der Förderung
    ausgeschlossen". Matching exclusions against the full text turned that sentence
    into a reason to drop the call, which is the opposite of what it says: the call
    is agreeing with the company, not conflicting with it."""
    p = profile(ausschluss=["Wehrtechnik*"])
    entry = call(
        "Gefördert werden Vorhaben zur KI-basierten Simulation. "
        "Vorhaben der Wehrtechnik sind von der Förderung ausgeschlossen.",
        title="Förderung von KI-Methoden",
    )
    assert matching.prefilter(entry, p).passed, "the disclaimer must not drop the call"


def test_exclusion_respects_word_boundaries():
    p = profile(ausschluss=["Wehr"])
    assert matching.prefilter(call("KI", title="KI in der Wehrtechnik"), p).passed  # "Wehr" != "Wehrtechnik"
    assert not matching.prefilter(call("KI", title="KI für die Wehr"), p).passed


def test_the_exclusions_reach_the_evaluation_prompt():
    """Narrowing the prefilter only works if the evaluation picks the slack up —
    otherwise an exclusion buried in the text stops being honoured at all."""
    assert "{ausschluss}" in matching.PROMPT_TEMPLATE
    assert "Will not work on" in matching.PROMPT_TEMPLATE


# --- quote verification ----------------------------------------------------


def test_quote_found_despite_whitespace_and_case():
    text = "Antragsberechtigt sind\n  ausschließlich   KMU\nmit Sitz in Deutschland."
    assert matching.verify_quote("antragsberechtigt sind ausschließlich KMU", text)


def test_invented_quote_is_rejected():
    assert not matching.verify_quote("Die Förderquote beträgt 80 %", "Kein Wort davon steht hier.")


@pytest.mark.parametrize("quote", [None, "", "   "])
def test_missing_quote_is_not_verified(quote):
    assert not matching.verify_quote(quote, "irgendein Text")


def test_unverified_quote_is_flagged_in_the_match():
    evaluation = matching._normalise_evaluation(
        {"score": 80, "begruendung": "passt", "foerderfaehig": "ja",
         "foerderfaehig_grund": None, "fundstelle": "frei erfunden"},
        "der echte Text sagt etwas anderes",
    )
    assert evaluation["fundstelle_belegt"] is False
    assert "unbelegt" in matching.Match(call=call(), **evaluation).quote_note


# --- evaluation sanitising -------------------------------------------------


@pytest.mark.parametrize("raw, expected", [(150, 100), (-5, 0), ("abc", 0), (None, 0), (73, 73)])
def test_score_is_clamped(raw, expected):
    result = matching._normalise_evaluation({"score": raw, "foerderfaehig": "ja"}, "text")
    assert result["score"] == expected


@pytest.mark.parametrize("raw", ["vielleicht", "", None, "JA!"])
def test_unexpected_eligibility_falls_back_to_unklar(raw):
    result = matching._normalise_evaluation({"score": 1, "foerderfaehig": raw}, "text")
    assert result["foerderfaehig"] == "unklar"


def test_eligibility_is_case_insensitive():
    assert matching._normalise_evaluation({"score": 1, "foerderfaehig": "Ja"}, "t")["foerderfaehig"] == "ja"


# --- cache key -------------------------------------------------------------


def test_cache_key_distinguishes_call_and_profile():
    a = matching.cache_key("http://a", "hash1")
    assert a != matching.cache_key("http://b", "hash1")  # other call
    assert a != matching.cache_key("http://a", "hash2")  # other profile
    assert a == matching.cache_key("http://a", "hash1")  # stable


def test_cache_key_changes_with_prompt_version(monkeypatch):
    before = matching.cache_key("http://a", "hash1")
    monkeypatch.setattr(matching, "PROMPT_VERSION", "999")
    assert matching.cache_key("http://a", "hash1") != before


def test_cache_roundtrip_and_expiry(tmp_path):
    path = tmp_path / "matches.json"
    evaluation = {"score": 42, "begruendung": "x", "foerderfaehig": "ja",
                  "foerderfaehig_grund": "", "fundstelle": "", "fundstelle_belegt": False}

    cache = matching.MatchCache(path)
    assert cache.get("k") is None
    cache.put("k", evaluation)
    cache.save()

    assert matching.MatchCache(path).get("k") == evaluation
    assert matching.MatchCache(path, max_age_days=0).get("k") is None  # expired
    assert matching.MatchCache(path, enabled=False).get("k") is None


# --- backend restriction ---------------------------------------------------


@pytest.mark.parametrize("llm_config", [None, {}, {"backend": "gemini"}, {"backend": "something"}])
def test_profile_matching_refuses_non_local_backends(llm_config):
    """Company profiles are confidential — anything but local Ollama must abort."""
    with pytest.raises(matching.BackendNotLocalError):
        matching.ensure_local_backend(llm_config)


def test_cloud_backend_allowed_when_profile_opts_in():
    matching.ensure_local_backend({"backend": "gemini"}, allow_cloud=True)


def test_local_ollama_backend_is_accepted():
    matching.ensure_local_backend({"backend": "ollama", "ollama": {"base_url": "http://x"}})


# --- URL canonicalisation (regression: one announcement reported twice) -----


def test_html_suffix_variants_are_the_same_announcement():
    """Brave returns both spellings; without this they were fetched, evaluated and
    reported twice — observed with differing funding rates in a profile report."""
    from fue_monitoring.sources.gov_bekanntmachungen import _canonical

    with_suffix = "https://example.test/Bekanntmachungen/DE/2026/07/ki.html"
    without = "https://example.test/Bekanntmachungen/DE/2026/07/ki"
    assert _canonical(with_suffix) == _canonical(without)
    assert _canonical(with_suffix + "?view=renderNewsletterHtml") == _canonical(without)


def test_different_announcements_stay_distinct():
    from fue_monitoring.sources.gov_bekanntmachungen import _canonical

    assert _canonical("https://x.test/a.html") != _canonical("https://x.test/b.html")


def test_html_spelling_is_the_one_kept_for_fetching():
    from fue_monitoring.sources.gov_bekanntmachungen import _remember_url

    for order in ([".html", ""], ["", ".html"]):  # whichever arrives first
        urls: dict[str, str] = {}
        for suffix in order:
            _remember_url(urls, f"https://example.test/ki{suffix}")
        assert list(urls.values()) == ["https://example.test/ki.html"]


def test_query_parameters_are_dropped_from_the_fetched_url():
    from fue_monitoring.sources.gov_bekanntmachungen import _remember_url

    urls: dict[str, str] = {}
    _remember_url(urls, "https://example.test/ki.html?nn=123")
    assert list(urls.values()) == ["https://example.test/ki.html"]


# --- a failed LLM call must not be cached (regression) ----------------------


def test_failed_llm_extraction_is_not_cached(tmp_path, monkeypatch):
    """A transient backend outage would otherwise persist for cache.max_age_days.

    The regex fallback can't read written dates like "30. April 2026", so the
    cached deadline becomes None — and a call with no deadline passes the
    open_deadlines_only filter, surfacing an expired call in a company report.
    """
    from fue_monitoring import llm_extract
    from fue_monitoring.cache import PageCache
    from fue_monitoring.sources import gov_bekanntmachungen as gov

    html = "<html><body><main><h1>T</h1><p>Frist bis zum 30. April 2026.</p></main></body></html>"

    class Resp:
        status_code = 200
        text = html

    monkeypatch.setattr(http.requests, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(llm_extract, "extract", lambda *a, **k: None)  # backend down

    cache = PageCache(tmp_path / "pages.json")
    gov.fetch(
        {"name": "T", "discovery": "listing", "listing_url": "http://x/list",
         "link_path_marker": "/x/", "crawl_delay": 0},
        ["T"], "", {"backend": "ollama", "enabled": True, "ollama": {"base_url": "http://x"}}, cache,
    )
    cache.save()
    assert cache._entries == {}, "a degraded extraction was written to the cache"


def test_successful_llm_extraction_is_cached(tmp_path, monkeypatch):
    from fue_monitoring import llm_extract
    from fue_monitoring.cache import PageCache
    from fue_monitoring.sources import gov_bekanntmachungen as gov

    class Resp:
        status_code = 200
        text = '<html><body><main><h1>T</h1><p><a href="/x/a.html">A</a> Frist 30.04.2026</p></main></body></html>'

    monkeypatch.setattr(http.requests, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(
        llm_extract, "extract",
        lambda *a, **k: {"funding_rate_percent": None, "deadline": "2026-04-30", "title": None},
    )

    cache = PageCache(tmp_path / "pages.json")
    gov.fetch(
        {"name": "T", "discovery": "listing", "listing_url": "http://x/list",
         "link_path_marker": "/x/", "crawl_delay": 0},
        ["T"], "", {"backend": "ollama", "enabled": True, "ollama": {"base_url": "http://x"}}, cache,
    )
    assert cache._entries, "a good extraction should be cached"


# --- per-profile consent: skip, don't abort the whole run ------------------


def consenting(allowed: bool, pid: str = "p") -> Profile:
    p = profile()
    p.id = pid
    p.cloud_erlaubt = allowed
    return p


def test_hosted_backend_runs_the_consenting_profiles_and_skips_the_rest():
    """One company withholding consent must not block the others' reports."""
    yes, no = consenting(True, "beispielbau"), consenting(False, "musterwerk")
    runnable, skipped = matching.permitted_profiles([yes, no], {"backend": "gemini"})
    assert [p.id for p in runnable] == ["beispielbau"]
    assert [p.id for p in skipped] == ["musterwerk"]


def test_local_backend_runs_everything_regardless_of_consent():
    yes, no = consenting(True, "a"), consenting(False, "b")
    runnable, skipped = matching.permitted_profiles([yes, no], {"backend": "ollama"})
    assert [p.id for p in runnable] == ["a", "b"] and skipped == []


def test_no_profile_consents_leaves_nothing_to_run():
    runnable, skipped = matching.permitted_profiles([consenting(False)], {"backend": "gemini"})
    assert runnable == [] and len(skipped) == 1


# --- wildcards -------------------------------------------------------------


def test_a_leading_wildcard_matches_the_tail_of_a_compound():
    """Regression: "*absicherung*" was escaped into a literal asterisk, so it matched
    nothing at all — and said nothing about it."""
    assert matching.theme_pattern("*absicherung*").search("Fahrzeugabsicherung und Test")


def test_a_leading_wildcard_alone_still_anchors_the_end():
    assert matching.theme_pattern("*absicherung").search("Fahrzeugabsicherung")
    assert not matching.theme_pattern("*absicherung").search("Absicherungsstrategie")


def test_a_trailing_wildcard_still_works_as_before():
    assert matching.theme_pattern("Mobilität*").search("Mobilitätskonzept")
    assert not matching.theme_pattern("Mobilität").search("Mobilitätskonzept")


def test_a_bare_acronym_stays_strict():
    """The whole reason short terms don't get implicit prefix matching."""
    assert not matching.theme_pattern("KI").search("Kinderbetreuung")
    assert matching.theme_pattern("KI").search("Einsatz von KI im Betrieb")


def test_a_wildcard_term_finds_itself_standalone():
    assert matching.theme_pattern("*absicherung*").search("Die Absicherung erfolgt später")
