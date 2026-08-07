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
from datetime import date, datetime, timedelta
from pathlib import Path


class PageCache:
    def __init__(self, path: str | Path, max_age_days: int = 30, enabled: bool = True):
        self.path = Path(path)
        self.max_age = timedelta(days=max_age_days)
        self.enabled = enabled
        self._entries: dict[str, dict] = {}
        self._dirty = False

        if self.enabled and self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                print(f"  (cache at {self.path} unreadable, starting empty: {exc})")
                self._entries = {}

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
    ) -> None:
        if not self.enabled:
            return

        self._entries[url] = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "text": text,
            "title": title,
            "funding_rate": funding_rate,
            "funding_rate_percent": funding_rate_percent,
            "deadline": deadline.isoformat() if deadline else None,
        }
        self._dirty = True

    def save(self) -> None:
        if not self.enabled or not self._dirty:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, ensure_ascii=False), encoding="utf-8")
        self._dirty = False
