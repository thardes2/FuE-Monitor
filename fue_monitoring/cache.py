# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""On-disk cache of already-fetched announcement pages.

Discovery finds far more announcements than a single run can afford to process
from scratch: every uncached page costs its site's robots.txt Crawl-delay (30s for
bmftr.bund.de) plus an LLM call. Announcement documents are static once published,
so there's no reason to pay that twice.

What's cached is the page text plus the extracted fields — deliberately *not* the
keyword matches, since those depend on the current config and are recomputed on
every run. That way editing `keywords` takes effect immediately without
invalidating the cache.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from . import llm_extract

log = logging.getLogger(__name__)


class PageCache:
    def __init__(self, path: str | Path, max_age_days: int = 30, enabled: bool = True):
        self.path = Path(path)
        self.max_age = timedelta(days=max_age_days)
        self.enabled = enabled
        self._entries: dict[str, dict] = {}
        self._dirty = False
        # Which URLs this run actually wrote. save() only applies these, so a run
        # never pushes its start-of-run snapshot over entries somebody else added
        # meanwhile — see save() for why that matters.
        self._touched: set[str] = set()

        if self.enabled:
            self._entries = self._read()

    def _read(self) -> dict[str, dict]:
        """Reads the cache file, treating an unusable one as empty."""
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(f"  (cache at {self.path} unreadable, starting empty: {exc})")
            return {}

    def get(self, url: str) -> dict | None:
        """Returns {text, title, funding_rate, funding_rate_percent, deadline} or None."""
        if not self.enabled:
            return None

        entry = self._entries.get(url)
        if not entry:
            return None

        try:
            fetched_at = datetime.fromisoformat(entry["fetched_at"])
        except (KeyError, ValueError):
            return None
        if datetime.now() - fetched_at > self.max_age:
            return None

        data = dict(entry)
        data.pop("fetched_at", None)
        data.setdefault("llm_model", "")
        data.setdefault("llm_failures", 0)
        data.setdefault("submission_mode", "")

        # Which submission date matters depends on when you ask, so the effective
        # deadline is re-picked from the stored list on every read. That lets a
        # cached recurring call advance to its next Stichtag instead of expiring.
        data["deadlines"] = [date.fromisoformat(d) for d in data.get("deadlines", [])]
        if data["deadlines"]:
            data["deadline"] = llm_extract.pick_deadline(data["deadlines"])
        else:
            raw_deadline = data.get("deadline")
            data["deadline"] = date.fromisoformat(raw_deadline) if raw_deadline else None
        return data

    def put(
        self,
        url: str,
        text: str,
        title: str,
        funding_rate: str,
        funding_rate_percent: float | None,
        deadline: date | None,
        model: str = "",
        deadlines: list[date] | None = None,
        submission_mode: str = "",
    ) -> None:
        if not self.enabled:
            return

        self._entries[url] = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            # Which model derived the fields below. Kept as a value rather than
            # folded into the key: the page text doesn't depend on the model, so a
            # backend switch should re-derive from the stored text, not re-crawl
            # every page at its site's Crawl-delay.
            "llm_model": model,
            "llm_failures": 0,
            "text": text,
            "title": title,
            "funding_rate": funding_rate,
            "funding_rate_percent": funding_rate_percent,
            "deadline": deadline.isoformat() if deadline else None,
            "deadlines": [d.isoformat() for d in (deadlines or [])],
            "submission_mode": submission_mode,
        }
        self._touched.add(url)
        self._dirty = True

    def note_failure(self, url: str) -> None:
        """Counts a failed re-extraction without touching the entry's good fields.

        An entry whose re-extraction keeps failing would otherwise be retried on
        every single run, because its recorded model never catches up with the
        configured one. The derived fields stay as they were — stale beats
        replacing them with a regex-only guess.
        """
        entry = self._entries.get(url)
        if not self.enabled or not entry:
            return
        entry["llm_failures"] = entry.get("llm_failures", 0) + 1
        self._touched.add(url)
        self._dirty = True


    def save(self) -> None:
        """Merges this run's entries into whatever is on disk now.

        Writing self._entries wholesale would mean the last run to finish wins:
        a second, concurrently started run holds a snapshot taken before the first
        one saved, so saving it discards everything the first one achieved. That is
        not hypothetical — an interrupted parallel run cost ~30 freshly extracted
        entries their recorded model, because save() runs from a finally block and
        Ctrl-C still reaches it. Re-reading here keeps both runs' work.
        """
        if not self.enabled or not self._dirty:
            return

        merged = self._read()
        merged.update({url: self._entries[url] for url in self._touched if url in self._entries})

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Written via a temp file in the same directory, so an interrupt during the
        # write leaves the old cache intact rather than a truncated one.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

        self._entries = merged
        self._dirty = False
