# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""LLM-based extraction of funding rate / deadline from raw scraped text.

Regex can't reliably handle the many ways German funding-call text phrases a
deadline or funding rate ("Anträge sind bis zum ... einzureichen", "Stichtag ist
der...", "bis zu 100 % der zuwendungsfähigen Ausgaben, in Ausnahmefällen weniger").
This asks an LLM to pull out exactly what's stated, as structured JSON, and
explicitly tells it to return null rather than guess when something isn't stated.

Two interchangeable backends, selected via llm.backend in config.yaml:
  - "gemini": Google's hosted API. Needs an API key; text leaves the machine.
  - "ollama": a local Ollama instance. No API key, no quota, nothing sent to a
    third party — but only as fast as the local hardware.
Both are asked for the same JSON shape, so the rest of the pipeline doesn't care
which one produced a result.
"""

from __future__ import annotations

import json
import re
from datetime import date

import requests
from dateutil import parser as dateparser

GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# BMFTR "Bekanntmachungen" often only state the funding rate/deadline in later
# sections (Art/Höhe der Zuwendung, Verfahren), well past the ~15k chars a tighter
# cutoff used to allow. Both backends handle far more than a ~50k-char government
# notice (Gemini flash: 1M tokens, qwen3.6 on Ollama: 262k), so cut generously.
MAX_TEXT_CHARS = 200000

# Gemini uses OpenAPI-flavoured schemas ("nullable"), Ollama takes plain JSON
# Schema (union with "null"), so the same shape has to be spelled twice.
GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "funding_rate_percent": {"type": "number", "nullable": True},
        "deadline": {"type": "string", "nullable": True},
        "title": {"type": "string", "nullable": True},
    },
    "required": ["funding_rate_percent", "deadline", "title"],
}

OLLAMA_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "funding_rate_percent": {"type": ["number", "null"]},
        "deadline": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
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


def is_configured(llm_config: dict | None) -> bool:
    """Whether the selected backend has everything it needs to actually run.

    Checked once up front so a missing API key doesn't produce one warning per
    scraped page.
    """
    if not llm_config or not llm_config.get("enabled"):
        return False

    backend = llm_config.get("backend")
    if backend == "gemini":
        return bool(llm_config.get("gemini", {}).get("api_key"))
    if backend == "ollama":
        return bool(llm_config.get("ollama", {}).get("base_url"))
    return False


def _extract_gemini(prompt: str, config: dict) -> dict | None:
    url = GEMINI_URL_TEMPLATE.format(model=config["model"])
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": GEMINI_RESPONSE_SCHEMA,
        },
    }

    resp = requests.post(url, params={"key": config["api_key"]}, json=payload, timeout=60)
    resp.raise_for_status()
    return json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])


def _extract_ollama(prompt: str, config: dict) -> dict | None:
    payload = {
        "model": config["model"],
        "prompt": prompt,
        "stream": False,
        # Reasoning models (like qwen3.6) would otherwise spend most of their time
        # and tokens on a thinking pass this straightforward extraction task
        # doesn't need.
        "think": config.get("think", False),
        "format": OLLAMA_RESPONSE_SCHEMA,
    }

    url = f"{config['base_url'].rstrip('/')}/api/generate"
    resp = requests.post(url, json=payload, timeout=config.get("timeout", 300))
    resp.raise_for_status()
    return json.loads(resp.json()["response"])


def _drop_unsupported_funding_rate(result: dict, text: str) -> dict:
    """Discards a funding rate that doesn't literally appear in the source text.

    The prompt says to only report what's stated, but nothing structurally stops a
    model from returning a plausible-looking number anyway, and local models in
    particular aren't deterministic. An invented rate is worse than a blank one — a
    blank is flagged as "not detected" for manual review, whereas a made-up number
    looks like a real answer. So the number has to be findable in the text next to
    a % / "Prozent" for us to keep it.
    """
    pct = result.get("funding_rate_percent")
    if pct is None:
        return result

    # Match the value as both "60" and "60,5"/"60.5", followed by % or Prozent.
    as_int = f"{pct:g}"
    number = re.escape(as_int).replace(r"\.", "[.,]")
    if not re.search(rf"\b{number}\s*(%|Prozent)", text, re.IGNORECASE):
        print(f"  (discarding funding rate '{as_int} %' — not found in the source text)")
        result["funding_rate_percent"] = None

    return result


def extract(text: str, title: str, llm_config: dict) -> dict | None:
    """Returns the extracted fields, or None if the LLM couldn't be reached/parsed.

    Never raises: any backend failure degrades to the regex-based extraction
    rather than taking the whole run down.
    """
    backend = llm_config.get("backend")
    prompt = PROMPT_TEMPLATE.format(title=title, text=text[:MAX_TEXT_CHARS])

    try:
        if backend == "gemini":
            result = _extract_gemini(prompt, llm_config["gemini"])
        elif backend == "ollama":
            result = _extract_ollama(prompt, llm_config["ollama"])
        else:
            print(f"  (unknown llm backend '{backend}', falling back to regex)")
            return None
    except (requests.exceptions.RequestException, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        print(f"  ({backend} extraction failed, falling back to regex: {exc})")
        return None

    return _drop_unsupported_funding_rate(result, text) if result else result


def _parse_llm_date(raw: str) -> date | None:
    """Parses the date string the model returned.

    ISO is tried strictly first, because that's the format the prompt asks for and
    because dateutil's dayfirst=True — which we need for the German DD.MM.YYYY the
    model sometimes returns instead — silently misreads an ISO date whenever both
    components are <= 12: "2026-09-10" (10 September) comes back as 9 October.
    """
    try:
        return date.fromisoformat(raw.strip()[:10])
    except (ValueError, AttributeError):
        pass

    try:
        return dateparser.parse(raw, dayfirst=True).date()
    except (ValueError, OverflowError, TypeError):
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
        parsed = _parse_llm_date(raw_deadline)
        if parsed:
            deadline = parsed

    return title, funding_rate, funding_rate_percent, deadline
