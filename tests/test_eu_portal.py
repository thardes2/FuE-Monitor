# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""The EU portal search: pages refused as too large, and failures per keyword.

On 09.11.2026 a server run died on "Beamforming": the search API refuses any page
whose records are too large together (HTTP 400, "Result size limit 100mb has been
reached, try decrease page size."), and the exception ended the whole nightly run.
"""

from __future__ import annotations

import pytest

from fue_monitoring import http
from fue_monitoring.sources import eu_portal


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.headers = {}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


SIZE_LIMIT = Resp(400, {"apiVersion": "2.157.1", "type": "businessError",
                        "message": "Result size limit 100mb has been reached, try decrease page size."})


def record(text: str, position: int) -> dict:
    return {
        "summary": f"{text} call {position}",
        "url": f"https://example.org/{text}/{position}",
        "metadata": {"deadlineDate": ["2099-01-31T17:00:00.000+0000"], "identifier": [f"ID-{text}-{position}"]},
    }


class FakeApi:
    """Serves `total` hits per keyword. A page holding a position in `big` is refused
    when it is larger than `max_size_with_big` (0: refused even alone)."""

    def __init__(self, total=149, big=(), max_size_with_big=10, broken=()):
        self.total, self.big, self.max_size_with_big, self.broken = total, set(big), max_size_with_big, set(broken)
        self.requests = []

    def __call__(self, url, *, params, **kwargs):
        text, size, number = params["text"], params["pageSize"], params["pageNumber"]
        self.requests.append((text, size, number))
        if text in self.broken:
            return Resp(503)  # retried by http.post, then given up
        positions = range((number - 1) * size + 1, min(number * size, self.total) + 1)
        if size > self.max_size_with_big and self.big.intersection(positions):
            return SIZE_LIMIT
        return Resp(200, {"totalResults": self.total, "results": [record(text, p) for p in positions]})


@pytest.fixture
def api(monkeypatch):
    def install(**kwargs):
        fake = FakeApi(**kwargs)
        monkeypatch.setattr(http.requests, "post", fake)
        monkeypatch.setattr(http.time, "sleep", lambda s: None)
        return fake
    return install


def positions(result):
    return sorted(int(call.link.rsplit("/", 1)[1]) for call in result.calls)


def test_all_results_without_trouble(api):
    fake = api()
    result = eu_portal.fetch(["Beamforming"], page_size=100, max_pages=3)
    assert positions(result) == list(range(1, 150))
    assert result.problems == []
    assert [r[2] for r in fake.requests] == [1, 2], "page 2 is short, so no page 3"


def test_page_too_large_is_fetched_in_smaller_pages(api):
    """The measured case: something around position 125 makes large pages fail."""
    fake = api(big={125}, max_size_with_big=10)
    result = eu_portal.fetch(["Beamforming"], page_size=100, max_pages=3)
    assert positions(result) == list(range(1, 150)), "nothing may get lost by splitting"
    assert result.problems == []
    sizes = {size for _, size, _ in fake.requests}
    assert {100, 50, 25, 5} <= sizes, "100 -> 50 -> 25 -> 5, each a divisor so pages line up"


def test_record_too_large_alone_is_skipped_and_reported(api):
    api(big={42}, max_size_with_big=0)
    result = eu_portal.fetch(["Beamforming"], page_size=100, max_pages=3)
    assert positions(result) == [p for p in range(1, 150) if p != 42]
    assert result.problems == ["'Beamforming': result(s) 42 too large, skipped"]


def test_failing_keyword_does_not_stop_the_others(api):
    api(total=3, broken={"Radar"})
    result = eu_portal.fetch(["Radar", "Beamforming"], page_size=100, max_pages=3)
    assert positions(result) == [1, 2, 3]
    assert result.problems == ["'Radar': not reachable"]


def test_other_client_error_is_reported_not_raised(api, monkeypatch):
    monkeypatch.setattr(http.requests, "post", lambda url, **kw: Resp(400, {"message": "bad query"}))
    result = eu_portal.fetch(["Beamforming"])
    assert result.calls == []
    assert result.problems == ["'Beamforming': HTTP 400"]


@pytest.mark.parametrize("size, expected", [(100, 50), (50, 25), (25, 5), (5, 1), (7, 1), (2, 1)])
def test_smaller_size_divides(size, expected):
    assert eu_portal._smaller_size(size) == expected


def test_collect_reports_problems_as_unreachable(tmp_path, monkeypatch):
    """Through collect(): FuE-Web deactivates missing calls only after a complete
    run, so a partly read EU portal must show up in `unreachable`."""
    from fue_monitoring import pipeline

    config = {
        "keywords": ["Beamforming"], "min_funding_rate_percent": 0, "open_deadlines_only": False,
        "deadline_warning_days": 14,
        "eu_portal": {"enabled": True, "page_size": 100, "max_pages": 3},
        "llm": {"enabled": False},
        "manual_import": {"enabled": False},
        "foerderinfo_newsletter": {"enabled": False},
        "gov_bekanntmachungen": {"enabled": False},
        "cache": {"enabled": False, "path": str(tmp_path / "pages.json"), "max_age_days": 30},
        "profiles": None,
    }
    monkeypatch.setattr(eu_portal, "fetch", lambda *a, **k: eu_portal.FetchResult(
        calls=[], problems=["'Beamforming': HTTP 400"]))

    result = pipeline.collect(config)
    assert result.unreachable == ["EU Funding & Tenders Portal ('Beamforming': HTTP 400)"]
