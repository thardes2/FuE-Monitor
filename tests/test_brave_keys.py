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
