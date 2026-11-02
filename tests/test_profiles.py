# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Loading, validating and fingerprinting company profiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from fue_monitoring.profiles import Profile, ProfileError, Theme, load_profile, load_profiles

VALID = """
id: musterwerk
name: Musterwerk GmbH
groesse: kmu
description: Entwicklungspartner
themen:
  - begriff: "KI"
    gewicht: 3
  - begriff: "Mobilität*"
    gewicht: 1
ausschluss:
  - "Medizinprodukt*"
programme_bevorzugt: ["ZIM"]
eigenanteil_max_prozent: 50
sicp_ansprechpartner: Max Mustermann
"""


def write(tmp_path: Path, text: str, name: str = "p.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_all_fields(tmp_path):
    p = load_profile(write(tmp_path, VALID))
    assert (p.id, p.name, p.groesse, p.description) == ("musterwerk", "Musterwerk GmbH", "kmu", "Entwicklungspartner")
    assert p.themen == [Theme("KI", 3), Theme("Mobilität*", 1)]
    assert p.ausschluss == ["Medizinprodukt*"]
    assert p.programme_bevorzugt == ["ZIM"]
    assert p.eigenanteil_max_prozent == 50.0
    assert p.sicp_ansprechpartner == "Max Mustermann"


def test_bare_string_theme_defaults_to_weight_one(tmp_path):
    p = load_profile(write(tmp_path, "id: a\nname: A\ngroesse: kmu\nthemen: ['Robotik*']\n"))
    assert p.themen == [Theme("Robotik*", 1)]


@pytest.mark.parametrize(
    "yaml_text, expected",
    [
        ("name: A\ngroesse: kmu\nthemen: ['x']\n", "'id' is required"),
        ("id: a\ngroesse: kmu\nthemen: ['x']\n", "'name' is required"),
        ("id: a\nname: A\nthemen: ['x']\n", "'groesse' is required"),
        ("id: a\nname: A\ngroesse: mittel\nthemen: ['x']\n", "'groesse' must be one of"),
        ("id: a\nname: A\ngroesse: kmu\n", "at least one entry under 'themen'"),
        ("id: a\nname: A\ngroesse: kmu\nthemen: [{begriff: x, gewicht: 7}]\n", "between 1 and 3"),
        ("id: a\nname: A\ngroesse: kmu\nthemen: [{gewicht: 2}]\n", "needs a 'begriff'"),
        ("id: a/b\nname: A\ngroesse: kmu\nthemen: ['x']\n", "must not contain slashes"),
        ("id: a\nname: A\ngroesse: kmu\nthemen: ['x']\neigenanteil_max_prozent: viel\n", "must be a number"),
    ],
)
def test_invalid_profiles_fail_with_a_useful_message(tmp_path, yaml_text, expected):
    with pytest.raises(ProfileError) as exc:
        load_profile(write(tmp_path, yaml_text))
    assert expected in str(exc.value)


def test_broken_yaml_names_the_file(tmp_path):
    with pytest.raises(ProfileError) as exc:
        load_profile(write(tmp_path, "id: [unclosed\n", name="kaputt.yaml"))
    assert "kaputt.yaml" in str(exc.value)


def test_groesse_is_case_insensitive(tmp_path):
    assert load_profile(write(tmp_path, "id: a\nname: A\ngroesse: KMU\nthemen: ['x']\n")).groesse == "kmu"


# --- directory loading -----------------------------------------------------


def test_loads_every_profile_in_the_directory(tmp_path):
    write(tmp_path, VALID, "a.yaml")
    write(tmp_path, "id: zweite\nname: Zweite AG\ngroesse: grossunternehmen\nthemen: ['Logistik*']\n", "b.yaml")
    assert sorted(p.id for p in load_profiles(tmp_path)) == ["musterwerk", "zweite"]


def test_only_selects_a_single_profile(tmp_path):
    write(tmp_path, VALID, "a.yaml")
    write(tmp_path, "id: zweite\nname: Z\ngroesse: kmu\nthemen: ['x']\n", "b.yaml")
    assert [p.id for p in load_profiles(tmp_path, only="zweite")] == ["zweite"]


def test_unknown_id_lists_what_is_available(tmp_path):
    write(tmp_path, VALID, "a.yaml")
    with pytest.raises(ProfileError) as exc:
        load_profiles(tmp_path, only="gibtsnicht")
    assert "musterwerk" in str(exc.value)


def test_duplicate_ids_are_rejected(tmp_path):
    write(tmp_path, VALID, "a.yaml")
    write(tmp_path, VALID, "b.yaml")
    with pytest.raises(ProfileError) as exc:
        load_profiles(tmp_path)
    assert "duplicate" in str(exc.value)


def test_a_broken_file_fails_the_run_rather_than_being_skipped(tmp_path):
    """A skipped profile is indistinguishable from "no matches this week"."""
    write(tmp_path, VALID, "a.yaml")
    write(tmp_path, "id: b\nname: B\ngroesse: unsinn\nthemen: ['x']\n", "b.yaml")
    with pytest.raises(ProfileError):
        load_profiles(tmp_path)


def test_missing_directory_is_an_error(tmp_path):
    with pytest.raises(ProfileError):
        load_profiles(tmp_path / "nope")


# --- hash: the cache key's stability contract ------------------------------


def base() -> Profile:
    return Profile(id="a", name="A", groesse="kmu", description="Partner",
                   themen=[Theme("KI", 3)], ausschluss=["Wehrtechnik*"],
                   eigenanteil_max_prozent=50, sicp_ansprechpartner="Max")


@pytest.mark.parametrize(
    "change",
    [
        lambda p: setattr(p, "name", "Anderer Name"),
        lambda p: setattr(p, "sicp_ansprechpartner", "Erika Musterfrau"),
        lambda p: setattr(p, "id", "andere-id"),
    ],
)
def test_cosmetic_changes_keep_cached_evaluations(change):
    """Renaming a contact must not throw away hours of local inference."""
    p = base()
    before = p.hash
    change(p)
    assert p.hash == before


@pytest.mark.parametrize(
    "change",
    [
        lambda p: setattr(p, "themen", [Theme("KI", 2)]),  # weight changed
        lambda p: setattr(p, "themen", [Theme("Robotik*", 3)]),  # term changed
        lambda p: setattr(p, "ausschluss", []),
        lambda p: setattr(p, "groesse", "grossunternehmen"),
        lambda p: setattr(p, "description", "Anwender"),
        lambda p: setattr(p, "eigenanteil_max_prozent", 30),
    ],
)
def test_matching_relevant_changes_invalidate_the_cache(change):
    p = base()
    before = p.hash
    change(p)
    assert p.hash != before


def test_renaming_rolle_to_description_kept_every_hash():
    """Pinned to the value the code produced while the field was still "rolle".
    A changed hash would have discarded every cached evaluation — one LLM call
    each — for a change of name only."""
    assert base().hash == "becffca94027410d"


def test_the_old_key_rolle_is_refused_with_its_new_name(tmp_path):
    """The general "unknown key" message would list every supported key and leave
    the reader to work out which one was meant."""
    old = VALID.replace("description:", "rolle:")
    with pytest.raises(ProfileError, match="'rolle' has been renamed to 'description'"):
        load_profile(write(tmp_path, old))


def test_theme_order_does_not_matter():
    a = Profile(id="a", name="A", groesse="kmu", themen=[Theme("KI", 3), Theme("Robotik*", 1)])
    b = Profile(id="a", name="A", groesse="kmu", themen=[Theme("Robotik*", 1), Theme("KI", 3)])
    assert a.hash == b.hash


def test_shipped_example_profile_is_valid():
    """The one profile in the repo has to load — it's the template users copy."""
    example = Path(__file__).resolve().parent.parent / "profiles" / "example.yaml"
    assert load_profile(example).id == "musterwerk"
