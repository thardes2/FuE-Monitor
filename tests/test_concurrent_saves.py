# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Two runs sharing a cache file must not erase each other's work.

Both caches load the whole file at start and used to write it back wholesale, so
the run that finished last silently reverted the other one. Observed in practice:
an interrupted parallel run cost ~30 freshly extracted entries their recorded
model, because save() sits in a finally block and Ctrl-C still reaches it.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from fue_monitoring.cache import PageCache
from fue_monitoring.matching import MatchCache


def put_page(cache: PageCache, url: str, model: str) -> None:
    cache.put(url, f"text of {url}", "T", "60 %", 60.0, date(2026, 4, 30), model=model)


def test_a_later_save_keeps_what_the_other_run_added(tmp_path):
    path = tmp_path / "pages.json"
    first, second = PageCache(path), PageCache(path)   # both start from empty

    put_page(first, "http://x/a", "gemini/m")
    first.save()
    put_page(second, "http://x/b", "ollama/q")
    second.save()                                      # started before first saved

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert set(on_disk) == {"http://x/a", "http://x/b"}


def test_an_untouched_entry_is_not_reverted(tmp_path):
    """The stale copy a long run carries must not overwrite a newer one."""
    path = tmp_path / "pages.json"
    writer = PageCache(path)
    put_page(writer, "http://x/a", "old/model")
    writer.save()

    stale = PageCache(path)                            # holds the old value

    fresh = PageCache(path)
    put_page(fresh, "http://x/a", "new/model")
    fresh.save()

    put_page(stale, "http://x/other", "old/model")     # touches something else
    stale.save()

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["http://x/a"]["llm_model"] == "new/model"
    assert "http://x/other" in on_disk


def test_a_failure_count_survives_a_concurrent_save(tmp_path):
    path = tmp_path / "pages.json"
    writer = PageCache(path)
    put_page(writer, "http://x/a", "m")
    writer.save()

    counter = PageCache(path)
    counter.note_failure("http://x/a")

    other = PageCache(path)
    put_page(other, "http://x/b", "m")
    other.save()

    counter.save()

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["http://x/a"]["llm_failures"] == 1
    assert "http://x/b" in on_disk, "the other run's page must still be there"


def test_two_profiles_evaluated_concurrently_keep_both_results(tmp_path):
    """An evaluation costs an LLM call — unlike a page, it can't just be re-fetched."""
    path = tmp_path / "matches.json"
    a, b = MatchCache(path), MatchCache(path)

    a.put("key-profile-a", {"score": 80})
    b.put("key-profile-b", {"score": 20})
    a.save()
    b.save()

    assert MatchCache(path).get("key-profile-a") == {"score": 80}
    assert MatchCache(path).get("key-profile-b") == {"score": 20}


@pytest.mark.parametrize("cache_class,filename", [(PageCache, "pages.json"), (MatchCache, "matches.json")])
def test_an_unreadable_file_does_not_take_the_save_down(tmp_path, cache_class, filename):
    """save() re-reads the file; a corrupt one must degrade, not raise."""
    path = tmp_path / filename
    cache = cache_class(path)
    if cache_class is PageCache:
        put_page(cache, "http://x/a", "m")
    else:
        cache.put("k", {"score": 1})

    path.write_text("{ not json", encoding="utf-8")
    cache.save()

    assert json.loads(path.read_text(encoding="utf-8")), "this run's work still landed"
