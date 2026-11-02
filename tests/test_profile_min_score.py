# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""A profile's own min_score, and keys that nothing reads.

Both come from the same defect: a "min_score: 40" written into a profile file was
silently ignored, so that company's report kept listing zero-score calls while the
global profiles.min_score of 0 quietly applied. Nothing said a word — which is the
part worth fixing, because the next unsupported key will be a typo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fue_monitoring import main, matching, pipeline, profile_report
from fue_monitoring import profiles as profiles_module
from fue_monitoring.profiles import KNOWN_KEYS, Profile, ProfileError, Theme, load_profile

BASE = {
    "id": "musterwerk",
    "name": "Musterwerk GmbH",
    "groesse": "kmu",
    "themen": [{"begriff": "KI", "gewicht": 3}],
}


def write(tmp_path: Path, **extra) -> Path:
    path = tmp_path / "musterwerk.yaml"
    path.write_text(yaml.safe_dump({**BASE, **extra}, allow_unicode=True), encoding="utf-8")
    return path


# --- the field -------------------------------------------------------------


def test_a_profile_can_set_its_own_threshold(tmp_path):
    assert load_profile(write(tmp_path, min_score=40)).min_score == 40


def test_no_threshold_means_use_the_global_one(tmp_path):
    assert load_profile(write(tmp_path)).min_score is None


def test_zero_is_a_threshold_not_an_absence(tmp_path):
    """0 must stay 0 and not fall back to the global setting."""
    assert load_profile(write(tmp_path, min_score=0)).min_score == 0


@pytest.mark.parametrize("bad", ["hoch", 101, -1, 4.7])
def test_an_unusable_threshold_is_refused_loudly(tmp_path, bad):
    with pytest.raises(ProfileError, match="min_score"):
        load_profile(write(tmp_path, min_score=bad))


def test_the_threshold_is_not_part_of_the_fingerprint():
    """Raising it must not throw away evaluations that were already paid for — it
    decides what gets reported, not what a match means."""
    themen = [Theme("KI", 3)]
    a = Profile(id="p", name="P", groesse="kmu", themen=themen, min_score=0)
    b = Profile(id="p", name="P", groesse="kmu", themen=themen, min_score=40)
    assert a.hash == b.hash


# --- it has to reach the run ----------------------------------------------


@pytest.fixture
def running(monkeypatch, tmp_path):
    """Captures the min_score that match_profile is actually called with."""
    seen = {}
    monkeypatch.setattr(pipeline, "collect", lambda cfg: pipeline.CollectionResult())
    monkeypatch.setattr(matching, "match_profile",
                        lambda *a, **k: seen.update(k) or [])
    monkeypatch.setattr(profile_report, "write_reports",
                        lambda *a, **k: (tmp_path / "md", tmp_path / "html"))
    return seen


def profile(**over) -> Profile:
    fields = dict(id="p", name="P", groesse="kmu", themen=[Theme("KI", 3)], cloud_erlaubt=True)
    return Profile(**{**fields, **over})


def test_the_profiles_threshold_is_the_one_used(config, monkeypatch, running):
    """Regression: this is the bug. config.yaml said 0, the profile said 40, and 0
    won — so a report kept showing zero-score calls."""
    config["profiles"]["min_score"] = 0
    monkeypatch.setattr(profiles_module, "load_profiles",
                        lambda d, only=None: [profile(min_score=40)])

    main.run_profiles(config, only=None)
    assert running["min_score"] == 40


def test_the_global_threshold_applies_where_a_profile_sets_none(config, monkeypatch, running):
    config["profiles"]["min_score"] = 25
    monkeypatch.setattr(profiles_module, "load_profiles",
                        lambda d, only=None: [profile(min_score=None)])

    main.run_profiles(config, only=None)
    assert running["min_score"] == 25


def test_a_profile_may_lower_the_threshold_too(config, monkeypatch, running):
    config["profiles"]["min_score"] = 50
    monkeypatch.setattr(profiles_module, "load_profiles",
                        lambda d, only=None: [profile(min_score=0)])

    main.run_profiles(config, only=None)
    assert running["min_score"] == 0, "an explicit 0 must not fall back to 50"


# --- keys nothing reads ----------------------------------------------------


def test_unsupported_keys_are_refused(tmp_path):
    """These three sat in a real profile file for days without a word. A warning
    would have been read once and scrolled past, while the file kept claiming a
    setting that had no effect."""
    with pytest.raises(ProfileError) as exc:
        load_profile(write(tmp_path, standort="Paderborn", verbund="SICP", stand="2026-10"))

    message = str(exc.value)
    assert all(key in message for key in ("standort", "verbund", "stand"))
    assert "notizen" in message, "the message must say where notes belong"


def test_a_supported_key_is_accepted(tmp_path):
    loaded = load_profile(write(tmp_path, min_score=40, description="Werkzeuganbieter",
                                sicp_ansprechpartner="Max Mustermann"))
    assert loaded.min_score == 40 and loaded.description == "Werkzeuganbieter"


def test_notes_can_be_parked_under_notizen(tmp_path):
    """Without an escape hatch, refusing unknown keys would force real notes out of
    the file altogether."""
    assert load_profile(write(tmp_path, notizen={"standort": "Paderborn", "stand": "2026-10"})).id


def test_a_misspelled_weight_is_refused(tmp_path):
    """"gewichtung" instead of "gewicht" would fall back to weight 1 and change how
    everything is scored for that company."""
    path = tmp_path / "musterwerk.yaml"
    path.write_text(yaml.safe_dump(
        {**BASE, "themen": [{"begriff": "Simulation", "gewichtung": 3}]}, allow_unicode=True),
        encoding="utf-8")

    with pytest.raises(ProfileError) as exc:
        load_profile(path)
    assert "gewichtung" in str(exc.value) and "Simulation" in str(exc.value)


def test_the_example_profile_uses_only_supported_keys():
    """It is the template everyone copies, so it must not teach a key we ignore."""
    raw = yaml.safe_load(Path("profiles/example.yaml").read_text(encoding="utf-8"))
    assert set(raw) <= KNOWN_KEYS, f"example.yaml has unsupported key(s): {set(raw) - KNOWN_KEYS}"


# --- the static catalogue must be reachable by a profile at all ------------


def test_the_static_catalogue_entries_carry_matchable_topics():
    """An entry nothing can match is an entry that may as well not exist. The DNS
    programme's description named no topics at all, so no profile — not even a
    mobility one — ever reached it through the prefilter."""
    from fue_monitoring import matching
    from fue_monitoring.sources import static_programmes

    calls = static_programmes.fetch("programmes_static.yaml")
    assert calls, "the catalogue should hold at least one programme"

    mobility = Profile(
        id="m", name="Mobilitäts-GmbH", groesse="kmu",
        themen=[Theme("Mobilität*", 3), Theme("automatisiertes Fahren", 3), Theme("*absicherung*", 2)],
    )
    for entry in calls:
        assert matching.prefilter(entry, mobility).passed, (
            f"{entry.title!r} is unreachable for a plainly related profile"
        )


def test_the_static_catalogue_does_not_invent_programme_names():
    """"DNS" stands for "Digital – Nachhaltig – Systemfähig". An earlier entry
    expanded it to "Digitale Netze und Sicherheit", which is simply wrong and would
    have been quoted into a company report as if it came from the ministry."""
    from fue_monitoring.sources import static_programmes

    for entry in static_programmes.fetch("programmes_static.yaml"):
        assert "Digitale Netze und Sicherheit" not in f"{entry.title} {entry.programme} {entry.text}"
