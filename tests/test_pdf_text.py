# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Text out of a PDF announcement, and the enrichment built on it.

The Bundesanzeiger publishes every Förderrichtlinie as a PDF at a stable URL. That
is the only way BMWE's announcements reach this monitor with their full text, so the
newsletter teaser's two sentences grow into 6–69 kB of real Richtlinie.
"""

from __future__ import annotations

from datetime import date

import pytest

from fue_monitoring import http, llm_extract, pdf_text
from fue_monitoring.cache import PageCache
from fue_monitoring.sources import foerderinfo_newsletter as nl


# --- detecting a PDF -------------------------------------------------------


def test_a_pdf_is_recognised_by_its_header_not_its_url():
    """The Bundesanzeiger's publication URLs carry no .pdf suffix."""
    assert pdf_text.looks_like_pdf(b"%PDF-1.7\nrest", "")


def test_the_content_type_alone_is_enough():
    assert pdf_text.looks_like_pdf(b"", "application/pdf;charset=binary")


def test_an_html_page_is_not_mistaken_for_a_pdf():
    assert not pdf_text.looks_like_pdf(b"<!doctype html><html>", "text/html;charset=utf-8")


# --- de-hyphenation --------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("For-\nschungs- und Entwicklungseinrichtungen", "Forschungs- und Entwicklungseinrichtungen"),
    ("Wirt-\nschaftsbeteiligung", "Wirtschaftsbeteiligung"),
    ("Zuwendungs-\nempfänger", "Zuwendungsempfänger"),
])
def test_a_word_broken_across_lines_is_joined(raw, expected):
    """Measured in a real document. Left in, a profile looking for "Forschung" never
    sees "For- schungs", and a faithfully copied quote fails verification."""
    assert pdf_text._HYPHEN_BREAK.sub(r"\1\2", raw) == expected


def test_extract_text_actually_applies_the_de_hyphenation(monkeypatch):
    """Through extract_text, not just the pattern: a tested regex that nothing calls
    is worth nothing, and the earlier tests here passed with the call removed."""
    class Page:
        def extract_text(self):
            return "Antragsberechtigt sind For-\nschungseinrichtungen. " * 20

    class Reader:
        pages = [Page()]

    monkeypatch.setattr(pdf_text, "PdfReader", lambda stream: Reader())
    text = pdf_text.extract_text(b"%PDF-1.7")
    assert "Forschungseinrichtungen" in text
    assert "For-" not in text


@pytest.mark.parametrize("raw", [
    "Forschungs-\nund Entwicklungsvorhaben",
    "Forschungs-\noder Entwicklungsvorhaben",
    "Forschungs-\nbeziehungsweise Entwicklungsvorhaben",
])
def test_a_german_compound_keeps_its_hyphen(raw):
    """"Forschungs- und Entwicklungsvorhaben" must not become "Forschungsund"."""
    assert pdf_text._HYPHEN_BREAK.sub(r"\1\2", raw) == raw


# --- failure modes ---------------------------------------------------------


def test_a_corrupt_pdf_degrades_instead_of_raising():
    assert pdf_text.extract_text(b"%PDF-1.7 but then garbage") is None


def test_a_pdf_with_almost_no_text_is_treated_as_unreadable(monkeypatch):
    """A scanned-image announcement yields a few characters per page, not nothing —
    caching that would evaluate a call on noise."""
    class Page:
        def extract_text(self):
            return "Seite 1"

    class Reader:
        pages = [Page(), Page()]

    monkeypatch.setattr(pdf_text, "PdfReader", lambda stream: Reader())
    assert pdf_text.extract_text(b"%PDF-1.7") is None


def test_enough_text_comes_back(monkeypatch):
    class Page:
        def extract_text(self):
            return "Richtlinie zur Förderung. " * 40

    class Reader:
        pages = [Page()]

    monkeypatch.setattr(pdf_text, "PdfReader", lambda stream: Reader())
    text = pdf_text.extract_text(b"%PDF-1.7")
    assert text and len(text) >= pdf_text.MIN_USABLE_CHARS


# --- the host allowlist ----------------------------------------------------


def test_only_allowlisted_hosts_are_fetched():
    """The teasers link to a dozen domains; this project checks a site's robots.txt
    before fetching from it."""
    hosts = ["www.bundesanzeiger.de"]
    assert nl._host_allowed("https://www.bundesanzeiger.de/pub/publication/x", hosts)
    assert not nl._host_allowed("https://www.zim.de/call", hosts)
    assert not nl._host_allowed("https://evil.example/www.bundesanzeiger.de", hosts)


def test_a_call_on_an_unlisted_host_keeps_its_teaser(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("fetched a host that is not on the allowlist")

    monkeypatch.setattr(http.requests, "get", lambda url, **k: Resp(ARCHIVE) if "archive" in url else Resp(ISSUE))
    source = {"archive_url": "http://x/archive", "state_path": str(tmp_path / "s.json"),
              "crawl_delay": 0, "full_text_hosts": [], "full_text_delay": 0}
    calls = nl.fetch(source, [])
    assert calls and calls[0].note.startswith("Kurzmeldung")


# --- enrichment ------------------------------------------------------------


def test_the_full_text_replaces_the_teaser_and_brings_the_rate():
    call = nl._as_call(nl.Entry(title="T", link="http://x/1", ministry="BMWE",
                                kind="Bekanntmachung", summary="zwei Sätze",
                                deadline="2026-12-06"), [])
    nl._enrich(call, {"text": "Volltext " * 200, "funding_rate": "50 %",
                      "funding_rate_percent": 50.0, "deadline": date(2027, 1, 1)})

    assert call.funding_rate == "50 %" and call.funding_rate_percent == 50.0
    assert "Volltext" in call.text and "zwei Sätze" not in call.text
    assert "Volltext aus der verlinkten" in call.note


def test_the_newsletters_exact_deadline_wins_over_one_read_from_the_text():
    """The teaser's date comes from a structured datetime attribute naming the
    submission window. A date picked out of a full document has been measured
    mistaking a Geltungsdauer for a deadline."""
    call = nl._as_call(nl.Entry(title="T", link="http://x/1", ministry="BMWE",
                                kind="Bekanntmachung", summary="s",
                                deadline="2026-12-06"), [])
    nl._enrich(call, {"text": "Volltext " * 200, "funding_rate": "",
                      "funding_rate_percent": None, "deadline": date(2036, 12, 31)})
    assert call.deadline == date(2026, 12, 6)


def test_the_documents_deadline_is_used_when_the_teaser_had_none():
    call = nl._as_call(nl.Entry(title="T", link="http://x/1", ministry="BMWE",
                                kind="Bekanntmachung", summary="s", deadline=None), [])
    nl._enrich(call, {"text": "Volltext " * 200, "funding_rate": "",
                      "funding_rate_percent": None, "deadline": date(2027, 3, 15)})
    assert call.deadline == date(2027, 3, 15) and call.submission_mode == "dated"


def test_a_failed_fetch_leaves_the_teaser_intact():
    call = nl._as_call(nl.Entry(title="T", link="http://x/1", ministry="BMWE",
                                kind="Bekanntmachung", summary="zwei Sätze",
                                deadline="2026-12-06"), [])
    nl._enrich(call, None)
    assert "zwei Sätze" in call.text and call.deadline == date(2026, 12, 6)


# --- caching the document --------------------------------------------------


ARCHIVE = '<a href="/foerderinfo/shareddocs/newsletter/i1/i1.html">Ausgabe</a>'
ISSUE = """
<main><div class="newsletter-teaser">
  <h3 class="newsletter-teaser__heading">
    <a href="https://www.bundesanzeiger.de/pub/publication/abc/content/abc/B.pdf">Richtlinie</a>
    <small class="c-topline">
      <span class="c-topline__item"><time datetime="2026-10-19">19.10.2026</time></span>
      <span class="c-topline__item c-topline__item--stopdate"><time datetime="2026-12-06">- 06.12.2026</time></span>
      <span class="c-topline__item"><span class="c-topline__category">BMWE</span></span>
      <span class="c-topline__item">Bekanntmachung</span>
    </small>
  </h3><p>Teaser.</p></div></main>
"""


class Resp:
    def __init__(self, text="", status=200, content=b"", content_type="text/html"):
        self.text = text
        self.status_code = status
        self.content = content or text.encode()
        self.headers = {"content-type": content_type}
        self.url = "http://x/"


@pytest.fixture
def source(tmp_path) -> dict:
    return {"archive_url": "http://x/archive", "state_path": str(tmp_path / "s.json"),
            "crawl_delay": 0, "full_text_hosts": ["www.bundesanzeiger.de"],
            "full_text_delay": 0}


def test_a_document_is_downloaded_once_and_re_derived_afterwards(source, tmp_path, monkeypatch):
    """A model or prompt change must re-derive from the stored text, not re-download."""
    downloads: list[str] = []

    def fake_get(url, **kwargs):
        if "archive" in url:
            return Resp(ARCHIVE)
        if "bundesanzeiger" in url:
            downloads.append(url)
            return Resp(content=b"%PDF-1.7", content_type="application/pdf")
        return Resp(ISSUE)

    monkeypatch.setattr(http.requests, "get", fake_get)
    monkeypatch.setattr(nl.time, "sleep", lambda s: None)
    monkeypatch.setattr(pdf_text, "extract_text", lambda content: "Förderquote 50 %. " * 40)
    monkeypatch.setattr(llm_extract, "extract",
                        lambda *a, **k: {"funding_rate_percent": 50, "deadlines": [],
                                         "submission_mode": "dated", "title": None})

    cache_path = tmp_path / "pages.json"
    llm = {"backend": "gemini", "gemini": {"model": "m", "api_key": "k"}}

    cache = PageCache(cache_path)
    first = nl.fetch(source, [], llm, cache)
    cache.save()
    assert len(downloads) == 1
    assert next(c for c in first).funding_rate == "50 %"

    # A different model: the fields must be re-derived without a second download.
    downloads.clear()
    other = {"backend": "ollama", "ollama": {"model": "q"}}
    cache = PageCache(cache_path)
    nl.fetch(source, [], other, cache)
    assert downloads == [], "the stored PDF text must be reused"


def test_a_failed_extraction_is_not_cached(source, tmp_path, monkeypatch):
    """Otherwise a backend outage would freeze a rate-less result for 30 days."""
    monkeypatch.setattr(http.requests, "get",
                        lambda url, **k: Resp(ARCHIVE) if "archive" in url
                        else (Resp(content=b"%PDF-1.7", content_type="application/pdf")
                              if "bundesanzeiger" in url else Resp(ISSUE)))
    monkeypatch.setattr(nl.time, "sleep", lambda s: None)
    monkeypatch.setattr(pdf_text, "extract_text", lambda content: "text " * 200)
    monkeypatch.setattr(llm_extract, "extract", lambda *a, **k: None)

    cache = PageCache(tmp_path / "pages.json")
    calls = nl.fetch(source, [], {"backend": "gemini", "gemini": {"model": "m"}}, cache)

    assert cache.get("https://www.bundesanzeiger.de/pub/publication/abc/content/abc/B.pdf") is None
    assert "Teaser" in next(c for c in calls).text, "the teaser must survive"


def test_a_wrapper_page_is_followed_to_the_pdf_once(source, tmp_path, monkeypatch):
    """The newsletter sometimes links an older bookmark URL that redirects to an HTML
    page carrying the PDF link. The PDF URL is read from the page, not guessed."""
    wrapper = ('<a href="https://www.bundesanzeiger.de/pub/publication/abc/content/abc/'
               'BAnz%20AT%2019.10.2026%20B2.pdf">Download</a>')
    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(url)
        if "archive" in url:
            return Resp(ARCHIVE)
        if url.lower().endswith(".pdf") and "BAnz" in url:
            return Resp(content=b"%PDF-1.7", content_type="application/pdf")
        if "bundesanzeiger" in url:
            return Resp(wrapper, content_type="text/html")
        return Resp(ISSUE)

    monkeypatch.setattr(http.requests, "get", fake_get)
    monkeypatch.setattr(nl.time, "sleep", lambda s: None)
    monkeypatch.setattr(pdf_text, "extract_text", lambda content: "Volltext " * 200)

    calls = nl.fetch(source, [], None, PageCache(tmp_path / "pages.json"))
    assert "Volltext" in next(c for c in calls).text
    assert sum(1 for u in seen if "BAnz" in u) == 1, "one hop, not a crawl"


def test_a_wrapper_page_that_links_elsewhere_is_not_followed(source, tmp_path, monkeypatch):
    """One hop, same host only."""
    wrapper = '<a href="https://elsewhere.example/doc.pdf">Download</a>'

    def fake_get(url, **kwargs):
        if "elsewhere" in url:
            raise AssertionError("followed a link off the allowlisted host")
        if "archive" in url:
            return Resp(ARCHIVE)
        return Resp(wrapper, content_type="text/html") if "bundesanzeiger" in url else Resp(ISSUE)

    monkeypatch.setattr(http.requests, "get", fake_get)
    monkeypatch.setattr(nl.time, "sleep", lambda s: None)
    calls = nl.fetch(source, [], None, PageCache(tmp_path / "pages.json"))
    assert "Teaser" in next(c for c in calls).text
