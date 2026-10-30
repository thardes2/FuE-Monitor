# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Orchestration in run_profiles: who drives the search, and whose results mix.

The `config` fixture is shared — see conftest.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fue_monitoring import main, matching, pipeline, profile_report
from fue_monitoring import profiles as profiles_module
from fue_monitoring.profiles import Profile, Theme


def make_profile(pid: str, term: str, cloud: bool) -> Profile:
    return Profile(id=pid, name=pid.upper(), groesse="kmu",
                   themen=[Theme(term, 3)], cloud_erlaubt=cloud)


@pytest.fixture
def spy(monkeypatch):
    """Records the keywords each collect() call was given."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        pipeline, "collect",
        lambda cfg: calls.append(list(cfg["keywords"])) or pipeline.CollectionResult(),
    )
    monkeypatch.setattr(matching, "match_profile", lambda *a, **k: [])
    monkeypatch.setattr(
        profile_report, "write_reports",
        lambda p, m, d, *a, **k: (Path(d) / p.id / "report.md", Path(d) / p.id / "report.html"),
    )
    return calls


def test_one_search_pass_pools_the_running_profiles_terms(config, spy, monkeypatch):
    """Searching per profile would repeat the metered source queries per company."""
    monkeypatch.setattr(
        profiles_module, "load_profiles",
        lambda d, only=None: [make_profile("a", "Alpha", True), make_profile("b", "Beta", True)],
    )

    main.run_profiles(config, only=None)

    assert spy == [["Alpha", "Beta"]], "collect must run exactly once, over the pooled terms"
    assert "GLOBAL-KEYWORD" not in spy[0], "config.yaml keywords must not leak in"


def test_a_skipped_profile_does_not_drive_the_search(config, spy, monkeypatch):
    """A profile withheld from a hosted backend must not steer discovery either —
    otherwise its terms cost queries for a report that is never written."""
    monkeypatch.setattr(
        profiles_module, "load_profiles",
        lambda d, only=None: [make_profile("yes", "Alpha", True), make_profile("no", "Beta", False)],
    )

    main.run_profiles(config, only=None)

    assert spy == [["Alpha"]], "the skipped profile's terms must not be searched"


def test_everything_runs_when_the_backend_is_local(config, spy, monkeypatch):
    config["llm"]["backend"] = "ollama"
    config["llm"]["ollama"] = {"base_url": "http://x", "model": "m"}
    monkeypatch.setattr(
        profiles_module, "load_profiles",
        lambda d, only=None: [make_profile("a", "Alpha", False), make_profile("b", "Beta", False)],
    )

    main.run_profiles(config, only=None)

    assert spy == [["Alpha", "Beta"]]


def test_no_consenting_profile_aborts_instead_of_doing_nothing(config, spy, monkeypatch):
    monkeypatch.setattr(
        profiles_module, "load_profiles", lambda d, only=None: [make_profile("no", "Beta", False)]
    )

    with pytest.raises(matching.BackendNotLocalError):
        main.run_profiles(config, only=None)

    assert spy == [], "nothing should be collected when no profile may be processed"


def test_missing_backend_reports_configuration_not_consent(config, spy, monkeypatch):
    config["llm"]["enabled"] = False
    monkeypatch.setattr(
        profiles_module, "load_profiles", lambda d, only=None: [make_profile("a", "Alpha", True)]
    )

    with pytest.raises(matching.BackendNotLocalError) as exc:
        main.run_profiles(config, only=None)
    assert "none is configured" in str(exc.value)
