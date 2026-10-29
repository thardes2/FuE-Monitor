# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Retrying a page fetch, and never passing a failure off as an empty result.

Measured in one run: three ConnectionResetError(54) — twice bmds.bund.de, once
ble.de — each on the first and only contact with that host. One of them cost a
whole source, which then reported "0 announcements discovered": indistinguishable
from a ministry that has published nothing.
"""

from __future__ import annotations

import pytest
import requests

from fue_monitoring import http
from fue_monitoring.sources import gov_bekanntmachungen as gov


class Resp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


def responses(monkeypatch, *sequence):
    """Serves the given responses/exceptions in order, recording the attempts."""
    attempts = []

    def fake_get(url, **kwargs):
        attempts.append(url)
        item = sequence[min(len(attempts) - 1, len(sequence) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(http.requests, "get", fake_get)
    return attempts


def test_a_connection_reset_is_retried(monkeypatch):
    """This is the measured failure: the server closed the first connection."""
    reset = requests.exceptions.ConnectionError("('Connection aborted.', ConnectionResetError(54, ...))")
    attempts = responses(monkeypatch, reset, Resp(200, "fine"))

    assert http.get("http://x/listing").text == "fine"
    assert len(attempts) == 2


@pytest.mark.parametrize("status", sorted(http.RETRYABLE_STATUS))
def test_a_server_saying_later_is_retried(monkeypatch, status):
    attempts = responses(monkeypatch, Resp(status), Resp(200, "fine"))
    assert http.get("http://x/").status_code == 200
    assert len(attempts) == 2


def test_a_timeout_is_retried(monkeypatch):
    attempts = responses(monkeypatch, requests.exceptions.Timeout("slow"), Resp(200))
    assert http.get("http://x/").status_code == 200
    assert len(attempts) == 2


def test_attempts_are_bounded(monkeypatch):
    attempts = responses(monkeypatch, requests.exceptions.ConnectionError("down"))
    assert http.get("http://x/") is None
    assert len(attempts) == 1 + len(http.BACKOFF_SECONDS)


def test_a_real_error_is_not_retried(monkeypatch):
    """A 404 won't become a 200 by asking again."""
    attempts = responses(monkeypatch, Resp(404))
    assert http.get("http://x/").status_code == 404
    assert len(attempts) == 1


def test_a_success_costs_exactly_one_request(monkeypatch):
    attempts = responses(monkeypatch, Resp(200))
    http.get("http://x/")
    assert len(attempts) == 1


# --- the server's own waiting time ----------------------------------------


def test_a_short_retry_after_is_honoured(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(http.time, "sleep", lambda s: slept.append(s))
    responses(monkeypatch, Resp(429, headers={"Retry-After": "7"}), Resp(200))

    assert http.get("http://x/").status_code == 200
    assert 7 in slept, "the server asked for 7 seconds"


def test_an_absurd_retry_after_is_not_waited_out(monkeypatch):
    """Waiting out "come back in an hour" would stall every other source."""
    slept: list[float] = []
    monkeypatch.setattr(http.time, "sleep", lambda s: slept.append(s))
    responses(monkeypatch, Resp(503, headers={"Retry-After": "3600"}), Resp(200))

    http.get("http://x/")
    assert 3600 not in slept


def test_a_nonsense_retry_after_is_ignored(monkeypatch):
    """The header may also carry an HTTP date, which we don't parse."""
    responses(monkeypatch, Resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), Resp(200))
    assert http.get("http://x/").status_code == 200


# --- a failure must never read as "nothing published" ----------------------


def test_an_unreadable_listing_raises_rather_than_returning_nothing(monkeypatch):
    monkeypatch.setattr(http.requests, "get",
                        lambda url, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("reset")))

    with pytest.raises(gov.ListingUnavailable):
        gov._listing_urls("http://x/listing", "/marker/")


def test_a_listing_that_is_simply_empty_is_not_an_error(monkeypatch):
    monkeypatch.setattr(http.requests, "get",
                        lambda url, **k: Resp(200, "<html><body><main></main></body></html>"))
    assert gov._listing_urls("http://x/listing", "/marker/") == []


def test_an_unreadable_source_is_reported_and_does_not_stop_the_run(tmp_path, monkeypatch, capsys):
    """The run continues, but it says plainly that its results are incomplete."""
    from fue_monitoring import main
    from fue_monitoring.models import FundingCall

    def fetch(source_config, *a, **k):
        if source_config["name"] == "Broken":
            raise gov.ListingUnavailable("http://x/listing")
        return [FundingCall(source="Working", title="T", link="http://x/call")]

    monkeypatch.setattr(gov, "fetch", fetch)

    config = {
        "keywords": [], "eu_portal": {"enabled": False}, "llm": {"enabled": False},
        "manual_import": {"enabled": False}, "foerderinfo_newsletter": {"enabled": False},
        "gov_bekanntmachungen": {"enabled": True, "brave_api_key": "",
                                 "sources": [{"name": "Broken"}, {"name": "Working"}]},
        "cache": {"enabled": False, "path": str(tmp_path / "p.json"), "max_age_days": 30},
        "profiles": None,
    }
    entries = main.collect(config)
    out = capsys.readouterr().out

    assert len(entries) == 1, "the working source still contributes"
    assert "incomplete" in out and "Broken" in out
