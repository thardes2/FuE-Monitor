# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""A quote is checked against the source — strictly enough to catch an invented one.

The real case: a report flagged a quote as unsupported because the model wrote
"nichtstatliche" where the source says "nichtstaatliche". One missing letter in
~210 characters. Exact matching rejected it, which sends the reader hunting through
the source for a passage that is really there.
"""

from __future__ import annotations

import pytest

from fue_monitoring.matching import QUOTE_SIMILARITY_THRESHOLD, verify_quote

SOURCE = (
    "3 Zuwendungsempfänger Antragsberechtigt sind staatliche und nichtstaatliche "
    "Universitäten beziehungsweise Hochschulen, außeruniversitäre Forschungseinrichtungen "
    "sowie andere nicht gewinnorientiert arbeitende öffentliche oder private Einrichtungen "
    "wie Vereine und Verbände, die Forschungsbeiträge liefern können und den Zuwendungszweck "
    "sowie die Zuwendungsvoraussetzungen erfüllen. Es werden ausschließlich nichtwirtschaftliche "
    "Tätigkeiten der genannten Einrichtungen gefördert."
)

VERBATIM = (
    "Antragsberechtigt sind staatliche und nichtstaatliche Universitäten beziehungsweise "
    "Hochschulen, außeruniversitäre Forschungseinrichtungen sowie andere nicht "
    "gewinnorientiert arbeitende öffentliche oder private Einrichtungen wie Vereine und Verbände"
)


def test_the_real_case_a_single_missing_letter():
    """"nichtstatliche" instead of "nichtstaatliche" — this is what went wrong."""
    assert verify_quote(VERBATIM.replace("nichtstaatliche", "nichtstatliche"), SOURCE)


def test_a_verbatim_quote_still_passes():
    assert verify_quote(VERBATIM, SOURCE)


def test_whitespace_and_case_differences_still_pass():
    assert verify_quote("  ANTRAGSBERECHTIGT   sind\n staatliche\tund nichtstaatliche "
                        "Universitäten beziehungsweise Hochschulen,", SOURCE)


@pytest.mark.parametrize("slip,description", [
    ("Antragsberechtigt sind staatliche und nichtstaatliche Universitaeten beziehungsweise "
     "Hochschulen, außeruniversitäre Forschungseinrichtungen sowie andere nicht "
     "gewinnorientiert arbeitende öffentliche oder private Einrichtungen", "umlaut transliterated"),
    ("Antragsberechtigt sind staatliche und nichtstaatliche Universitäten bzw. "
     "Hochschulen, außeruniversitäre Forschungseinrichtungen sowie andere nicht "
     "gewinnorientiert arbeitende öffentliche oder private Einrichtungen", "abbreviation"),
])
def test_small_transcription_slips_pass(slip, description):
    assert verify_quote(slip, SOURCE), description


# --- the part that must not get weaker -------------------------------------


@pytest.mark.parametrize("invented", [
    "Antragsberechtigt sind ausschließlich Unternehmen der gewerblichen Wirtschaft mit Sitz in Deutschland",
    "Die Förderquote beträgt bis zu 80 Prozent der zuwendungsfähigen Ausgaben für kleine Unternehmen",
    "Gefördert werden Verbundvorhaben zwischen Industrie und Forschungseinrichtungen über drei Jahre",
])
def test_an_invented_quote_is_still_rejected(invented):
    """The whole point of the check. A plausible-sounding sentence that is not in the
    document must not come back as supported."""
    assert not verify_quote(invented, SOURCE)


def test_a_negated_claim_does_not_pass():
    """The nastiest failure would be a quote that reads as the source's opposite."""
    assert not verify_quote(
        "Antragsberechtigt sind staatliche und nichtstaatliche Universitäten beziehungsweise "
        "Hochschulen, jedoch keine außeruniversitären Forschungseinrichtungen und keine "
        "gewinnorientiert arbeitenden privaten Einrichtungen wie Vereine und Verbände",
        SOURCE,
    )


def test_a_short_phrase_must_match_exactly():
    """Too little substance for a ratio to mean anything."""
    assert verify_quote("Es werden ausschließlich nichtwirtschaftliche Tätigkeiten", SOURCE)
    assert not verify_quote("Es werden ausschliesslich nichtwirtschaftliche Taetigkeiten", SOURCE)


@pytest.mark.parametrize("empty", [None, "", "   ", "\n"])
def test_nothing_quoted_is_not_support(empty):
    assert not verify_quote(empty, SOURCE)


def test_a_quote_whose_words_are_absent_entirely_is_rejected():
    """No anchor in the text at all — the candidate search must come up empty rather
    than fall over."""
    assert not verify_quote("Korallenriffe in der Tiefsee werden systematisch kartiert und bewertet", SOURCE)


def test_the_threshold_is_not_set_so_low_that_it_waves_things_through():
    assert 0.9 <= QUOTE_SIMILARITY_THRESHOLD < 1.0


def test_verification_on_a_long_document_stays_cheap():
    """Anchored rather than scanning every position: a scraped announcement runs to
    tens of thousands of words, and a window at each one would cost more than the
    LLM call that produced the quote."""
    import time

    haystack = ((SOURCE + " ") * 400).replace("nichtstaatliche", "staatliche", 399)
    started = time.monotonic()
    verify_quote(VERBATIM.replace("nichtstaatliche", "nichtstatliche"), haystack)
    assert time.monotonic() - started < 2.0
