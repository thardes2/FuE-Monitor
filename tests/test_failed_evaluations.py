# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""A failed LLM evaluation must be reported, not silently read as "no match"."""

from unittest import mock

from fue_monitoring import matching, pipeline, profile_report
from fue_monitoring.models import FundingCall
from fue_monitoring.profiles import Profile, Theme


def _profile():
    return Profile(id="p", name="P GmbH", groesse="kmu", themen=[Theme(begriff="KI", gewicht=3)])


def _call(n):
    return FundingCall(source="t", title=f"KI-Aufruf {n}", text="Gefördert wird KI.", link=f"https://x.invalid/{n}")


LLM = {"backend": "ollama", "ollama": {"base_url": "http://ollama.invalid", "model": "m"}}
GUT = {"score": 80, "begruendung": "passt", "foerderfaehig": "ja", "foerderfaehig_grund": "", "fundstelle": ""}


def test_failed_evaluations_are_collected():
    calls = [_call(1), _call(2)]
    cache = matching.MatchCache("/nonexistent/matches.json", enabled=False)
    failed = []
    # First call answers, second hits a rate limit (run_json returns None).
    with mock.patch("fue_monitoring.llm_extract.run_json", side_effect=[GUT, None]):
        matches = matching.match_profile(calls, _profile(), LLM, cache=cache, failed=failed)
    assert [m.call.link for m in matches] == ["https://x.invalid/1"]
    assert [c.link for c in failed] == ["https://x.invalid/2"]


def test_a_failed_evaluation_is_not_cached_and_is_retried_next_run(tmp_path):
    """With a real cache: the next run asks the model about the failed call only.
    A cached failure would keep the call out of every report until the entry expired."""
    calls = [_call(1), _call(2)]
    cache = matching.MatchCache(tmp_path / "matches.json")
    with mock.patch("fue_monitoring.llm_extract.run_json", side_effect=[GUT, None]):
        matching.match_profile(calls, _profile(), LLM, cache=cache, failed=[])

    failed = []
    with mock.patch("fue_monitoring.llm_extract.run_json", return_value=GUT) as model:
        matches = matching.match_profile(calls, _profile(), LLM, cache=cache, failed=failed)

    assert model.call_count == 1, "only the call that failed is evaluated again"
    assert sorted(m.call.link for m in matches) == ["https://x.invalid/1", "https://x.invalid/2"]
    assert failed == []


def test_failed_is_optional():
    with mock.patch("fue_monitoring.llm_extract.run_json", return_value=None):
        assert matching.match_profile([_call(1)], _profile(), LLM) == []


def test_the_pipeline_hands_failed_calls_to_the_profile_result(tmp_path, monkeypatch):
    def match_profile(calls, profile, llm_config, failed=None, **kwargs):
        failed.extend(calls)
        return []

    monkeypatch.setattr(matching, "match_profile", match_profile)
    config = {
        "llm": {"enabled": False},
        "profiles": {"min_score": 0, "max_evaluations_per_profile": None, "max_text_chars": 1000,
                     "review_state_template": str(tmp_path / "review_{profile}.json")},
        "review": {"enabled": False, "hide_dismissed": False, "dismissed_values": []},
    }
    collection = pipeline.CollectionResult(open_calls=[_call(1)])

    [result] = pipeline._match_each(config, collection, [_profile()], cache=None)

    assert [c.link for c in result.failed] == ["https://x.invalid/1"]


# --- the report itself says it is incomplete --------------------------------


def test_the_report_names_the_calls_it_could_not_evaluate():
    """The CLI's warning goes to a terminal; the report is what gets passed on."""
    failed = [_call(2)]
    markdown = profile_report.render_markdown(_profile(), [], failed=failed)
    page = profile_report.render_html(_profile(), [], failed=failed)

    for text in (markdown, page):
        assert "Unvollständig: 1 thematisch passende Ausschreibung(en)" in text
        assert "https://x.invalid/2" in text
    assert markdown.index("Unvollständig") < markdown.index("Keine Treffer")


def test_a_complete_report_carries_no_such_note():
    assert "Unvollständig" not in profile_report.render_markdown(_profile(), [])
    assert "Unvollständig" not in profile_report.render_html(_profile(), [])


def test_write_reports_passes_failed_through(tmp_path):
    md_path, html_path = profile_report.write_reports(_profile(), [], tmp_path, failed=[_call(2)])
    assert "Unvollständig" in md_path.read_text(encoding="utf-8")
    assert "Unvollständig" in html_path.read_text(encoding="utf-8")
