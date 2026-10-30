# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""The promises `fue_monitoring.api` makes to a second application.

The CLI is one consumer of this package; a web service is meant to be another. What
is tested here is the contract that makes the second possible at all: the package
configures no logging, writes no files unless asked, and takes its configuration
from wherever the caller keeps it.
"""

from __future__ import annotations

import importlib
import logging
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from fue_monitoring import api, pipeline

REPO = Path(__file__).resolve().parent.parent


# --- the surface -----------------------------------------------------------


def test_every_exported_name_exists():
    """__all__ is the contract; a name in it that doesn't resolve is a broken promise."""
    missing = [name for name in api.__all__ if not hasattr(api, name)]
    assert not missing, f"api.__all__ names nothing: {missing}"


def test_the_package_states_a_version():
    import fue_monitoring

    assert fue_monitoring.__version__


@pytest.mark.parametrize("name", ["collect", "match_profiles", "running_profiles",
                                  "load_config", "render_markdown", "CollectionResult",
                                  "ProfileResult", "FundingCall", "Profile"])
def test_the_essentials_are_exported(name):
    """A consumer that has to reach into fue_monitoring.pipeline or .models breaks
    the next time those move, which is exactly what this module exists to prevent."""
    assert name in api.__all__


# --- logging stays the host's business ------------------------------------


def test_importing_the_package_attaches_no_handler():
    """A library that configures logging on import takes over its host's output.
    Only the CLI may attach a handler — see main.configure_cli_logging."""
    logger = logging.getLogger("fue_monitoring")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    importlib.reload(importlib.import_module("fue_monitoring"))
    importlib.reload(importlib.import_module("fue_monitoring.api"))

    assert logging.getLogger("fue_monitoring").handlers == []


def test_importing_the_package_does_not_touch_the_root_logger():
    root_before = (logging.getLogger().level, list(logging.getLogger().handlers))
    importlib.reload(importlib.import_module("fue_monitoring.api"))
    assert (logging.getLogger().level, list(logging.getLogger().handlers)) == root_before


def test_progress_reaches_a_handler_the_caller_attaches(config, monkeypatch, caplog):
    monkeypatch.setattr(pipeline, "collect", lambda cfg: pipeline.CollectionResult())
    with caplog.at_level(logging.INFO, logger="fue_monitoring.pipeline"):
        pipeline.collect(config)
    # The point is not this one message but that records carry the package's name,
    # so a host can filter and route them.
    assert all(r.name.startswith("fue_monitoring") for r in caplog.records)


def test_the_cli_handler_does_not_leak_into_the_host(monkeypatch):
    """configure_cli_logging must stop at the package's own logger."""
    from fue_monitoring import main

    root_handlers = list(logging.getLogger().handlers)
    main.configure_cli_logging()
    try:
        package = logging.getLogger("fue_monitoring")
        assert len(package.handlers) == 1
        assert package.propagate is False, "otherwise a configured host prints everything twice"
        assert list(logging.getLogger().handlers) == root_handlers
    finally:
        package.handlers.clear()
        package.propagate = True


# --- configuration comes from the caller ----------------------------------


def minimal_config(tmp_path: Path) -> Path:
    source = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(source, allow_unicode=True), encoding="utf-8")
    return path


def test_a_config_can_live_anywhere(tmp_path):
    """A host application does not keep its settings next to our source."""
    loaded = api.load_config(minimal_config(tmp_path))
    assert loaded["cache"]["path"].startswith(str(tmp_path))


def test_overrides_beat_the_file(tmp_path):
    loaded = api.load_config(minimal_config(tmp_path),
                             overrides={"profiles": {"min_score": 77}})
    assert loaded["profiles"]["min_score"] == 77


def test_overrides_are_merged_not_replaced(tmp_path):
    """Overriding one key under llm must not drop the rest of the section."""
    loaded = api.load_config(minimal_config(tmp_path), overrides={"llm": {"backend": "ollama"}})
    assert loaded["llm"]["backend"] == "ollama"
    assert loaded["llm"]["ollama"]["model"], "the untouched sections must survive"
    assert "gemini" in loaded["llm"]


def test_a_relative_path_in_an_override_still_gets_resolved(tmp_path):
    """Overrides are applied before paths are made absolute, so a caller may pass a
    relative path exactly as the file does."""
    loaded = api.load_config(minimal_config(tmp_path),
                             overrides={"cache": {"path": "somewhere/pages.json"}})
    assert loaded["cache"]["path"] == str(tmp_path / "somewhere/pages.json")


# --- nothing is written unless asked --------------------------------------


def test_collect_and_match_write_nothing(tmp_path, monkeypatch):
    """The whole reason a web service can use this: no writable directory needed."""
    from fue_monitoring.profiles import Profile, Theme

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = api.load_config(minimal_config(tmp_path), overrides={
        # A local backend that is never reached: there are no calls to evaluate, so
        # nothing is sent anywhere, and the consent gate is satisfied either way.
        "llm": {"enabled": True, "backend": "ollama",
                "ollama": {"base_url": "http://127.0.0.1:1", "model": "x"}},
        "eu_portal": {"enabled": False}, "manual_import": {"enabled": False},
        "gov_bekanntmachungen": {"enabled": False}, "foerderinfo_newsletter": {"enabled": False},
        "profiles": {"static_catalogue": "/nonexistent", "output_dir": str(workspace)},
        "review": {"enabled": False},
        "cache": {"enabled": False},
    })

    collection = api.collect(config)
    profile = Profile(id="p", name="P", groesse="kmu", themen=[Theme("KI", 3)])
    results = api.match_profiles(config, collection, [profile])

    assert list(workspace.iterdir()) == [], "match_profiles must not write files"
    assert len(results) == 1


def test_rendering_returns_strings_rather_than_paths():
    from fue_monitoring.profiles import Profile, Theme

    profile = Profile(id="p", name="P", groesse="kmu", themen=[Theme("KI", 3)])
    assert api.render_markdown(profile, []).startswith("#")
    assert "<html" in api.render_html(profile, []).lower()


def test_writing_reports_is_available_for_callers_that_want_files(tmp_path):
    from fue_monitoring.profiles import Profile, Theme

    profile = Profile(id="p", name="P", groesse="kmu", themen=[Theme("KI", 3)])
    md_path, html_path = api.write_reports(profile, [], tmp_path)
    assert md_path.exists() and html_path.exists()


# --- the CLI is unchanged --------------------------------------------------


def test_the_cli_still_accepts_its_arguments():
    from fue_monitoring import main

    parsed = main.build_parser().parse_args(["--profile", "beispielbau", "-v"])
    assert parsed.profile == "beispielbau" and parsed.verbose is True


def test_the_module_entry_point_works():
    """`python -m fue_monitoring` must behave like the old
    `python -m fue_monitoring.main`."""
    result = subprocess.run([sys.executable, "-m", "fue_monitoring", "--help"],
                            capture_output=True, text=True, cwd=REPO, timeout=60)
    assert result.returncode == 0
    assert "--profiles" in result.stdout


def test_the_console_script_target_is_importable():
    """pyproject points fue-monitor at this exact callable."""
    from fue_monitoring.main import main as entry

    assert callable(entry)


# --- the pipeline against the real matcher --------------------------------


def test_match_profiles_drives_the_real_matcher_and_caches_evaluations(tmp_path, monkeypatch):
    """Regression. Every other test stubs matching.match_profile, so all 329 stayed
    green while match_profiles handed it a PageCache where a MatchCache belongs —
    the two share a put() name and nothing else. The failure only showed when the
    library was driven end to end."""
    from fue_monitoring import llm_extract
    from fue_monitoring.models import FundingCall
    from fue_monitoring.profiles import Profile, Theme

    call = FundingCall(source="S", title="Förderung von KI-Methoden", text="Künstliche Intelligenz",
                       link="http://x/call", submission_mode="continuous")
    profile = Profile(id="p", name="P", groesse="kmu", themen=[Theme("KI", 3)], cloud_erlaubt=True)

    monkeypatch.setattr(llm_extract, "run_json", lambda *a, **k: {
        "score": 80, "begruendung": "passt", "foerderfaehig": "ja",
        "foerderfaehig_grund": None, "fundstelle": "Künstliche Intelligenz",
    })

    config = api.load_config(minimal_config(tmp_path), overrides={
        "llm": {"enabled": True, "backend": "ollama",
                "ollama": {"base_url": "http://127.0.0.1:1", "model": "x"}},
        "profiles": {"min_score": 0, "cache_path": str(tmp_path / "matches.json")},
        "review": {"enabled": False},
        "cache": {"enabled": True},
    })
    collection = pipeline.CollectionResult(calls=[call], open_calls=[call])

    results = pipeline.match_profiles(config, collection, [profile])

    assert [m.score for m in results[0].matches] == [80]
    assert (tmp_path / "matches.json").exists(), "the evaluation must have been cached"

    # And a second run must reuse it rather than paying for inference again.
    monkeypatch.setattr(llm_extract, "run_json",
                        lambda *a, **k: pytest.fail("evaluated again instead of using the cache"))
    again = pipeline.match_profiles(config, collection, [profile])
    assert [m.score for m in again[0].matches] == [80]


def test_a_caller_supplied_match_cache_is_not_written_by_us(tmp_path, monkeypatch):
    """A cache the caller passes is theirs to persist — the path may be read-only,
    or they may want to decide when it lands."""
    from fue_monitoring import llm_extract, matching
    from fue_monitoring.models import FundingCall
    from fue_monitoring.profiles import Profile, Theme

    monkeypatch.setattr(llm_extract, "run_json", lambda *a, **k: {
        "score": 50, "begruendung": "x", "foerderfaehig": "ja",
        "foerderfaehig_grund": None, "fundstelle": None,
    })
    config = api.load_config(minimal_config(tmp_path), overrides={
        "llm": {"enabled": True, "backend": "ollama",
                "ollama": {"base_url": "http://127.0.0.1:1", "model": "x"}},
        "profiles": {"min_score": 0},
        "review": {"enabled": False},
    })
    call = FundingCall(source="S", title="KI", text="KI", link="http://x/c",
                       submission_mode="continuous")
    profile = Profile(id="p", name="P", groesse="kmu", themen=[Theme("KI", 3)], cloud_erlaubt=True)

    theirs = tmp_path / "theirs.json"
    cache = matching.MatchCache(theirs)
    pipeline.match_profiles(config, pipeline.CollectionResult(calls=[call], open_calls=[call]),
                            [profile], cache=cache)

    assert not theirs.exists(), "we must not save a cache we were handed"
    cache.save()
    assert theirs.exists(), "and the caller can still save it themselves"


def test_the_wrong_cache_is_refused_with_a_useful_message(tmp_path):
    """It used to fail several frames deep with "put() missing 4 required positional
    arguments", which tells a consumer nothing about what they got wrong."""
    config = api.load_config(minimal_config(tmp_path))
    with pytest.raises(TypeError) as exc:
        pipeline.match_profiles(config, pipeline.CollectionResult(), [],
                                cache=api.PageCache(tmp_path / "pages.json"))

    message = str(exc.value)
    assert "MatchCache" in message and "PageCache" in message
    assert "collect()" in message, "say where the page cache belongs instead"


def test_evaluations_already_paid_for_survive_a_later_failure(tmp_path, monkeypatch):
    """One profile failing must not throw away what the profiles before it cost.
    The old code saved the cache in a finally block; the refactor briefly didn't."""
    from fue_monitoring import llm_extract, matching
    from fue_monitoring.models import FundingCall
    from fue_monitoring.profiles import Profile, Theme

    monkeypatch.setattr(llm_extract, "run_json", lambda *a, **k: {
        "score": 60, "begruendung": "x", "foerderfaehig": "ja",
        "foerderfaehig_grund": None, "fundstelle": None,
    })

    good = Profile(id="good", name="G", groesse="kmu", themen=[Theme("KI", 3)], cloud_erlaubt=True)
    bad = Profile(id="bad", name="B", groesse="kmu", themen=[Theme("KI", 3)], cloud_erlaubt=True)

    real_prefilter = matching.prefilter

    def exploding(call, profile):
        if profile.id == "bad":
            raise RuntimeError("boom")
        return real_prefilter(call, profile)

    monkeypatch.setattr(matching, "prefilter", exploding)

    cache_path = tmp_path / "matches.json"
    config = api.load_config(minimal_config(tmp_path), overrides={
        "llm": {"enabled": True, "backend": "ollama",
                "ollama": {"base_url": "http://127.0.0.1:1", "model": "x"}},
        "profiles": {"min_score": 0, "cache_path": str(cache_path)},
        "review": {"enabled": False},
        "cache": {"enabled": True},
    })
    call = FundingCall(source="S", title="KI", text="KI", link="http://x/c",
                       submission_mode="continuous")
    collection = pipeline.CollectionResult(calls=[call], open_calls=[call])

    with pytest.raises(RuntimeError):
        pipeline.match_profiles(config, collection, [good, bad])

    assert cache_path.exists(), "the first profile's evaluation must have been saved"
    assert matching.MatchCache(cache_path).get(
        matching.cache_key("http://x/c", good.hash, "ollama/x")
    ) is not None
