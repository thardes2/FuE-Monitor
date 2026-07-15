"""Import of manually saved search-result pages (e.g. foerderdatenbank.de).

foerderdatenbank.de is protected by bot-detection (Radware/Reblaze) and therefore
can't be queried automatically. Instead: search there in your browser, save the
result page completely (Cmd+S / Ctrl+S, "complete webpage") and drop the .html
file into input_dir. This module reads all HTML files placed there.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..filters import extract_deadline, extract_funding_rate, matched_keywords
from ..models import FundingCall

BASE_URL = "https://www.foerderdatenbank.de"
LINK_MARKER = "/Foerderprogramm/"


def _context_text(link_tag) -> str:
    """Walks up to the surrounding block (e.g. a search-result tile) so we can find
    the deadline/funding rate near the link, regardless of the exact CSS structure."""
    node = link_tag
    for _ in range(4):
        if node.parent is None:
            break
        node = node.parent
        text = node.get_text(" ", strip=True)
        if len(text) > len(link_tag.get_text(strip=True)) + 20:
            return text
    return link_tag.get_text(" ", strip=True)


def _parse_html_file(path: Path, keywords: list[str]) -> list[FundingCall]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    entries: dict[str, FundingCall] = {}

    for a in soup.find_all("a", href=True):
        if LINK_MARKER not in a["href"]:
            continue

        link = urljoin(BASE_URL, a["href"])
        title = a.get_text(" ", strip=True)
        if not title:
            continue

        context = _context_text(a)
        funding_rate, funding_rate_percent = extract_funding_rate(context)
        deadline = extract_deadline(context)
        found_keywords = matched_keywords(f"{title} {context}", keywords)

        notes = []
        if not funding_rate:
            notes.append("funding rate not detected")
        if not deadline:
            notes.append("deadline not detected")

        entry = FundingCall(
            source="Funding Database (Federal/State)",
            title=title,
            funding_rate=funding_rate,
            funding_rate_percent=funding_rate_percent,
            deadline=deadline,
            link=link,
            matched_keywords=found_keywords,
            note="; ".join(notes) + (" -> please check manually" if notes else ""),
        )
        entries[entry.dedup_key] = entry

    return list(entries.values())


def fetch(input_dir: str, keywords: list[str]) -> list[FundingCall]:
    directory = Path(input_dir)
    if not directory.exists():
        return []

    results: dict[str, FundingCall] = {}
    for path in sorted(directory.glob("*.htm*")):
        for entry in _parse_html_file(path, keywords):
            results[entry.dedup_key] = entry

    return list(results.values())
