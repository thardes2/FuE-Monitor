# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Theme suggestions: only terms the prefilter can actually use come back."""

from unittest import mock

import pytest

from fue_monitoring import api, matching
from fue_monitoring.profiles import Theme

CONFIG = {"llm": {"enabled": True, "backend": "ollama",
                  "ollama": {"base_url": "http://ollama.invalid", "model": "m"}}}


def suggest(answer, **kw):
    with mock.patch("fue_monitoring.llm_extract.run_json", return_value=answer) as model:
        result = api.suggest_themes(CONFIG, "Fachgruppe X", "Forscht an Lieferketten.", "SBOMs, SLSA", **kw)
    return result, model


def test_good_terms_come_back_best_weight_first():
    result, _ = suggest({"themen": [
        {"begriff": "Usable Security", "gewicht": 2},
        {"begriff": "Software-Lieferkette*", "gewicht": 3},
        {"begriff": "SBOM", "gewicht": 3},
    ]})
    assert result == [Theme("Software-Lieferkette*", 3), Theme("SBOM", 3), Theme("Usable Security*", 2)]


@pytest.mark.parametrize("begriff", [
    "Security- und Privacy-Ratschläge für Endnutzer",  # a description, not a term
    "Forschung", "Innovation*", "digitalisierung", "Sicherheit*",  # in nearly every call
    "Lie*fer", "KI?", "",  # nothing the prefilter can match
])
def test_unusable_terms_are_dropped(begriff):
    result, _ = suggest({"themen": [{"begriff": begriff, "gewicht": 3}, {"begriff": "SBOM", "gewicht": 1}]})
    assert result == [Theme("SBOM", 1)]


def test_duplicates_in_other_spelling_and_star_are_merged():
    result, _ = suggest({"themen": [
        {"begriff": "Mobilität*", "gewicht": 3}, {"begriff": "mobilität", "gewicht": 1},
    ]})
    assert result == [Theme("Mobilität*", 3)]


def test_weight_is_clamped_and_count_respected():
    result, _ = suggest({"themen": [{"begriff": f"Begriff{i}", "gewicht": 9} for i in range(30)]}, count=5)
    assert len(result) == 5 and {t.gewicht for t in result} == {3}


def test_every_suggestion_works_in_the_prefilter():
    result, _ = suggest({"themen": [{"begriff": "*absicherung*", "gewicht": 2}]})
    assert matching.theme_pattern(result[0].begriff).search("Fahrzeugabsicherungen")


def test_no_answer_is_none_not_empty():
    result, _ = suggest(None)
    assert result is None


def test_material_reaches_the_prompt_and_is_capped():
    _, model = suggest({"themen": []})
    assert "SBOMs, SLSA" in model.call_args.args[0]
    with mock.patch("fue_monitoring.llm_extract.run_json", return_value={"themen": []}) as model:
        api.suggest_themes(CONFIG, "X", "", "A" * 50 + "ENDE", max_text_chars=50)
    assert "ENDE" not in model.call_args.args[0]


def test_hosted_backend_needs_consent():
    gemini = {"llm": {"enabled": True, "backend": "gemini", "gemini": {"api_key": "k"}}}
    with pytest.raises(api.BackendNotLocalError):
        api.suggest_themes(gemini, "X", "", "")
    with mock.patch("fue_monitoring.llm_extract.run_json", return_value={"themen": []}):
        assert api.suggest_themes(gemini, "X", "", "", allow_cloud=True) == []


def test_without_llm_it_says_so():
    with pytest.raises(api.BackendNotLocalError):
        api.suggest_themes({"llm": {"enabled": False}}, "X", "", "")


@pytest.mark.parametrize("begriff, expected", [
    ("Software-Lieferkette", "Software-Lieferkette*"),  # must find the plural and compounds
    ("SBOM", "SBOM"),  # acronym stays strict
    ("KI", "KI"),
    ("Ethik", "Ethik"),  # short word stays strict
    ("Mobilität*", "Mobilität*"),  # already starred
    ("Quantencomputing", "Quantencomputing*"),
])
def test_star_is_added_to_long_words(begriff, expected):
    result, _ = suggest({"themen": [{"begriff": begriff, "gewicht": 2}]})
    assert result == [Theme(expected, 2)]


def test_starred_suggestion_finds_the_plural():
    result, _ = suggest({"themen": [{"begriff": "Software-Lieferkette", "gewicht": 3}]})
    assert matching.theme_pattern(result[0].begriff).search("sichere Software-Lieferketten")
