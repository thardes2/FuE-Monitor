# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""University research groups as profiles, and sections of other tools in a profile."""

from unittest import mock

import pytest

from fue_monitoring import matching
from fue_monitoring.models import FundingCall
from fue_monitoring.profiles import FOREIGN_SECTIONS, Profile, ProfileError, Theme, load_profile

LLM = {"backend": "ollama", "ollama": {"base_url": "http://ollama.invalid", "model": "m"}}
GUT = {"score": 70, "begruendung": "passt", "foerderfaehig": "ja", "foerderfaehig_grund": None, "fundstelle": None}


def write(tmp_path, text, name="a.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def lehrstuhl(**kw):
    return Profile(id="upb-x", name="Fachgruppe X", groesse="hochschule",
                   themen=[Theme("KI", 3)], **kw)


def firma():
    return Profile(id="f", name="F GmbH", groesse="kmu", themen=[Theme("KI", 3)])


def call():
    return FundingCall(source="t", title="KI-Aufruf", text="Gefördert wird KI.", link="https://x.invalid/1")


# --- profile file ----------------------------------------------------------


def test_hochschule_is_a_valid_size(tmp_path):
    p = load_profile(write(tmp_path, "id: a\nname: A\ngroesse: Hochschule\nthemen: ['x']\n"))
    assert p.groesse == "hochschule"


@pytest.mark.parametrize("section", sorted(FOREIGN_SECTIONS))
def test_sections_of_other_tools_are_accepted_and_ignored(tmp_path, section):
    plain = load_profile(write(tmp_path, "id: a\nname: A\ngroesse: kmu\nthemen: ['x']\n", "plain.yaml"))
    with_section = load_profile(write(
        tmp_path,
        f"id: a\nname: A\ngroesse: kmu\nthemen: ['x']\n{section}:\n  irgendwas: [1, 2]\n",
        "section.yaml",
    ))
    assert with_section == plain
    # Not part of the fingerprint: editing another tool's section keeps the cache.
    assert with_section.hash == plain.hash


def test_unknown_top_level_key_is_still_refused(tmp_path):
    with pytest.raises(ProfileError, match="unknown key"):
        load_profile(write(tmp_path, "id: a\nname: A\ngroesse: kmu\nthemen: ['x']\nkonsortien: {}\n"))


# --- evaluation prompt -----------------------------------------------------


def test_companies_keep_their_prompt_and_cache_key():
    template, version = matching.prompt_for(firma())
    assert template is matching.PROMPT_TEMPLATE
    assert version == matching.PROMPT_VERSION
    # Existing cache entries were written without a prompt_version argument.
    assert matching.cache_key("u", "h", "m", version) == matching.cache_key("u", "h", "m")


def test_university_gets_its_own_prompt_and_version():
    template, version = matching.prompt_for(lehrstuhl())
    assert template is matching.HOCHSCHULE_PROMPT_TEMPLATE
    assert matching.cache_key("u", "h", "m", version) != matching.cache_key("u", "h", "m")


def test_university_prompt_version_leaves_company_keys_alone(monkeypatch):
    before = matching.cache_key("u", "h", "m", matching.prompt_for(firma())[1])
    monkeypatch.setattr(matching, "HOCHSCHULE_PROMPT_VERSION", "99")
    assert matching.cache_key("u", "h", "m", matching.prompt_for(firma())[1]) == before


def test_university_prompt_needs_no_argument_the_company_prompt_lacks():
    # evaluate() fills both templates with the same arguments; extra ones are
    # ignored by format(), a missing one would raise a KeyError at run time.
    import string
    fields = lambda t: {f for _, f, _, _ in string.Formatter().parse(t) if f}
    assert fields(matching.HOCHSCHULE_PROMPT_TEMPLATE) <= fields(matching.PROMPT_TEMPLATE)


def test_evaluation_of_a_university_uses_the_university_prompt():
    with mock.patch("fue_monitoring.llm_extract.run_json", return_value=GUT) as model:
        matching.evaluate(call(), lehrstuhl(description="Forscht an KI."), [("KI", 3)], LLM)
    prompt = model.call_args.args[0]
    assert "university research group" in prompt
    assert "Forscht an KI." in prompt
    assert "SME under the EU definition" not in prompt


def test_cached_university_evaluation_is_reused(tmp_path):
    cache = matching.MatchCache(tmp_path / "m.json")
    with mock.patch("fue_monitoring.llm_extract.run_json", return_value=GUT) as model:
        matching.match_profile([call()], lehrstuhl(), LLM, cache=cache)
        matching.match_profile([call()], lehrstuhl(), LLM, cache=cache)
    assert model.call_count == 1
