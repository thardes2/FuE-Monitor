# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Matches collected calls against company profiles, in two stages.

Stage 1 is a cheap word-boundary prefilter. It exists because stage 2 costs a
local LLM call per (call, profile) pair: at ~110 calls and a handful of profiles
an unfiltered run would be hundreds of inferences. The prefilter also fixes the
precision problem the global keyword list has — substring matching makes "KI"
hit "Kinder" and "Kilometer", which is tolerable for a human-scanned table but
not for a report sent to a company.

Stage 2 asks the model to score the fit and judge eligibility, and to back it
with a verbatim quote from the call. That quote is checked against the source
text; one that isn't found is reported as unverified rather than trusted, the
same stance the funding-rate guard in llm_extract takes.

Profile data is confidential, so this refuses to run on a hosted backend.
"""

from __future__ import annotations

import logging
import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import llm_extract
from .models import FundingCall
from .profiles import Profile

log = logging.getLogger(__name__)

# Bump when the prompt or schema changes so cached evaluations from the old
# wording don't get reused.
PROMPT_VERSION = "2"

# Far below llm_extract's 200k. Measured against qwen3.8 on the configured host:
# the same announcement costs ~24 s at 20k characters and ~85 s at 98k, and one
# evaluation runs per (call, profile) pair — so the full text would turn a wide
# profile into hours. Fit scoring and eligibility rest on the opening sections
# (Förderziel, Zuwendungsempfänger), which sit well inside this budget; the
# deadline and funding rate come from llm_extract, which does read the whole
# document. Override with profiles.max_text_chars.
DEFAULT_MAX_TEXT_CHARS = 20000

EVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "begruendung": {"type": "string"},
        "foerderfaehig": {"type": "string", "enum": ["ja", "nein", "unklar"]},
        "foerderfaehig_grund": {"type": ["string", "null"]},
        "fundstelle": {"type": ["string", "null"]},
    },
    "required": ["score", "begruendung", "foerderfaehig", "foerderfaehig_grund", "fundstelle"],
}

PROMPT_TEMPLATE = """You assess whether a German/EU research funding call fits a specific company.
Judge only on what the call text below actually says — never invent conditions it doesn't state.
Answer in German (the report goes to German-speaking staff).

COMPANY
Name: {name}
Size: {groesse}   (kmu = SME under the EU definition, grossunternehmen = large enterprise)
Role: {rolle}
Topics (weight 3 = core business, 1 = adjacent): {themen}
Will not work on: {ausschluss}
Preferred programmes: {programme}
Will shoulder at most this own contribution: {eigenanteil}
Prefiltered topic hits in this call: {treffer}

FIELDS
- score: 0-100, how well this call fits THIS company. Weigh the topic weights: a hit on a
  weight-3 topic counts far more than one on a weight-1 topic. A call that merely mentions a
  topic in passing scores low; one whose actual subject is the company's core business scores high.
  Score 0 if the call's actual subject is something under "Will not work on" above. Note the
  difference: the call *funding* such work rules it out, whereas the call merely saying that such
  work is excluded from funding does not — that is the call agreeing with the company, not a
  conflict. Say which of the two it is in begruendung whenever one of those words appears.
- begruendung: 2-3 sentences, German, why that score — name the concrete overlap or mismatch.
- foerderfaehig: "ja", "nein" or "unklar" — whether THIS company may apply at all.
  Check the call for: restriction to SMEs or to large enterprises, a mandatory consortium
  or partner from another country, required own contribution above what the company accepts,
  and restriction to a region or sector the company isn't in.
- foerderfaehig_grund: German, the concrete reason when it's "nein" or "unklar"
  (e.g. "nur für KMU", "Verbund mit mind. 3 Partnern gefordert", "Eigenanteil 50 % übersteigt Maximum").
  null when "ja".
- fundstelle: a VERBATIM quote from the call text below that supports your eligibility judgement,
  copied exactly, no paraphrase, at most ~200 characters. null if the text says nothing about it.

CALL
Title: {title}
Programme: {programme_call}
Deadline: {deadline}
Funding rate: {funding_rate}

TEXT:
{text}
"""


class BackendNotLocalError(RuntimeError):
    """Raised when profile matching would send confidential data off the machine."""


def ensure_local_backend(llm_config: dict | None, allow_cloud: bool = False) -> None:
    """Profile data must not leave the machine, so only Ollama is acceptable here —
    unless the profile opts in with `cloud_erlaubt: true`.

    Checked once up front instead of per call, so a misconfigured backend fails
    before any company data has been assembled into a prompt.
    """
    if not llm_config:
        raise BackendNotLocalError(
            "Profile matching needs an LLM, but none is configured (llm.enabled / llm.backend)."
        )
    backend = llm_config.get("backend")
    if backend != "ollama" and not allow_cloud:
        raise BackendNotLocalError(
            f"Profile matching is restricted to the local 'ollama' backend, but llm.backend is "
            f"'{backend}'. Company profiles are confidential and must not be sent to a hosted API "
            f"unless the profile sets 'cloud_erlaubt: true'."
        )


def permitted_profiles(profiles, llm_config: dict | None):
    """Splits profiles into those the configured backend may process and those it may not.

    Used so one profile withholding consent doesn't block the others: a run with a
    hosted backend processes everything that set `cloud_erlaubt: true` and skips the
    rest, rather than aborting wholesale. Callers are expected to report the skipped
    ones — silently dropping a company would look exactly like "no matches found".
    """
    runnable, skipped = [], []
    for profile in profiles:
        try:
            ensure_local_backend(llm_config, allow_cloud=profile.cloud_erlaubt)
            runnable.append(profile)
        except BackendNotLocalError:
            skipped.append(profile)
    return runnable, skipped


def theme_pattern(begriff: str) -> re.Pattern:
    """Compiles a profile term into a word-boundary regex.

    A trailing "*" opts into German compounds: "Mobilität*" becomes
    \\bMobilität\\w* and so matches "Mobilitätskonzept", while plain "Mobilität"
    only matches the standalone word. Short acronyms are left strict on purpose —
    a prefix match on "KI" would bring back exactly the "Kinder"/"Kilometer"
    noise this prefilter exists to remove.
    """
    term = begriff.strip()
    # A leading "*" matches the tail of a compound: "*absicherung*" finds
    # "Fahrzeugabsicherung". Previously the "*" was escaped into a literal asterisk,
    # so such a term matched nothing at all and said nothing about it.
    leading = term.startswith("*")
    trailing = term.endswith("*")
    core = re.escape(term.strip("*"))

    prefix = r"\w*" if leading else r"\b"
    suffix = r"\w*" if trailing else r"\b"
    return re.compile(rf"{prefix}{core}{suffix}", re.IGNORECASE)


def find_terms(text: str, terms: list[str]) -> list[str]:
    """Which of `terms` occur in `text`, word-boundary matched."""
    return [t for t in terms if theme_pattern(t).search(text)]


@dataclass
class PrefilterResult:
    passed: bool
    hits: list[tuple[str, int]] = field(default_factory=list)  # (begriff, gewicht)
    excluded_by: str = ""

    @property
    def weight(self) -> int:
        return sum(gewicht for _, gewicht in self.hits)


def prefilter(call: FundingCall, profile: Profile) -> PrefilterResult:
    """Decides cheaply whether this call is worth an LLM evaluation for this profile.

    Themes are matched against the whole text, because that is where a call's subject
    actually shows — a Richtlinie's title rarely names everything it funds.

    Hard exclusions are matched only against the title and programme name, which is
    narrower on purpose. An announcement's boilerplate routinely mentions what is
    *not* funded ("Vorhaben der Wehrtechnik sind ausgeschlossen"), and matching
    exclusions against the full text turned such a sentence into a reason to drop the
    call — the profile's exclusion term and the call's own disclaimer are
    indistinguishable at this level. Anything subtler than the title is left to the
    evaluation, which gets the exclusions as a criterion and can read the sentence
    around them.
    """
    excluded = find_terms(f"{call.title} {call.programme}", profile.ausschluss)
    if excluded:
        return PrefilterResult(passed=False, excluded_by=excluded[0])

    haystack = f"{call.title} {call.text}"
    hits = [(t.begriff, t.gewicht) for t in profile.themen if theme_pattern(t.begriff).search(haystack)]
    return PrefilterResult(passed=bool(hits), hits=hits)


def _normalise(text: str) -> str:
    """Collapses whitespace and case so a quote can be compared against the source.

    Models reproduce quotes with different line breaks and spacing than the
    scraped text has, which would make an otherwise-correct quote look invented.
    """
    return re.sub(r"\s+", " ", text).casefold().strip()


# How close a quote has to be to the source to count as supported. A quote that is
# off by a character or two is still a quote; one the model made up scores far lower,
# so the exact threshold matters much less than having one at all. Measured on the
# case that prompted this: "nichtstatliche" for "nichtstaatliche", one missing
# letter in ~210 characters, which exact matching rejected outright.
QUOTE_SIMILARITY_THRESHOLD = 0.95

# Below this a quote has too little substance for similarity to mean anything — a
# short phrase can reach a high ratio against unrelated text by accident, so those
# still have to match exactly.
QUOTE_MIN_WORDS_FOR_FUZZY = 6

# Bounds the work on a long document: only the most distinctive words are used as
# anchors, and only so many candidate positions are compared.
QUOTE_ANCHOR_COUNT = 3
QUOTE_MAX_CANDIDATES = 40


def _close_enough(quote_words: list[str], text_words: list[str]) -> bool:
    """Whether some window of the text is within the threshold of the quote.

    Anchored on the quote's longest words rather than scanning every position: a
    scraped announcement runs to tens of thousands of words, and comparing a window
    at each one would cost more than the LLM call that produced the quote.
    """
    quote = " ".join(quote_words)
    anchors = sorted(set(quote_words), key=len, reverse=True)[:QUOTE_ANCHOR_COUNT]

    candidates: list[int] = []
    for anchor in anchors:
        offset = quote_words.index(anchor)
        candidates += [
            i - offset for i, word in enumerate(text_words)
            if word == anchor and i >= offset
        ]
        if len(candidates) >= QUOTE_MAX_CANDIDATES:
            break

    for start in sorted(set(candidates))[:QUOTE_MAX_CANDIDATES]:
        window = " ".join(text_words[start:start + len(quote_words)])
        if difflib.SequenceMatcher(None, quote, window).ratio() >= QUOTE_SIMILARITY_THRESHOLD:
            return True
    return False


def verify_quote(quote: str | None, text: str) -> bool:
    """Whether the quote is actually in the source text.

    Exact containment first, then a near match. The point of the check is to catch a
    quote the model invented, and a single mistyped character doesn't make a quote
    invented — flagging one as unsupported sends the reader hunting through the
    source for a passage that is really there, which costs more trust than it saves.
    """
    if not quote or not quote.strip():
        return False

    normalised_quote, normalised_text = _normalise(quote), _normalise(text)
    if normalised_quote in normalised_text:
        return True

    quote_words = normalised_quote.split()
    if len(quote_words) < QUOTE_MIN_WORDS_FOR_FUZZY:
        return False

    return _close_enough(quote_words, normalised_text.split())


def cache_key(call_link: str, profile_hash: str, model: str = "") -> str:
    """Identifies one evaluation: this call, for this profile, by this model and prompt.

    The model belongs in the key because different models score the same call
    differently — measured on one document: qwen3.8 gave 10 and 15 on repeat runs
    where gemini gave 15. Without it, switching backends would silently mix scores
    from two models into one report, and switching back would resurrect the older
    model's verdicts.
    """
    raw = f"{call_link}|{profile_hash}|{model}|v{PROMPT_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class MatchCache:
    """Stores LLM evaluations so only new (call, profile) pairs cost inference time."""

    def __init__(self, path: str | Path, max_age_days: int = 30, enabled: bool = True):
        self.path = Path(path)
        self.max_age = timedelta(days=max_age_days)
        self.enabled = enabled
        self._entries: dict[str, dict] = {}
        self._dirty = False
        # Only the keys this run evaluated are written back — see save().
        self._touched: set[str] = set()

        if self.enabled:
            self._entries = self._read()

    def _read(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(f"  (match cache at {self.path} unreadable, starting empty: {exc})")
            return {}

    def get(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        entry = self._entries.get(key)
        if not entry:
            return None
        try:
            if datetime.now() - datetime.fromisoformat(entry["evaluated_at"]) > self.max_age:
                return None
        except (KeyError, ValueError):
            return None
        return entry["evaluation"]

    def put(self, key: str, evaluation: dict) -> None:
        if not self.enabled:
            return
        self._entries[key] = {
            "evaluated_at": datetime.now().isoformat(timespec="seconds"),
            "evaluation": evaluation,
        }
        self._touched.add(key)
        self._dirty = True

    def save(self) -> None:
        """Merges this run's evaluations into whatever is on disk now.

        Two profiles run concurrently write disjoint keys, so writing the whole
        snapshot would make each one erase the other's evaluations outright — and
        an evaluation costs an LLM call, unlike a page that could be re-fetched.
        """
        if not self.enabled or not self._dirty:
            return

        merged = self._read()
        merged.update({key: self._entries[key] for key in self._touched if key in self._entries})

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

        self._entries = merged
        self._dirty = False


@dataclass
class Match:
    call: FundingCall
    score: int = 0
    begruendung: str = ""
    foerderfaehig: str = "unklar"
    foerderfaehig_grund: str = ""
    fundstelle: str = ""
    fundstelle_belegt: bool = False
    hits: list[tuple[str, int]] = field(default_factory=list)
    review: str = ""

    @property
    def quote_note(self) -> str:
        if not self.fundstelle:
            return ""
        return "" if self.fundstelle_belegt else "unbelegt — Zitat nicht im Ausschreibungstext gefunden"


def _normalise_evaluation(raw: dict, text: str) -> dict:
    """Clamps and sanity-checks what the model returned before it's used."""
    try:
        score = int(raw.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    foerderfaehig = str(raw.get("foerderfaehig") or "unklar").strip().lower()
    if foerderfaehig not in {"ja", "nein", "unklar"}:
        foerderfaehig = "unklar"

    quote = (raw.get("fundstelle") or "").strip()
    return {
        "score": score,
        "begruendung": str(raw.get("begruendung") or "").strip(),
        "foerderfaehig": foerderfaehig,
        "foerderfaehig_grund": str(raw.get("foerderfaehig_grund") or "").strip(),
        "fundstelle": quote,
        "fundstelle_belegt": verify_quote(quote, text),
    }


def evaluate(
    call: FundingCall,
    profile: Profile,
    hits: list[tuple[str, int]],
    llm_config: dict,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> dict | None:
    text = f"{call.title} {call.text}".strip()
    themen = ", ".join(f"{t.begriff} ({t.gewicht})" for t in profile.themen)
    prompt = PROMPT_TEMPLATE.format(
        name=profile.name,
        groesse=profile.groesse,
        rolle=profile.rolle or "—",
        themen=themen,
        # The prefilter only screens title and programme for these, so the
        # evaluation is where an exclusion buried in the text has to be caught.
        ausschluss=", ".join(profile.ausschluss) or "—",
        programme=", ".join(profile.programme_bevorzugt) or "—",
        eigenanteil=f"{profile.eigenanteil_max_prozent:g} %" if profile.eigenanteil_max_prozent is not None else "—",
        treffer=", ".join(f"{b} ({g})" for b, g in hits) or "—",
        title=call.title,
        programme_call=call.programme or "—",
        deadline=call.deadline.strftime("%d.%m.%Y") if call.deadline else "unbekannt",
        funding_rate=call.funding_rate or "unbekannt",
        text=text[:max_text_chars],
    )

    raw = llm_extract.run_json(prompt, llm_config, EVALUATION_SCHEMA)
    if not raw:
        return None
    # Verified against the excerpt the model was given, not the full document:
    # a quote from text it never saw would be a fabrication either way.
    return _normalise_evaluation(raw, text[:max_text_chars])


def match_profile(
    calls: list[FundingCall],
    profile: Profile,
    llm_config: dict,
    cache: MatchCache | None = None,
    min_score: int = 0,
    max_evaluations: int | None = None,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> list[Match]:
    """Returns this profile's matches, best score first."""
    ensure_local_backend(llm_config, allow_cloud=profile.cloud_erlaubt)

    candidates = []
    for call in calls:
        result = prefilter(call, profile)
        if result.passed:
            candidates.append((call, result))

    # Highest prefilter weight first, so a capped run spends its inferences on the
    # most promising calls rather than whatever happened to be collected first.
    candidates.sort(key=lambda c: c[1].weight, reverse=True)
    passed_prefilter = len(candidates)
    if max_evaluations is not None:
        candidates = candidates[:max_evaluations]

    matches: list[Match] = []
    evaluated = 0
    for call, result in candidates:
        key = cache_key(call.link, profile.hash, llm_extract.model_id(llm_config))
        evaluation = cache.get(key) if cache else None

        if evaluation is None:
            evaluation = evaluate(call, profile, result.hits, llm_config, max_text_chars)
            evaluated += 1
            if evaluation is None:
                continue
            if cache:
                cache.put(key, evaluation)

        if evaluation["score"] < min_score:
            continue
        matches.append(Match(call=call, hits=result.hits, **evaluation))

    # The prefilter count is reported before the cap is applied: showing the capped
    # number would read as "this profile barely matches anything" when in truth
    # most candidates simply weren't looked at.
    message = (
        f"  ({profile.id}: {passed_prefilter} of {len(calls)} calls passed the prefilter, "
        f"{evaluated} newly evaluated, {len(candidates) - evaluated} from cache"
    )
    if max_evaluations is not None and passed_prefilter > max_evaluations:
        message += f"; {passed_prefilter - max_evaluations} not evaluated, capped by max_evaluations_per_profile"
    log.info(message + ")")
    matches.sort(key=lambda m: (-m.score, m.call.deadline is None, m.call.deadline))
    return matches
