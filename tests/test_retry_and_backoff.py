# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Transient backend failures: retried, then given up on without losing data."""

from __future__ import annotations

from datetime import date

import pytest

from fue_monitoring import http, llm_extract
from fue_monitoring.cache import PageCache
from fue_monitoring.sources import gov_bekanntmachungen as gov


class Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = "boom"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise llm_extract.requests.exceptions.HTTPError(f"{self.status_code} Error")


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(llm_extract.time, "sleep", lambda s: None)


@pytest.mark.parametrize("status", sorted(llm_extract.RETRYABLE_STATUS))
def test_transient_status_is_retried_until_it_succeeds(monkeypatch, status):
    """503 is Gemini's documented 'overloaded' answer — routine on shared capacity,
    and measured here at ~1 in 7 requests. Treating it as final cost the whole
    extraction."""
    attempts = []
    ok = Resp(200, {"candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}]})

    def fake_post(url, **kw):
        attempts.append(1)
        return ok if len(attempts) == 3 else Resp(status)

    monkeypatch.setattr(llm_extract.requests, "post", fake_post)
    assert llm_extract.post_with_retry("http://x").status_code == 200
    assert len(attempts) == 3


def test_retries_are_bounded(monkeypatch):
    attempts = []
    monkeypatch.setattr(llm_extract.requests, "post",
                        lambda url, **kw: (attempts.append(1), Resp(503))[1])

    assert llm_extract.post_with_retry("http://x").status_code == 503
    assert len(attempts) == 1 + len(llm_extract.RETRY_BACKOFF_SECONDS)


def test_a_normal_error_is_not_retried(monkeypatch):
    attempts = []
    monkeypatch.setattr(llm_extract.requests, "post",
                        lambda url, **kw: (attempts.append(1), Resp(400))[1])

    assert llm_extract.post_with_retry("http://x").status_code == 400
    assert len(attempts) == 1, "a bad request won't fix itself by asking again"


# --- giving up on a page that keeps failing --------------------------------


def _cached(tmp_path, failures=0):
    cache = PageCache(tmp_path / "pages.json")
    cache.put("http://x/a.html", "Frist bis zum 30. April 2026.", "T", "60 %", 60.0,
              date(2026, 4, 30), model="ollama/qwen3.8:latest")
    for _ in range(failures):
        cache.note_failure("http://x/a.html")
    return cache


def _run(cache, monkeypatch, extract_result):
    monkeypatch.setattr(http.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-crawled")))
    monkeypatch.setattr(gov.time, "sleep", lambda s: None)
    monkeypatch.setattr(gov, "_listing_urls", lambda *a, **k: ["http://x/a.html"])
    monkeypatch.setattr(llm_extract, "extract", lambda *a, **k: extract_result)
    return gov.fetch(
        {"name": "T", "discovery": "listing", "listing_url": "http://x/list",
         "link_path_marker": "/x/", "crawl_delay": 0},
        ["Frist"], "", {"backend": "gemini", "gemini": {"model": "m"}}, cache,
    )


def test_a_failed_re_extraction_keeps_the_previous_fields(tmp_path, monkeypatch):
    """Stale values beat replacing them with a regex-only guess: the regex can't
    read "30. April 2026", and an empty deadline lets an expired call through."""
    cache = _cached(tmp_path)
    calls = _run(cache, monkeypatch, None)      # backend down

    assert calls[0].deadline == date(2026, 4, 30), "the earlier value must survive"
    assert cache.get("http://x/a.html")["llm_failures"] == 1


def test_a_page_is_left_alone_after_repeated_failures(tmp_path, monkeypatch):
    """Otherwise its recorded model never catches up and it queues again every run."""
    cache = _cached(tmp_path, failures=gov.MAX_REEXTRACT_ATTEMPTS)
    seen = []
    monkeypatch.setattr(llm_extract, "extract", lambda *a, **k: seen.append(1))

    _run(cache, monkeypatch, None)
    assert seen == [], "no further attempt once the limit is reached"


def test_a_successful_re_extraction_clears_the_counter(tmp_path, monkeypatch):
    cache = _cached(tmp_path, failures=2)
    _run(cache, monkeypatch, {"funding_rate_percent": 60, "deadlines": ["2026-04-30"], "title": None})

    entry = cache.get("http://x/a.html")
    assert entry["llm_failures"] == 0
    assert entry["llm_model"] == llm_extract.extraction_id({"backend": "gemini", "gemini": {"model": "m"}})


# --- the key must not reach the log ----------------------------------------


def test_the_api_key_is_sent_as_a_header_not_in_the_url(monkeypatch):
    """requests folds the query string into the HTTPError message, so a key in
    params lands in the run log on every single backend error."""
    seen = {}

    def fake_post(url, **kw):
        seen.update(url=url, **kw)
        return Resp(200, {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]})

    monkeypatch.setattr(llm_extract.requests, "post", fake_post)
    llm_extract._extract_gemini("p", {"model": "m", "api_key": "SECRET"})

    assert "SECRET" not in seen["url"]
    assert seen.get("params") is None
    assert seen["headers"]["x-goog-api-key"] == "SECRET"


def test_an_error_message_from_gemini_carries_no_key(monkeypatch):
    monkeypatch.setattr(llm_extract.requests, "post", lambda url, **kw: Resp(503))

    result = llm_extract.extract("text", "t", {"backend": "gemini",
                                               "gemini": {"model": "m", "api_key": "SECRET"}})
    assert result is None, "a persistent 503 still degrades to regex"
