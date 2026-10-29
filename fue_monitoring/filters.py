from __future__ import annotations

import re
from datetime import date, timedelta

from . import llm_extract

PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s?%")
DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b")

# These hint words are deliberately German: the source text they run against
# (foerderdatenbank.de pages, German-language call descriptions) is in German,
# so matching German terms is what actually finds the funding rate / deadline.
FUNDING_RATE_HINT_RE = re.compile(
    r"(Förderquote|Fördersatz|Zuschuss|Beihilfeintensität|Förderhöhe|Erstattung)"
    r"[^.\n]{0,40}?(\d{1,3}(?:[.,]\d+)?)\s?%",
    re.IGNORECASE,
)
DEADLINE_HINT_RE = re.compile(
    r"(Frist|Bewerbungsschluss|Antragsfrist|Einreichungsfrist|Bewerbungsfrist|Stichtag|deadline)"
    r"[^.\n]{0,40}?(\d{1,2})\.(\d{1,2})\.(\d{2,4})",
    re.IGNORECASE,
)


URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def strip_urls(text: str) -> str:
    """Removes URLs before any number extraction runs on the text.

    Scraped announcement pages print full URLs inline, and percent-encoded German
    umlauts inside them look exactly like a percentage to a naive regex: the "Ä" in
    "...-%C3%84nderungsbekanntmachung-..." is the sequence "%C3%84", whose "3"
    followed by "%" was being read as a 3 % funding rate. Dates in URLs
    ("/2023/12/2023-12-07-...") are the same kind of trap.
    """
    return URL_RE.sub(" ", text)


def matched_keywords(text: str, keywords: list[str]) -> list[str]:
    text_low = text.lower()
    return [kw for kw in keywords if kw.lower() in text_low]


def extract_funding_rate(text: str, fallback_to_any_percentage: bool = True) -> tuple[str, float | None]:
    """Looks for a percentage near a typical funding-rate hint word.

    If none is found and fallback_to_any_percentage is True, falls back to the
    first percentage anywhere in the text. That fallback is a coarse guess — an
    announcement may well state some other percentage (a cap on subcontracting,
    a co-financing share) while naming no funding rate at all — so callers that
    have an LLM reading the whole document should pass False and let it decide.
    """
    text = strip_urls(text)

    m = FUNDING_RATE_HINT_RE.search(text)
    if m:
        value = float(m.group(2).replace(",", "."))
        return f"{m.group(2)} %", value

    if fallback_to_any_percentage:
        m = PERCENT_RE.search(text)
        if m:
            value = float(m.group(1).replace(",", "."))
            return f"{m.group(1)} %", value

    return "", None


def extract_deadline(text: str, fallback_to_any_date: bool = True) -> date | None:
    """Looks for a date near a typical deadline hint word. If none is found and
    fallback_to_any_date is True, falls back to the first date found anywhere in
    the text — only safe for sources where that's reliably the deadline, not e.g.
    a publication date printed at the top of the page."""
    text = strip_urls(text)

    m = DEADLINE_HINT_RE.search(text)
    match = m and m.group(2, 3, 4)
    if not match and fallback_to_any_date:
        m2 = DATE_RE.search(text)
        match = m2 and m2.group(1, 2, 3)

    if not match:
        return None

    day, month, year = match
    year = int(year)
    if year < 100:
        year += 2000
    try:
        return date(year, int(month), int(day))
    except ValueError:
        return None


def deadline_status(deadline: date | None, warning_days: int, today: date | None = None) -> str:
    today = today or date.today()
    if deadline is None:
        return "unknown"
    if deadline < today:
        return "expired"
    if deadline <= today + timedelta(days=warning_days):
        return "urgent"
    return "open"


def funding_rate_ok(percent: float | None, min_percent: float) -> bool:
    if min_percent <= 0:
        return True
    if percent is None:
        return True  # unknown -> don't exclude, let the user check manually
    return percent >= min_percent


def deadline_ok(
    deadline: date | None,
    open_only: bool,
    today: date | None = None,
    submission_mode: str = "",
) -> bool:
    """Whether a call still counts as open.

    A missing deadline normally means "unknown", and excluding those would hide
    genuinely open programmes — so they're kept. The exception is a framework
    document: it states no date because no application can be made on it at all,
    the Förderaufrufe published under it carry the dates. Measured on one run, 35 %
    of scraped pages had no date, and letting the framework ones through put
    documents into company reports that nobody can apply to.
    """
    today = today or date.today()
    if not open_only:
        return True
    if deadline is None:
        return submission_mode != llm_extract.FRAMEWORK
    return deadline >= today
