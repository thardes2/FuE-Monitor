# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Company profiles that calls get matched against.

One YAML file per company in profiles/. Those files hold confidential SICP member
data and are gitignored — only profiles/example.yaml (a made-up company) is in the
repository.

The profile hash deliberately covers only the fields that change what a match
*means* (themes, exclusions, size, role, own-contribution limit). Editing a
contact person or the display name therefore doesn't throw away cached LLM
evaluations, while editing a theme does.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_SIZES = {"kmu", "grossunternehmen"}

# Everything a profile file may contain. Anything else is refused, not warned about:
# an unsupported key looks exactly like a supported one in YAML, and a "min_score: 40"
# that nothing read produced a report full of zero-score calls in silence. A warning
# would have been read once and then scrolled past, while the file kept claiming a
# setting that had no effect — false confidence is the thing to prevent here.
# "notizen" is the escape hatch for anything genuinely meant as a note.
KNOWN_KEYS = frozenset({
    "id", "name", "groesse", "rolle", "themen", "ausschluss", "programme_bevorzugt",
    "eigenanteil_max_prozent", "sicp_ansprechpartner", "min_score", "cloud_erlaubt",
    "notizen",
})
KNOWN_THEME_KEYS = frozenset({"begriff", "gewicht"})


def _reject_unknown(raw: dict, source: str) -> None:
    """Refuses keys nothing reads, at both levels a typo can hide in."""
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        raise ProfileError(
            f"{source}: unknown key(s) {', '.join(unknown)}. Supported: "
            f"{', '.join(sorted(KNOWN_KEYS))}. Put anything that is only a note "
            f"under 'notizen:'."
        )

    for item in raw.get("themen") or []:
        if not isinstance(item, dict):
            continue
        # A "gewichtung:" instead of "gewicht:" would fall back to weight 1 and
        # quietly change how the LLM scores everything for this company.
        stray = sorted(set(item) - KNOWN_THEME_KEYS)
        if stray:
            begriff = item.get("begriff", "?")
            raise ProfileError(
                f"{source}: unknown key(s) {', '.join(stray)} under the 'themen' "
                f"entry {begriff!r}. Supported: {', '.join(sorted(KNOWN_THEME_KEYS))}."
            )


class ProfileError(ValueError):
    """A profile file is unusable — raised with the file name and what's wrong."""


@dataclass
class Theme:
    begriff: str
    gewicht: int = 1


@dataclass
class Profile:
    id: str
    name: str
    groesse: str
    rolle: str = ""
    themen: list[Theme] = field(default_factory=list)
    ausschluss: list[str] = field(default_factory=list)
    programme_bevorzugt: list[str] = field(default_factory=list)
    eigenanteil_max_prozent: float | None = None
    sicp_ansprechpartner: str = ""
    # Lowest score this company wants to see, overriding profiles.min_score from
    # config.yaml. None means "use the global setting". Deliberately not part of the
    # hash: it decides what gets reported, not what a match means, so raising it must
    # not throw away evaluations that were already paid for.
    min_score: int | None = None
    # Opt-in: lets this profile's data go to the hosted llm.backend (e.g. Gemini).
    # Deliberately not part of the hash — it changes where inference runs, not what a match means.
    cloud_erlaubt: bool = False

    @property
    def hash(self) -> str:
        """Stable fingerprint of the matching-relevant fields; used as cache key."""
        relevant = {
            "groesse": self.groesse,
            "rolle": self.rolle,
            "themen": sorted((t.begriff, t.gewicht) for t in self.themen),
            "ausschluss": sorted(self.ausschluss),
            "programme_bevorzugt": sorted(self.programme_bevorzugt),
            "eigenanteil_max_prozent": self.eigenanteil_max_prozent,
        }
        payload = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _parse(raw: dict, source: str) -> Profile:
    def require(key: str) -> str:
        value = raw.get(key)
        if not value or not str(value).strip():
            raise ProfileError(f"{source}: '{key}' is required")
        return str(value).strip()

    _reject_unknown(raw, source)

    profile_id = require("id")
    if "/" in profile_id or profile_id != profile_id.strip():
        raise ProfileError(f"{source}: 'id' must not contain slashes or outer spaces")

    groesse = require("groesse").lower()
    if groesse not in VALID_SIZES:
        raise ProfileError(f"{source}: 'groesse' must be one of {sorted(VALID_SIZES)}, got {groesse!r}")

    themen = []
    for item in raw.get("themen") or []:
        if isinstance(item, str):  # shorthand: a bare string means weight 1
            themen.append(Theme(begriff=item.strip(), gewicht=1))
            continue
        begriff = str(item.get("begriff", "")).strip()
        if not begriff:
            raise ProfileError(f"{source}: every entry under 'themen' needs a 'begriff'")
        try:
            gewicht = int(item.get("gewicht", 1))
        except (TypeError, ValueError):
            raise ProfileError(f"{source}: 'gewicht' for {begriff!r} must be a number 1-3")
        if not 1 <= gewicht <= 3:
            raise ProfileError(f"{source}: 'gewicht' for {begriff!r} must be between 1 and 3, got {gewicht}")
        themen.append(Theme(begriff=begriff, gewicht=gewicht))

    if not themen:
        raise ProfileError(f"{source}: at least one entry under 'themen' is required")

    eigenanteil = raw.get("eigenanteil_max_prozent")
    if eigenanteil is not None:
        try:
            eigenanteil = float(eigenanteil)
        except (TypeError, ValueError):
            raise ProfileError(f"{source}: 'eigenanteil_max_prozent' must be a number")

    min_score = raw.get("min_score")
    if min_score is not None:
        try:
            # Via float first, so "40" is accepted while 4.7 is refused rather than
            # silently truncated to 4 — a threshold quietly moved is the defect this
            # whole field exists to fix.
            as_number = float(min_score)
        except (TypeError, ValueError):
            raise ProfileError(f"{source}: 'min_score' must be a whole number")
        if as_number != int(as_number):
            raise ProfileError(f"{source}: 'min_score' must be a whole number, got {min_score!r}")
        min_score = int(as_number)
        if not 0 <= min_score <= 100:
            raise ProfileError(f"{source}: 'min_score' must be between 0 and 100, got {min_score}")

    cloud = raw.get("cloud_erlaubt", False)
    if not isinstance(cloud, bool):
        raise ProfileError(f"{source}: 'cloud_erlaubt' must be true or false")

    return Profile(
        id=profile_id,
        name=require("name"),
        groesse=groesse,
        rolle=str(raw.get("rolle", "")).strip(),
        themen=themen,
        ausschluss=[str(x).strip() for x in (raw.get("ausschluss") or []) if str(x).strip()],
        programme_bevorzugt=[str(x).strip() for x in (raw.get("programme_bevorzugt") or []) if str(x).strip()],
        eigenanteil_max_prozent=eigenanteil,
        sicp_ansprechpartner=str(raw.get("sicp_ansprechpartner", "")).strip(),
        min_score=min_score,
        cloud_erlaubt=cloud,
    )


def search_terms(profiles: list[Profile], min_weight: int = 2) -> list[str]:
    """Plain search terms for source queries, drawn from the profiles' themes.

    Wildcards are stripped ("*absicherung*" -> "absicherung"): the sources do
    substring matching and the EU portal takes no wildcards. Only themes of
    `min_weight` and up drive discovery, so weight-1 terms like "Fahrzeug*"
    don't flood the queries; a profile with nothing that heavy contributes all.
    """
    terms: list[str] = []
    for profile in profiles:
        chosen = [t for t in profile.themen if t.gewicht >= min_weight] or profile.themen
        terms.extend(t.begriff.replace("*", "").strip() for t in chosen)
    return list(dict.fromkeys(t for t in terms if t))


def load_profile(path: str | Path) -> Profile:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path.name}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"{path.name}: expected a mapping at the top level")
    return _parse(raw, path.name)


def load_profiles(directory: str | Path, only: str | None = None) -> list[Profile]:
    """Loads every profile in `directory`, or just the one whose id is `only`.

    A broken file fails loudly rather than being skipped: a silently ignored
    profile would look exactly like "this company had no matches this week".
    """
    directory = Path(directory)
    if not directory.exists():
        raise ProfileError(f"profile directory {directory} does not exist")

    profiles = [load_profile(p) for p in sorted(directory.glob("*.yaml"))]

    ids = [p.id for p in profiles]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ProfileError(f"duplicate profile id(s): {', '.join(sorted(duplicates))}")

    if only:
        profiles = [p for p in profiles if p.id == only]
        if not profiles:
            raise ProfileError(f"no profile with id {only!r} in {directory} (found: {', '.join(ids) or 'none'})")

    return profiles
