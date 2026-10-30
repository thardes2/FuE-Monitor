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

import logging
import json
import re
import time
from datetime import date

import requests
from dateutil import parser as dateparser

log = logging.getLogger(__name__)

GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# BMFTR "Bekanntmachungen" often only state the funding rate/deadline in later
# sections (Art/Höhe der Zuwendung, Verfahren), well past the ~15k chars a tighter
# cutoff used to allow. Both backends handle far more than a ~50k-char government
# notice (Gemini flash: 1M tokens, qwen3.6 on Ollama: 262k), so cut generously.
MAX_TEXT_CHARS = 200000

# Gemini uses OpenAPI-flavoured schemas ("nullable"), Ollama takes plain JSON
# Schema (union with "null"), so the same shape has to be spelled twice.
# Bumped whenever PROMPT_TEMPLATE or the schema changes in a way that makes an
# older extraction wrong rather than merely different. Recorded alongside the model
# in the page cache, so existing entries are re-derived from their stored text
# instead of being trusted or re-crawled.
EXTRACTION_PROMPT_VERSION = "3"

# Why a document can state no deadline at all, which the open_deadlines_only filter
# otherwise cannot tell apart: 35 % of scraped pages have no date, and treating them
# all as "open" fills every report with documents nobody can apply to.
#   dated      — it names submission dates (whether or not they have passed)
#   continuous — applications are accepted at any time, no date given
#   framework  — this document takes no applications itself; they go through separate
#                Förderaufrufe/Bekanntmachungen published under it
SUBMISSION_MODES = ("dated", "continuous", "framework")
FRAMEWORK = "framework"

# All submission dates are collected rather than one, because which of them matters
# depends on when you ask — see pick_deadline().
GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "funding_rate_percent": {"type": "number", "nullable": True},
        "deadlines": {"type": "array", "items": {"type": "string"}},
        "submission_mode": {"type": "string", "enum": list(SUBMISSION_MODES)},
        "title": {"type": "string", "nullable": True},
    },
    "required": ["funding_rate_percent", "deadlines", "submission_mode", "title"],
}

OLLAMA_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "funding_rate_percent": {"type": ["number", "null"]},
        "deadlines": {"type": "array", "items": {"type": "string"}},
        "submission_mode": {"type": "string", "enum": list(SUBMISSION_MODES)},
        "title": {"type": ["string", "null"]},
    },
    "required": ["funding_rate_percent", "deadlines", "submission_mode", "title"],
}

PROMPT_TEMPLATE = """You are extracting structured facts from a German government funding-call announcement.
Only use information literally stated in the text below. If a value isn't stated, return null — never guess or infer.

- funding_rate_percent: the funding rate/quota in percent (Förderquote/Fördersatz), as a number, or null.
- deadlines: EVERY date by which applications or project outlines must be submitted, each as an ISO date YYYY-MM-DD, in an array. Return an empty array if the text states no submission date at all.
  - Include a date even if the text says it's non-exclusionary/soft (e.g. "gilt nicht als Ausschlussfrist") — a soft deadline is still a deadline.
  - If submission dates recur (e.g. "jeweils der 15. März und der 15. September", with a first and a last Stichtag), list each one that the text actually names.
  - Include past dates too. Do not leave a date out because it has already passed — today is {today}, and whether a date is still useful is decided elsewhere.
  - DO NOT include dates that are not submission deadlines. In particular, exclude:
    - how long the Richtlinie/Bekanntmachung itself is valid ("Geltungsdauer", "ist bis zum Ablauf des ... gültig", "tritt am ... in Kraft", "tritt ... außer Kraft"),
    - the date the document was issued or published in the Bundesanzeiger ("Vom ...", "BAnz vom ..."),
    - the period a programme or project may run ("Laufzeit", "Vorhabenbeginn", "Projektende"),
    - deadlines for later steps that only selected applicants reach (formal Förderantrag after a positive Skizzen-Bewertung, reporting duties).
- submission_mode: exactly one of "dated", "continuous", "framework".
  - "dated" if the text names any submission date (also when every one of them has passed).
  - "continuous" if applications or outlines may be submitted at any time with no date given ("Projektskizzen können jederzeit eingereicht werden", "laufende Einreichung").
  - "framework" if no application can be made on the basis of THIS document — it sets the frame and announces that separate Förderaufrufe, Bekanntmachungen or Calls will be published under it, which carry the actual deadlines. A "Rahmenbekanntmachung" that only refers to future Förderaufrufe is this case. Do not use "framework" merely because the document is long or general.
- title: a short, clean title for this call if the given title below is messy or unclear, otherwise null.

Given title: {title}

Text:
{text}
"""


# Transient statuses worth another attempt rather than a fallback to regex.
# 503 UNAVAILABLE is the documented "temporarily overloaded" answer and is routine
# on shared/free capacity — measured here at roughly one request in seven, evenly
# spread and with no 429s alongside, i.e. not caused by our own request rate.
RETRYABLE_STATUS = {429, 503}
RETRY_BACKOFF_SECONDS = (2, 6, 18)


def post_with_retry(url: str, **kwargs):
    """POSTs, retrying the transient statuses with exponential backoff.

    Without this a single overload reply costs the whole extraction: the caller
    falls back to regex, which can't read written-out dates, and the degraded
    result then has to be kept out of the cache.
    """
    last = None
    for attempt, delay in enumerate((0,) + RETRY_BACKOFF_SECONDS):
        if delay:
            time.sleep(delay)
        last = requests.post(url, **kwargs)
        if last.status_code not in RETRYABLE_STATUS:
            return last
        if attempt < len(RETRY_BACKOFF_SECONDS):
            log.warning(f"  (backend returned {last.status_code}, retrying in {RETRY_BACKOFF_SECONDS[attempt]}s)")
    return last


def model_id(llm_config: dict | None) -> str:
    """Identifies the model behind a result, as "backend/model".

    Used in cache keys and cache entries so results aren't silently reused across
    a backend or model change — different models answer the same prompt
    differently.
    """
    if not llm_config:
        return "none"
    backend = llm_config.get("backend", "none")
    return f"{backend}/{llm_config.get(backend, {}).get('model', '?')}"


def extraction_id(llm_config: dict | None) -> str:
    """Identifies model *and* extraction prompt, as "backend/model#pN".

    The page cache records this rather than the bare model: a corrected prompt makes
    the stored fields wrong, not merely different, so those entries have to be
    re-derived. The match cache keeps using model_id() — the matching prompt has its
    own version, and an extraction fix shouldn't throw away paid-for evaluations.
    """
    return f"{model_id(llm_config)}#p{EXTRACTION_PROMPT_VERSION}"


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


def _extract_gemini(prompt: str, config: dict, schema: dict = GEMINI_RESPONSE_SCHEMA) -> dict | None:
    url = GEMINI_URL_TEMPLATE.format(model=config["model"])
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }

    # The key goes in a header, not in params: requests puts query strings into the
    # exception message raise_for_status() builds, so a single 503 would otherwise
    # print the API key into the run log (13 times in one measured run).
    resp = post_with_retry(url, headers={"x-goog-api-key": config["api_key"]}, json=payload, timeout=60)
    resp.raise_for_status()
    return json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])


def _extract_ollama(prompt: str, config: dict, schema: dict = OLLAMA_RESPONSE_SCHEMA) -> dict | None:
    payload = {
        "model": config["model"],
        "prompt": prompt,
        "stream": False,
        # Reasoning models (like qwen3.8) would otherwise spend most of their time
        # and tokens on a thinking pass this straightforward extraction task
        # doesn't need.
        "think": config.get("think", False),
        "format": schema,
    }

    url = f"{config['base_url'].rstrip('/')}/api/generate"
    resp = post_with_retry(url, json=payload, timeout=config.get("timeout", 300))
    resp.raise_for_status()
    return json.loads(resp.json()["response"])


def _to_gemini_schema(schema: dict) -> dict:
    """Rewrites a plain JSON Schema into the OpenAPI-ish dialect Gemini expects.

    The two differ on nullability: JSON Schema says type: ["string", "null"],
    Gemini wants type: "string" plus nullable: true.
    """
    converted = {k: v for k, v in schema.items() if k != "properties"}
    properties = {}
    for name, spec in schema.get("properties", {}).items():
        spec = dict(spec)
        types = spec.get("type")
        if isinstance(types, list):
            non_null = [t for t in types if t != "null"]
            spec["type"] = non_null[0] if non_null else "string"
            if "null" in types:
                spec["nullable"] = True
        properties[name] = spec
    converted["properties"] = properties
    return converted


def run_json(prompt: str, llm_config: dict, schema: dict) -> dict | None:
    """Runs an arbitrary prompt against the configured backend, parsed as JSON.

    Same failure contract as extract(): never raises, returns None so the caller
    can degrade. `schema` is plain JSON Schema and gets translated for Gemini.
    """
    backend = llm_config.get("backend")
    try:
        if backend == "gemini":
            return _extract_gemini(prompt, llm_config["gemini"], _to_gemini_schema(schema))
        if backend == "ollama":
            return _extract_ollama(prompt, llm_config["ollama"], schema)
        log.warning(f"  (unknown llm backend '{backend}')")
        return None
    except (requests.exceptions.RequestException, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        log.warning(f"  ({backend} request failed: {exc})")
        return None


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
        log.warning(f"  (discarding funding rate '{as_int} %' — not found in the source text)")
        result["funding_rate_percent"] = None

    return result


def extract(text: str, title: str, llm_config: dict) -> dict | None:
    """Returns the extracted fields, or None if the LLM couldn't be reached/parsed.

    Never raises: any backend failure degrades to the regex-based extraction
    rather than taking the whole run down.
    """
    backend = llm_config.get("backend")
    prompt = PROMPT_TEMPLATE.format(
        title=title, text=text[:MAX_TEXT_CHARS], today=date.today().isoformat()
    )

    try:
        if backend == "gemini":
            result = _extract_gemini(prompt, llm_config["gemini"])
        elif backend == "ollama":
            result = _extract_ollama(prompt, llm_config["ollama"])
        else:
            log.warning(f"  (unknown llm backend '{backend}', falling back to regex)")
            return None
    except (requests.exceptions.RequestException, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        log.warning(f"  ({backend} extraction failed, falling back to regex: {exc})")
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


def parse_deadlines(raw: object) -> list[date]:
    """Parses the model's date strings, dropping anything unusable, sorted."""
    if not isinstance(raw, list):
        raw = [raw] if raw else []
    parsed = {d for d in (_parse_llm_date(item) for item in raw) if d}
    return sorted(parsed)


def pick_deadline(deadlines: list[date], today: date | None = None) -> date | None:
    """Picks the deadline worth showing: the next one still open.

    Choosing the latest date instead — which an earlier prompt asked for — hid the
    deadline that actually mattered: one call listed "bis 30. November 2026
    beziehungsweise 31. Mai 2028" and was reported as 2028, four weeks before the
    real one. When every date has passed, the last one is returned rather than None,
    so open_deadlines_only recognises the call as expired; None would make it pass
    as "deadline unknown" and keep an expired call in the report.

    Deliberately evaluated on read, not at extraction time, so a cached entry still
    advances to the next Stichtag as dates go by.
    """
    if not deadlines:
        return None
    today = today or date.today()
    return next((d for d in deadlines if d >= today), deadlines[-1])


def merge(result: dict | None, title: str, funding_rate: str, funding_rate_percent: float | None, deadline: date | None):
    """Overrides the regex-based fields with the LLM's, field by field, wherever it found something.

    Returns the full list of submission dates alongside the single effective one, so
    callers can cache the list and re-pick from it later, plus the submission mode.
    An unrecognised mode comes back as "", which every caller treats as "unknown"
    rather than guessing.
    """
    if not result:
        return title, funding_rate, funding_rate_percent, deadline, [], ""

    if result.get("title"):
        title = result["title"]

    pct = result.get("funding_rate_percent")
    if pct is not None:
        funding_rate_percent = float(pct)
        funding_rate = f"{pct:g} %"

    deadlines = parse_deadlines(result.get("deadlines"))
    if deadlines:
        deadline = pick_deadline(deadlines)

    mode = result.get("submission_mode") or ""
    if mode not in SUBMISSION_MODES:
        mode = ""

    return title, funding_rate, funding_rate_percent, deadline, deadlines, mode
