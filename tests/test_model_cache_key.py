# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Backend and model are part of what identifies a cached result."""

from __future__ import annotations

from datetime import date

import pytest

from fue_monitoring import llm_extract, matching
from fue_monitoring.cache import PageCache

OLLAMA = {"backend": "ollama", "ollama": {"model": "qwen3.8:latest"}}
GEMINI = {"backend": "gemini", "gemini": {"model": "gemini-flash-lite-latest"}}


def test_model_id_names_backend_and_model():
    assert llm_extract.model_id(OLLAMA) == "ollama/qwen3.8:latest"
    assert llm_extract.model_id(GEMINI) == "gemini/gemini-flash-lite-latest"
    assert llm_extract.model_id(None) == "none"


def test_extraction_id_adds_the_prompt_version():
    """The page cache records this one: a corrected prompt makes stored fields wrong,
    so those entries must be re-derived rather than trusted."""
    assert llm_extract.extraction_id(GEMINI) == (
        f"gemini/gemini-flash-lite-latest#p{llm_extract.EXTRACTION_PROMPT_VERSION}"
    )


def test_an_extraction_prompt_change_does_not_discard_evaluations(monkeypatch):
    """Evaluations cost an LLM call each and don't depend on the extraction prompt —
    hence the match cache keys on model_id, not extraction_id."""
    before = matching.cache_key("http://x", "h", llm_extract.model_id(GEMINI))
    monkeypatch.setattr(llm_extract, "EXTRACTION_PROMPT_VERSION", "99")
    assert matching.cache_key("http://x", "h", llm_extract.model_id(GEMINI)) == before


def test_switching_backend_changes_the_evaluation_key():
    """Different models score the same call differently — measured 10 vs 15 on one
    document — so reusing another model's verdict would mix them in one report."""
    a = matching.cache_key("http://x", "hash", llm_extract.model_id(OLLAMA))
    b = matching.cache_key("http://x", "hash", llm_extract.model_id(GEMINI))
    assert a != b


def test_switching_model_within_a_backend_also_changes_the_key():
    other = {"backend": "ollama", "ollama": {"model": "qwen3.6:latest"}}
    assert matching.cache_key("http://x", "h", llm_extract.model_id(OLLAMA)) != matching.cache_key(
        "http://x", "h", llm_extract.model_id(other)
    )


def test_same_model_keeps_the_key_stable():
    a = matching.cache_key("http://x", "h", llm_extract.model_id(OLLAMA))
    b = matching.cache_key("http://x", "h", llm_extract.model_id(dict(OLLAMA)))
    assert a == b


# --- page cache: model recorded, but not part of the key -------------------


def test_page_cache_records_the_producing_model(tmp_path):
    cache = PageCache(tmp_path / "pages.json")
    cache.put("http://x", "text", "T", "50 %", 50.0, date(2026, 4, 30), model="ollama/qwen3.8:latest")
    cache.save()
    assert PageCache(tmp_path / "pages.json").get("http://x")["llm_model"] == "ollama/qwen3.8:latest"


def test_page_cache_still_returns_entries_written_by_another_model(tmp_path):
    """The key stays the URL on purpose: the stored page text doesn't depend on the
    model, so a backend switch must re-derive the fields from it rather than
    re-crawling every page at its site's Crawl-delay."""
    cache = PageCache(tmp_path / "pages.json")
    cache.put("http://x", "text", "T", "", None, None, model="ollama/qwen3.8:latest")
    assert cache.get("http://x") is not None


def test_entries_from_before_model_tracking_report_an_empty_model(tmp_path):
    path = tmp_path / "pages.json"
    path.write_text(
        '{"http://x": {"fetched_at": "2026-10-28T10:00:00", "text": "t", "title": "T",'
        ' "funding_rate": "", "funding_rate_percent": null, "deadline": null}}',
        encoding="utf-8",
    )
    assert PageCache(path).get("http://x")["llm_model"] == ""


def _gov_with_cached_pages(tmp_path, monkeypatch, urls, backend_config):
    """Sets up gov.fetch over pre-cached pages, recording sleeps and forbidding crawls."""
    from fue_monitoring.sources import gov_bekanntmachungen as gov

    cache = PageCache(tmp_path / "pages.json")
    for url in urls:
        cache.put(url, "Frist bis zum 30. April 2026.", "T", "", None, None,
                  model="ollama/qwen3.8:latest")

    def boom(*a, **k):
        raise AssertionError("re-crawled a page that was already cached")

    sleeps: list[float] = []
    monkeypatch.setattr(gov.requests, "get", boom)
    monkeypatch.setattr(gov.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(gov, "_listing_urls", lambda *a, **k: list(urls))
    monkeypatch.setattr(
        llm_extract, "extract",
        lambda *a, **k: {"funding_rate_percent": 60, "deadlines": ["2026-04-30"], "title": None},
    )

    calls = gov.fetch(
        {"name": "T", "discovery": "listing", "listing_url": "http://x/list",
         "link_path_marker": "/x/", "crawl_delay": 0},
        ["Frist"], "", backend_config, cache,
    )
    return gov, cache, calls, sleeps


def test_model_change_re_extracts_from_cached_text_without_re_crawling(tmp_path, monkeypatch):
    """The whole point of keeping the model out of the page-cache key: ~95 pages at
    a 30 s Crawl-delay would be close to an hour of waiting for output that doesn't
    depend on the network."""
    urls = ["http://x/a.html", "http://x/b.html"]
    gov, cache, calls, _ = _gov_with_cached_pages(tmp_path, monkeypatch, urls, GEMINI)

    assert len(calls) == 2
    assert all(c.deadline == date(2026, 4, 30) for c in calls), "fields must be re-derived"
    for url in urls:
        assert cache.get(url)["llm_model"] == llm_extract.extraction_id(GEMINI)


def test_re_extraction_against_a_hosted_backend_is_throttled(tmp_path, monkeypatch):
    """Regression: the throttle only fires from the second entry on, so a one-entry
    test passed while the constant behind it didn't even exist."""
    urls = ["http://x/a.html", "http://x/b.html", "http://x/c.html"]
    gov, _, _, sleeps = _gov_with_cached_pages(tmp_path, monkeypatch, urls, GEMINI)

    # First one goes straight through, the following two are spaced.
    assert sleeps == [gov.HOSTED_LLM_DELAY_SECONDS, gov.HOSTED_LLM_DELAY_SECONDS]


def test_re_extraction_against_a_local_backend_is_not_throttled(tmp_path, monkeypatch):
    """A local model has no per-minute quota to respect."""
    urls = ["http://x/a.html", "http://x/b.html", "http://x/c.html"]
    local = {"backend": "ollama", "ollama": {"model": "qwen3.6:latest"}}
    _, _, _, sleeps = _gov_with_cached_pages(tmp_path, monkeypatch, urls, local)

    assert sleeps == []


# --- discovery: the listing page is not one of its own announcements ------


def test_the_listing_page_is_not_collected_as_an_announcement(monkeypatch):
    """bmds.bund.de's marker "/service/bekanntmachungen/" is the path the listing
    itself lives under, and its in-page anchors ("#", "#top") reduce to the listing
    URL — so it would be fetched and reported as an announcement carrying every real
    one's teaser text."""
    from fue_monitoring.sources import gov_bekanntmachungen as gov

    listing = "https://bmds.bund.de/service/bekanntmachungen/"
    html = (
        '<html><body><main>'
        f'<a href="{listing}#">nach oben</a>'
        f'<a href="{listing}#top">top</a>'
        f'<a href="{listing}">Bekanntmachungen</a>'
        f'<a href="{listing}foerderrichtlinie-digital-tech-to-product">Richtlinie</a>'
        '</main></body></html>'
    )

    class Resp:
        status_code = 200
        text = html

    monkeypatch.setattr(gov.requests, "get", lambda *a, **k: Resp())

    found = gov._listing_urls(listing, "/service/bekanntmachungen/")
    assert found == [f"{listing}foerderrichtlinie-digital-tech-to-product"]


def test_a_marker_that_does_not_match_the_listing_is_unaffected(monkeypatch):
    """The existing sources rely on this: BLE's listing page doesn't contain its own
    marker, and nothing about it should change."""
    from fue_monitoring.sources import gov_bekanntmachungen as gov

    listing = "https://www.ble.de/DE/Projektfoerderung/Bekanntmachungen/Bekanntmachungen_node.html"
    target = "https://www.ble.de/DE/Projektfoerderung/Foerderungen-Auftraege/investiv_node.html"

    class Resp:
        status_code = 200
        text = f'<html><body><main><a href="{target}">x</a></main></body></html>'

    monkeypatch.setattr(gov.requests, "get", lambda *a, **k: Resp())
    assert gov._listing_urls(listing, "/Foerderungen-Auftraege/") == [target]
