# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Brave answers are cached for some hours, so a re-run doesn't pay for them again."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from fue_monitoring import http
from fue_monitoring.cache import SearchCache
from fue_monitoring.sources import gov_bekanntmachungen as gov

HIT = {"web": {"results": [{"url": "https://x.example/bekanntmachung/1", "title": "t"}]}}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else HIT
        self.text = ""
        self.headers = {}

    def json(self):
        return self._body


def count_brave(monkeypatch, status=200):
    calls = []

    def fake_get(url, headers=None, **kwargs):
        calls.append(url)
        return Resp(status)

    monkeypatch.setattr(http.requests, "get", fake_get)
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    return calls


def test_second_run_within_max_age_is_not_billed(tmp_path, monkeypatch, caplog):
    calls = count_brave(monkeypatch)
    path = tmp_path / "brave.json"
    first = gov._brave_urls("KI", "x.example", "bekanntmachung", ["k"], 20,
                            search_cache=SearchCache(path, max_age_hours=12))
    assert len(calls) == 1
    with caplog.at_level(logging.INFO):
        second = gov._brave_urls("KI", "x.example", "bekanntmachung", ["k"], 20,
                                 search_cache=SearchCache(path, max_age_hours=12))  # a new run
    assert len(calls) == 1
    assert first == second == ["https://x.example/bekanntmachung/1"]
    assert "from cache" in caplog.text


def test_expired_answer_is_asked_again(tmp_path, monkeypatch):
    calls = count_brave(monkeypatch)
    path = tmp_path / "brave.json"
    gov._brave_urls("KI", "x.example", "bekanntmachung", ["k"], 20, search_cache=SearchCache(path))
    data = json.loads(path.read_text())
    for entry in data.values():
        entry["fetched_at"] = (datetime.now() - timedelta(hours=13)).isoformat()
    path.write_text(json.dumps(data))
    gov._brave_urls("KI", "x.example", "bekanntmachung", ["k"], 20, search_cache=SearchCache(path))
    assert len(calls) == 2


def test_new_term_is_still_asked(tmp_path, monkeypatch):
    calls = count_brave(monkeypatch)
    cache = SearchCache(tmp_path / "brave.json")
    gov._brave_urls("KI", "x.example", "bekanntmachung", ["k"], 20, search_cache=cache)
    gov._brave_urls("Wasserstoff", "x.example", "bekanntmachung", ["k"], 20, search_cache=cache)
    assert len(calls) == 2


def test_failed_search_is_not_cached(tmp_path, monkeypatch):
    calls = count_brave(monkeypatch, status=500)
    path = tmp_path / "brave.json"
    gov._brave_urls("KI", "x.example", "bekanntmachung", ["k"], 20, search_cache=SearchCache(path))
    n = len(calls)
    gov._brave_urls("KI", "x.example", "bekanntmachung", ["k"], 20, search_cache=SearchCache(path))
    assert len(calls) == 2 * n


def test_zero_hours_turns_it_off(tmp_path, monkeypatch):
    calls = count_brave(monkeypatch)
    path = tmp_path / "brave.json"
    for _ in range(2):
        gov._brave_urls("KI", "x.example", "bekanntmachung", ["k"], 20,
                        search_cache=SearchCache(path, max_age_hours=0))
    assert len(calls) == 2
    assert not path.exists()


def test_cache_is_written_after_every_answer(tmp_path, monkeypatch):
    """A run killed mid-way keeps what it already paid for."""
    count_brave(monkeypatch)
    path = tmp_path / "brave.json"
    gov._brave_urls("KI", "x.example", "bekanntmachung", ["k"], 20, pages=2, search_cache=SearchCache(path))
    assert len(json.loads(path.read_text())) == 2


def test_cached_hit_needs_no_usable_key(tmp_path, monkeypatch):
    calls = count_brave(monkeypatch)
    path = tmp_path / "brave.json"
    gov._brave_urls("KI", "x.example", "bekanntmachung", ["k"], 20, search_cache=SearchCache(path))
    urls = gov._brave_urls("KI", "x.example", "bekanntmachung", ["k"], 20, usable=[],
                           search_cache=SearchCache(path))
    assert urls == ["https://x.example/bekanntmachung/1"] and len(calls) == 1
