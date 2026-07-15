# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Gemini-based extraction of funding rate / deadline from raw scraped text.

Regex can't reliably handle the many ways German funding-call text phrases a
deadline or funding rate ("Anträge sind bis zum ... einzureichen", "Stichtag ist
der...", "bis zu 100 % der zuwendungsfähigen Ausgaben, in Ausnahmefällen weniger").
This asks Gemini to pull out exactly what's stated, as structured JSON, and
explicitly tells it to return null rather than guess when something isn't stated.
"""

from __future__ import annotations

import json
from datetime import date

import requests
from dateutil import parser as dateparser

API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "funding_rate_percent": {"type": "number", "nullable": True},
        "deadline": {"type": "string", "nullable": True},
        "title": {"type": "string", "nullable": True},
    },
    "required": ["funding_rate_percent", "deadline", "title"],
}

PROMPT_TEMPLATE = """You are extracting structured facts from a German government funding-call announcement.
Only use information literally stated in the text below. If a value isn't stated, return null — never guess or infer.

- funding_rate_percent: the funding rate/quota in percent (Förderquote/Fördersatz), as a number, or null.
- deadline: the date by which applications/project outlines must be submitted (Frist/Bewerbungsschluss/Antragsfrist/Einreichungsfrist/Stichtag/Vorlagefrist/Einreichungstermin), as an ISO date YYYY-MM-DD.
  - Report this date even if the text also says it's non-exclusionary/soft (e.g. "gilt nicht als Ausschlussfrist") — a soft deadline is still a deadline, don't null it out just because late submissions might still be considered.
  - If several recurring submission dates are listed (e.g. one per year), return the latest one mentioned.
  - Only return null if the text truly states no specific date anywhere (fully continuous submission with no dates at all).
- title: a short, clean title for this call if the given title below is messy or unclear, otherwise null.

Given title: {title}

Text:
{text}
"""


def extract(text: str, title: str, api_key: str, model: str) -> dict | None:
    if not api_key:
        return None

    url = API_URL_TEMPLATE.format(model=model)
    # BMFTR "Bekanntmachungen" often only state the funding rate/deadline in later
    # sections (Art/Höhe der Zuwendung, Verfahren), well past the first 15k chars a
    # tighter cutoff used to allow — the flash models support up to 1M input tokens,
    # so there's no real need to cut a ~50k-char government notice short.
    payload = {
        "contents": [{"parts": [{"text": PROMPT_TEMPLATE.format(title=title, text=text[:200000])}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }

    try:
        resp = requests.post(url, params={"key": api_key}, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(raw)
    except (requests.exceptions.RequestException, KeyError, IndexError, json.JSONDecodeError) as exc:
        print(f"  (Gemini extraction failed, falling back to regex: {exc})")
        return None


def merge(result: dict | None, title: str, funding_rate: str, funding_rate_percent: float | None, deadline: date | None):
    """Overrides the regex-based fields with the LLM's, field by field, wherever it found something."""
    if not result:
        return title, funding_rate, funding_rate_percent, deadline

    if result.get("title"):
        title = result["title"]

    pct = result.get("funding_rate_percent")
    if pct is not None:
        funding_rate_percent = float(pct)
        funding_rate = f"{pct:g} %"

    raw_deadline = result.get("deadline")
    if raw_deadline:
        try:
            # Gemini is asked for ISO dates but doesn't always comply (sometimes
            # returns German DD.MM.YYYY instead), so parse leniently rather than
            # discarding a correctly-found deadline over a formatting mismatch.
            deadline = dateparser.parse(raw_deadline, dayfirst=True).date()
        except (ValueError, OverflowError):
            pass

    return title, funding_rate, funding_rate_percent, deadline
