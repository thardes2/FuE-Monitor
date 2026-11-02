# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Text out of a PDF announcement.

German funding announcements are often only published as PDF — the Bundesanzeiger,
where every Förderrichtlinie is officially published, serves them that way at stable
URLs. Without this, those calls reach the monitor as a two-sentence newsletter
teaser: no funding rate, and too little text for profile matching to judge them on.
"""

from __future__ import annotations

import logging
import io
import re
from urllib.parse import urlparse

from pypdf import PdfReader
from pypdf.errors import PyPdfError

log = logging.getLogger(__name__)

# Below this, extraction didn't really work: a scanned-image PDF yields a handful of
# characters per page rather than nothing at all, which would otherwise be cached and
# evaluated as though it were the announcement's text.
MIN_USABLE_CHARS = 400

# PDF layout breaks words across lines ("For-\nschungs-", "Wirt-\nschaftsbeteiligung"),
# and leaving that in costs real matches: a profile looking for "Forschung" never sees
# "For- schungs", and a quote the model copied faithfully fails verification against
# text that is hyphenated differently. Joined only where the next line continues the
# same word — German compounds legitimately end a line with a hyphen before "und",
# "oder" and the like ("Forschungs- und Entwicklungsvorhaben"), which must stay.
_HYPHEN_BREAK = re.compile(r"(\w)-\n(?!und\b|oder\b|bzw\b|sowie\b|beziehungsweise\b)([a-zäöüß])")


def is_same_document_pdf(page_url: str, target_url: str) -> bool:
    """Whether `target_url` is the PDF download of the wrapper page at `page_url`.

    The Government Site Builder that bmv.de (and many other bund.de sites) run on
    publishes an attachment as an HTML wrapper "…/Anlage/E/foo.html" carrying its title
    and one download link, "…/Anlage/E/foo.pdf?__blob=publicationFile". Same host, same
    directory, same file stem: that is what identifies the download, so other PDFs the
    page links (forms, an English version, a factsheet) are never mistaken for it.
    """
    page, target = urlparse(page_url), urlparse(target_url)
    if page.netloc.lower() != target.netloc.lower():
        return False
    page_path = page.path[:-5] if page.path.lower().endswith(".html") else page.path
    return bool(page_path) and target.path.lower() == f"{page_path}.pdf".lower()


def looks_like_pdf(content: bytes, content_type: str = "") -> bool:
    """Whether this response is a PDF, by its own header rather than its URL.

    The Bundesanzeiger's publication URLs carry no ".pdf" suffix, so the only
    reliable signal is what comes back.
    """
    return content.startswith(b"%PDF-") or "application/pdf" in content_type.lower()


def extract_text(content: bytes) -> str | None:
    """Returns the PDF's text, or None if there isn't usably much of it.

    Never raises: a malformed or encrypted document degrades to None so the caller
    can fall back to whatever it already had.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PyPdfError, ValueError, OSError, KeyError, RecursionError) as exc:
        log.warning(f"    (could not read PDF: {type(exc).__name__}: {exc})")
        return None

    # Collapsed because PDF text extraction litters the output with line breaks
    # mid-sentence, which would otherwise break the quote verification that profile
    # matching does against this text.
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", "\n".join(pages)).strip()
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    if len(text) < MIN_USABLE_CHARS:
        log.warning(f"    (PDF yielded only {len(text)} characters — treated as unreadable)")
        return None
    return text
