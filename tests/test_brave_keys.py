# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Several Brave API keys: every request picks one at random."""

from __future__ import annotations

import logging

import pytest

from fue_monitoring import http
from fue_monitoring.sources import gov_bekanntmachungen as gov


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {"web": {"results": []}}
        self.text = text
        self.headers = {}

    def json(self):
        return self._body


@pytest.fixture
def brave(monkeypatch):
    """Records the key of every request; answers with the given responses in turn."""
    def install(*answers):
        keys = []

        def fake_get(url, headers=None, **kwargs):
            keys.append(headers["X-Subscription-Token"])
            return answers[min(len(keys) - 1, len(answers) - 1)]

        monkeypatch.setattr(http.requests, "get", fake_get)
        monkeypatch.setattr(http.time, "sleep", lambda s: None)
        return keys
    return install


@pytest.mark.parametrize("value, expected", [
    ("abc", ["abc"]),
    (["a", "b", "c"], ["a", "b", "c"]),
    ([" a ", "", None, "b"], ["a", "b"]),
    ("", []),
    ([], []),
    (None, []),
])
def test_brave_keys_accepts_one_or_many(value, expected):
    assert gov.brave_keys(value) == expected


def test_brave_keys_rejects_other_types():
    with pytest.raises(ValueError, match="string or a list"):
        gov.brave_keys({"key": "abc"})


def test_every_request_draws_a_key(brave, monkeypatch):
    keys = brave(Resp(body={"web": {"results": [{"url": "https://x.example/bekanntmachung/1"}]}}))
    draws = iter([2, 0, 1])
    monkeypatch.setattr(gov.random, "randrange", lambda n: next(draws))
    gov._brave_urls("KI", "x.example", "bekanntmachung", ["k1", "k2", "k3"], 20, pages=3)
    assert keys == ["k3", "k1", "k2"]


def test_keys_are_spread_over_many_requests(brave):
    keys = brave(Resp(body={"web": {"results": [{"url": "https://x.example/bekanntmachung/1"}]}}))
    for _ in range(60):
        gov._brave_urls("KI", "x.example", "bekanntmachung", ["k1", "k2", "k3"], 20, pages=1)
    assert set(keys) == {"k1", "k2", "k3"}


def test_retry_after_429_draws_again(brave, monkeypatch):
    keys = brave(Resp(status=429), Resp())
    draws = iter([0, 1])
    monkeypatch.setattr(gov.random, "randrange", lambda n: next(draws))
    gov._brave_urls("KI", "x.example", "bekanntmachung", ["k1", "k2"], 20)
    assert keys == ["k1", "k2"]


def test_failure_names_the_key_position_never_the_key(brave, caplog):
    brave(Resp(status=401, text="invalid token"))
    with caplog.at_level(logging.WARNING):
        gov._brave_urls("KI", "x.example", "bekanntmachung", ["geheim-1", "geheim-2"], 20)
    assert "of 2" in caplog.text
    assert "geheim" not in caplog.text


def test_fetch_with_a_list_and_without_keys(brave, caplog):
    source = {"name": "BMFTR", "discovery": "brave", "link_path_marker": "bekanntmachung",
              "search_site": "x.example", "crawl_delay": 0}
    keys = brave(Resp())
    gov.fetch(source, ["KI"], ["k1", "k2"], None)
    assert keys and set(keys) <= {"k1", "k2"}

    with caplog.at_level(logging.WARNING):
        assert gov.fetch(source, ["KI"], ["", " "], None) == []
    assert "brave_api_key not set" in caplog.text


class KeyedBrave:
    """Answers per key: `answers` maps a key to its response, default 200."""
    def __init__(self, monkeypatch, answers):
        self.keys = []

        def fake_get(url, headers=None, **kwargs):
            key = (headers or {}).get("X-Subscription-Token")
            if key is None:  # not a Brave search: fetch() downloading a found page
                return Resp(status=404)
            self.keys.append(key)
            return answers.get(key, Resp(body={"web": {"results": [{"url": "https://x.example/bekanntmachung/1"}]}}))

        monkeypatch.setattr(http.requests, "get", fake_get)
        monkeypatch.setattr(http.time, "sleep", lambda s: None)


NO_CREDIT = Resp(status=402, body={"error": {"detail": "Prepaid credit balance is insufficient for this request."}})


def test_key_without_credit_falls_back_to_another(monkeypatch, caplog):
    brave = KeyedBrave(monkeypatch, {"leer": NO_CREDIT})
    draws = iter([0, 0])  # first draw: "leer"; after it is dropped only "voll" is left
    monkeypatch.setattr(gov.random, "randrange", lambda n: next(draws))
    with caplog.at_level(logging.WARNING):
        urls = gov._brave_urls("KI", "x.example", "bekanntmachung", ["leer", "voll"], 20)
    assert urls == ["https://x.example/bekanntmachung/1"]
    assert brave.keys == ["leer", "voll"]
    assert "HTTP 402" in caplog.text and "key 1 of 2" in caplog.text
    assert "failed" not in caplog.text


def test_rejected_key_is_dropped_for_all_following_terms(monkeypatch):
    brave = KeyedBrave(monkeypatch, {"leer": NO_CREDIT})
    monkeypatch.setattr(gov.random, "randrange", lambda n: 0)  # always the first usable key
    usable = [0, 1]
    for term in ["A", "B", "C", "D", "E", "F"]:
        gov._brave_urls(term, "x.example", "bekanntmachung", ["leer", "voll"], 20, usable=usable)
    assert brave.keys.count("leer") == 1
    assert usable == [1]


def test_all_keys_rejected_stops_without_looping(monkeypatch, caplog):
    brave = KeyedBrave(monkeypatch, {"a": NO_CREDIT, "b": Resp(status=401, text="invalid token")})
    with caplog.at_level(logging.WARNING):
        assert gov._brave_urls("KI", "x.example", "bekanntmachung", ["a", "b"], 20, pages=3) == []
    assert sorted(brave.keys) == ["a", "b"]
    assert "every API key was rejected" in caplog.text


def test_answered_search_is_logged_at_info(monkeypatch, caplog):
    KeyedBrave(monkeypatch, {})
    with caplog.at_level(logging.INFO):
        gov._brave_urls("KI", "x.example", "bekanntmachung", ["k1", "k2"], 20)
    assert "Brave 'KI' on x.example: 1 results on 1 page, 1 announcement link(s)" in caplog.text
    assert "failed" not in caplog.text


def test_fetch_summarises_the_brave_searches(monkeypatch, caplog):
    KeyedBrave(monkeypatch, {"leer": NO_CREDIT})
    monkeypatch.setattr(gov.random, "randrange", lambda n: 0)
    source = {"name": "BMFTR", "discovery": "brave", "link_path_marker": "bekanntmachung",
              "search_site": "x.example", "crawl_delay": 0, "discovery_terms": ["Bekanntmachung"]}
    with caplog.at_level(logging.INFO):
        gov.fetch(source, ["KI"], ["leer", "voll"], None)
    assert "BMFTR: Brave answered 2 of 2 searches, 1 distinct announcement URLs; 1 of 2 keys rejected" in caplog.text
