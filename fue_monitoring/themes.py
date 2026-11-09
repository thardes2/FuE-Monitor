# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Suggesting a profile's themes from what is written about the organisation.

Themes are search terms: the prefilter looks for each one, on word boundaries,
in a call's text, and only calls with a hit are ever evaluated. A theme that
reads well but never appears verbatim in an announcement ("Security- und
Privacy-Ratschläge für Endnutzer") silently matches nothing. So the model is
asked for the short terms a call would actually use, and whatever comes back is
checked against the same rules the prefilter applies.

The result is a suggestion for a person to review, never written into a
profile by this module.
"""

from __future__ import annotations

import logging
import re

from . import llm_extract
from .matching import ensure_local_backend, theme_pattern
from .profiles import Theme

log = logging.getLogger(__name__)

# The material can be long (descriptions, project lists, publication clusters);
# what characterises a group sits in the first part, and a local model gets
# slower and vaguer with every extra page.
DEFAULT_MAX_TEXT_CHARS = 20000
DEFAULT_COUNT = 15
MAX_WORDS = 3
MAX_CHARS = 40
# A suggestion whose last word is at least this long gets a trailing "*" if the
# model left it off: "Software-Lieferkette" must also find "Software-Lieferketten"
# and "Lieferkettengesetz"-style compounds. Shorter words — acronyms above all —
# stay strict, so "KI" never matches "Kinder".
STAR_MIN_LETTERS = 6

# Terms that appear in nearly every announcement and would let everything
# through the prefilter. Compared case-insensitively, without "*".
TOO_GENERIC = frozenset({
    "forschung", "entwicklung", "innovation", "innovationen", "digitalisierung", "technologie",
    "technologien", "projekt", "projekte", "förderung", "wissenschaft", "transfer", "kooperation",
    "nachhaltigkeit", "research", "development", "technology", "digital", "daten", "data",
    "software", "industrie", "anwendung", "anwendungen",
    # Seen in suggestions for real chairs: plausible words, but on their own they
    # occur in announcements of every field.
    "sicherheit", "vertrauen", "anwender", "nutzer", "endnutzer", "gesellschaft", "mensch",
    "menschen", "qualität", "effizienz", "prozess", "prozesse", "plattform", "plattformen",
})

# Letters (any script), digits, space, hyphen, slash, dot, ampersand and "+";
# "*" only at either end, which is all theme_pattern understands.
_ALLOWED = re.compile(r"^\*?[\w][\w \-/.&+]*\*?$")

SCHEMA = {
    "type": "object",
    "properties": {
        "themen": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "begriff": {"type": "string"},
                    "gewicht": {"type": "integer"},
                },
                "required": ["begriff", "gewicht"],
            },
        },
    },
    "required": ["themen"],
}

PROMPT_TEMPLATE = """You suggest search terms for screening research funding calls.
A prefilter looks for each term, as a whole word, in the full text of German and EU funding
announcements; only calls with a hit get a closer look. Terms that never appear verbatim in such
announcements are useless, however well they describe the organisation.

ORGANISATION
Name: {name}
Description: {description}

MATERIAL (research focus, projects, publications — use it, but do not copy its long phrases)
{material}

TASK
Return up to {count} terms in "themen", each with a weight:
- 3 = core of what the organisation does, 2 = clearly relevant, 1 = adjacent but still of interest.
- At most {max_words} words per term. Prefer the noun or established technical term a call would use
  ("Software-Lieferkette", "SBOM", "Quantencomputing", "Lernanalytik"), not a description.
- German announcements use German terms, EU calls English ones: give an important concept in both
  languages when the two differ ("Cybersicherheit" and "Cybersecurity").
- Append "*" to a German noun that gets compounded ("Mobilität*" also finds "Mobilitätskonzept").
  Leave short acronyms without "*" ("KI", "SBOM"), so "KI" does not match "Kinder".
- Avoid terms that occur in nearly every call (Forschung, Innovation, Digitalisierung, Technologie),
  and lone everyday words (Sicherheit, Vertrauen, Nutzer, Qualität): qualify them instead
  ("IT-Sicherheit", "Usable Security").
- Do not repeat a term in a different spelling.
"""


def _clean(raw: dict | None, count: int) -> list[Theme]:
    themes: list[Theme] = []
    seen: set[str] = set()
    for item in (raw or {}).get("themen") or []:
        if not isinstance(item, dict):
            continue
        begriff = " ".join(str(item.get("begriff") or "").split())
        core = begriff.strip("*").strip()
        key = core.casefold()
        if not core or key in seen:
            continue
        if len(begriff) > MAX_CHARS or len(core.split()) > MAX_WORDS:
            log.debug(f"  (theme suggestion dropped, too long: {begriff!r})")
            continue
        if key in TOO_GENERIC or not _ALLOWED.match(begriff):
            log.debug(f"  (theme suggestion dropped: {begriff!r})")
            continue
        last = core.split()[-1]
        if not begriff.endswith("*") and len(last) >= STAR_MIN_LETTERS and not last.isupper():
            begriff += "*"
        try:
            theme_pattern(begriff)
            gewicht = int(item.get("gewicht", 1))
        except (re.error, TypeError, ValueError):
            continue
        seen.add(key)
        themes.append(Theme(begriff=begriff, gewicht=min(3, max(1, gewicht))))
    themes.sort(key=lambda t: -t.gewicht)  # stable: the model's order within a weight stays
    return themes[:count]


def suggest_themes(
    config: dict,
    name: str,
    description: str,
    material: str,
    *,
    count: int = DEFAULT_COUNT,
    allow_cloud: bool = False,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> list[Theme] | None:
    """Suggests themes for a profile from what is known about the organisation.

    `material` is free text: research focus, projects, publication topics, product
    descriptions — whatever there is. Returns the cleaned suggestions, best weight
    first, or None if the model gave no usable answer (an empty list means it
    answered, but nothing survived the checks).

    Like matching, this sends organisation data to the model, so only the local
    backend is used unless `allow_cloud` is set (the profile's `cloud_erlaubt`).
    """
    llm_config = config.get("llm") if llm_extract.is_configured(config.get("llm") or {}) else None
    ensure_local_backend(llm_config, allow_cloud=allow_cloud)

    prompt = PROMPT_TEMPLATE.format(
        name=name.strip() or "—",
        description=description.strip() or "—",
        material=material.strip()[:max_text_chars] or "—",
        count=count,
        max_words=MAX_WORDS,
    )
    raw = llm_extract.run_json(prompt, llm_config, SCHEMA)
    if raw is None:
        return None
    return _clean(raw, count)
