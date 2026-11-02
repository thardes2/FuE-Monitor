# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""The BMV source: a site search as the listing, and calls that are PDF attachments.

bmv.de has no announcement listing, so its site search serves as one, once per term.
Each result is a Government Site Builder wrapper page (".../Anlage/E/foo.html")
whose call is the PDF "foo.pdf?__blob=publicationFile". The markup below follows
the real search result and wrapper pages, checked on 2026-11-02.
"""

from __future__ import annotations

import logging

import pytest

from fue_monitoring import dedup, http, pdf_text
from fue_monitoring.models import FundingCall
from fue_monitoring.sources import foerderinfo_newsletter as nl
from fue_monitoring.sources import gov_bekanntmachungen as gov

SITE = "https://www.bmv.de"
WRAPPER = f"{SITE}/SharedDocs/DE/Anlage/E/foerderaufruf-1-piodak.html"
PDF = f"{SITE}/SharedDocs/DE/Anlage/E/foerderaufruf-1-piodak.pdf?__blob=publicationFile"
PDF_TEXT = "Skizzen können ab dem 14.09.2026 bis zum 25.09.2026 eingereicht werden. " * 10


class Resp:
    def __init__(self, status=200, text="", content=None, headers=None, url=""):
        self.status_code = status
        self.text = text
        self.content = content if content is not None else text.encode()
        self.headers = headers or {"content-type": "text/html;charset=utf-8"}
        self.url = url


def pdf_resp(url=PDF):
    return Resp(content=b"%PDF-1.7\n...", headers={"content-type": "application/pdf"}, url=url)


def serve(monkeypatch, pages: dict):
    """Serves responses by URL and records what was requested. Unknown URL -> 404."""
    requested = []

    def fake_get(url, **kwargs):
        requested.append(url)
        return pages.get(url) or Resp(404, url=url)

    monkeypatch.setattr(http.requests, "get", fake_get)
    monkeypatch.setattr(gov.time, "sleep", lambda s: None)
    monkeypatch.setattr(pdf_text, "extract_text", lambda content: PDF_TEXT)
    return requested


def wrapper_page(stem="foerderaufruf-1-piodak", title="Erster Aufruf zur Skizzeneinreichung (PioDAK)"):
    return f"""<html><head><base href="https://www.bmv.de/"></head><body><main>
      <h1>{title}</h1>
      <a href="/SharedDocs/DE/Anlage/E/{stem}-formular.pdf?__blob=publicationFile">Formular</a>
      <a href="/SharedDocs/DE/Anlage/E/{stem}.pdf?__blob=publicationFile">Download</a>
    </main></body></html>"""


def result(stem, title):
    """One search hit: the title links the wrapper relatively, "Download" the PDF."""
    return f"""<div class="result">
      <a href="SharedDocs/DE/Anlage/E/{stem}.html?nn=76054">{title}</a>
      <a href="/SharedDocs/DE/Anlage/E/{stem}.pdf?__blob=publicationFile"><span>Download</span></a>
    </div>"""


def search_page(*results):
    return f"""<html><head><base href="https://www.bmv.de/"></head><body>
      <nav><a href="SharedDocs/DE/Anlage/Z/organisationsplan-bmv.html">Nav</a></nav>
      <main><h1>Suchergebnisse</h1>{''.join(results)}</main></body></html>"""


SEARCH = SITE + "/SiteGlobals/Forms/Suche/DE/Expertensuche_Formular.html?templateQueryString={term}"


def source(**overrides):
    config = {
        "name": "BMV",
        "discovery": "listing",
        "listing_url": SEARCH,
        "listing_terms": ["Förderaufruf"],
        "link_path_marker": "SharedDocs/DE/Anlage/",
        "link_text_include": ["aufruf", "richtlinie"],
        "link_text_exclude": ["factsheet"],
        "document_pdf": True,
        "crawl_delay": 0,
    }
    config.update(overrides)
    return config


# --- which PDF belongs to a wrapper page -------------------------------------


def test_the_download_with_the_wrappers_own_stem_is_its_document():
    assert pdf_text.is_same_document_pdf(WRAPPER, PDF)


@pytest.mark.parametrize("target", [
    f"{SITE}/SharedDocs/DE/Anlage/E/foerderaufruf-1-piodak-formular.pdf?__blob=publicationFile",
    f"{SITE}/SharedDocs/DE/Anlage/E/factsheet-piodak.pdf?__blob=publicationFile",
    f"{SITE}/SharedDocs/DE/Anlage/K/foerderaufruf-1-piodak.pdf?__blob=publicationFile",
    "https://elsewhere.example/SharedDocs/DE/Anlage/E/foerderaufruf-1-piodak.pdf",
])
def test_any_other_pdf_on_the_page_is_not(target):
    """A call's page also links forms, factsheets and English versions."""
    assert not pdf_text.is_same_document_pdf(WRAPPER, target)


# --- one document, two URLs ---------------------------------------------------


@pytest.mark.parametrize("pdf", [
    PDF,
    PDF + "&v=3",
    "https://bmv.de/SharedDocs/DE/Anlage/E/foerderaufruf-1-piodak.pdf?v=2&__blob=publicationFile",
])
def test_a_wrapper_and_its_download_are_one_announcement(pdf):
    """The newsletter links the PDF, the BMV source finds the wrapper."""
    assert dedup.canonical_link(pdf) == dedup.canonical_link(WRAPPER + "?nn=76054")


def test_a_plain_pdf_keeps_its_suffix_and_query():
    """Only the Site Builder's download URL is folded — anything else stays as it was."""
    assert dedup.canonical_link("https://x.de/a/foo.pdf?id=7") == "x.de/a/foo.pdf?id=7"


def test_the_newsletter_and_the_bmv_source_merge_into_one_entry():
    teaser = FundingCall(source="Förderinfo-Newsletter", title="PioDAK", link=PDF, text="kurz")
    crawled = FundingCall(source="BMV", title="PioDAK", link=WRAPPER, text=PDF_TEXT)

    merged = dedup.merge_duplicates([teaser, crawled])

    assert len(merged) == 1
    assert merged[0].text == PDF_TEXT, "the full document is the base"


# --- a search page as the listing ---------------------------------------------


def test_each_term_becomes_its_own_search_page():
    pages = gov._listing_pages(source(listing_terms=["Förderaufruf", "Richtlinie über Zuwendungen"]))
    assert pages == [
        SEARCH.replace("{term}", "F%C3%B6rderaufruf"),
        SEARCH.replace("{term}", "Richtlinie+%C3%BCber+Zuwendungen"),
    ]


def test_a_plain_listing_is_fetched_as_it_is():
    assert gov._listing_pages({"name": "X", "listing_url": "https://x.de/list"}) == ["https://x.de/list"]


def test_terms_without_a_placeholder_are_a_configuration_error():
    with pytest.raises(ValueError, match="{term}"):
        gov._listing_pages(source(listing_url="https://www.bmv.de/suche"))


@pytest.mark.parametrize("text,admitted", [
    ("Erster Aufruf zur Skizzeneinreichung", True),
    ("Bekanntmachung der Richtlinie über Zuwendungen", True),
    ("Factsheet/Kurzübersicht Förderrichtlinie PioDAK", False),   # excluded wins
    ("Organisationsplan Bundesministerium für Verkehr", False),   # not included
    ("Download", False),
])
def test_the_link_text_decides_what_is_followed(text, admitted):
    assert gov._link_text_admitted(text, ["aufruf", "richtlinie"], ["factsheet"]) is admitted


def test_without_an_include_list_everything_not_excluded_is_followed():
    assert gov._link_text_admitted("Anything at all", [], ["factsheet"])


def test_a_listing_link_carries_its_text_and_resolves_against_base(monkeypatch):
    serve(monkeypatch, {"https://www.bmv.de/s": Resp(text=search_page(result("foerderaufruf-1-piodak", "Erster Aufruf")))})

    links = dict(gov._listing_links("https://www.bmv.de/s", "SharedDocs/DE/Anlage/"))

    assert links[f"{SITE}/SharedDocs/DE/Anlage/E/foerderaufruf-1-piodak.html?nn=76054"] == "Erster Aufruf"
    assert links[PDF] == "Download"
    assert not any("organisationsplan" in u for u in links), "navigation is outside <main>"


# --- the text comes from the PDF ----------------------------------------------


def test_a_wrapper_page_yields_the_text_of_its_pdf(monkeypatch):
    requested = serve(monkeypatch, {WRAPPER: Resp(text=wrapper_page(), url=WRAPPER), PDF: pdf_resp()})

    data = gov._download_and_extract(WRAPPER, None, document_pdf=True)

    assert data["text"] == PDF_TEXT
    assert data["title"] == "Erster Aufruf zur Skizzeneinreichung (PioDAK)", "title from the wrapper"
    assert requested == [WRAPPER, PDF], "the form linked first is not fetched"


def test_a_wrapper_without_its_pdf_yields_nothing(monkeypatch):
    """Its own text is a title and a download button; an entry built on it would
    report "deadline not detected" for a call that has one."""
    page = wrapper_page().replace("foerderaufruf-1-piodak.pdf", "something-else.pdf")
    serve(monkeypatch, {WRAPPER: Resp(text=page, url=WRAPPER)})

    assert gov._download_and_extract(WRAPPER, None, document_pdf=True) is None


def test_a_download_that_is_not_a_pdf_yields_nothing(monkeypatch):
    serve(monkeypatch, {WRAPPER: Resp(text=wrapper_page(), url=WRAPPER), PDF: Resp(text="<html>error</html>")})

    assert gov._download_and_extract(WRAPPER, None, document_pdf=True) is None


def test_without_document_pdf_the_page_text_is_used_as_before(monkeypatch):
    requested = serve(monkeypatch, {WRAPPER: Resp(text=wrapper_page(), url=WRAPPER)})

    data = gov._download_and_extract(WRAPPER, None)

    assert "Erster Aufruf" in data["text"]
    assert requested == [WRAPPER]


# --- the whole source -----------------------------------------------------------


def test_the_source_follows_admitted_results_only(monkeypatch, caplog):
    search = search_page(
        result("foerderaufruf-1-piodak", "Erster Aufruf zur Skizzeneinreichung"),
        result("factsheet-piodak", "Factsheet Förderrichtlinie PioDAK"),
    )
    wrapper_url = f"{SITE}/SharedDocs/DE/Anlage/E/foerderaufruf-1-piodak.html"
    requested = serve(monkeypatch, {
        SEARCH.replace("{term}", "F%C3%B6rderaufruf"): Resp(text=search),
        wrapper_url: Resp(text=wrapper_page(), url=wrapper_url),
        PDF: pdf_resp(),
    })

    with caplog.at_level(logging.INFO, logger="fue_monitoring.sources.gov_bekanntmachungen"):
        calls = gov.fetch(source(), [], "", None, None)

    assert [c.link for c in calls] == [wrapper_url]
    assert calls[0].text == PDF_TEXT
    assert not any("factsheet" in u for u in requested), "a left-out result costs no request"
    # The factsheet counts once although it is linked twice (title and Download), and
    # the admitted call's own "Download" link is not counted as left out at all.
    assert "1 link(s) left out" in caplog.text


def test_one_unreadable_search_term_makes_the_source_unreachable(monkeypatch):
    """Otherwise the source would report a partial result as a complete one."""
    serve(monkeypatch, {SEARCH.replace("{term}", "F%C3%B6rderaufruf"): Resp(text=search_page())})

    with pytest.raises(gov.ListingUnavailable):
        gov.fetch(source(listing_terms=["Förderaufruf", "Förderrichtlinie"]), [], "", None, None)


# --- the newsletter reaches the same PDF ----------------------------------------


def test_the_newsletter_follows_a_bmv_wrapper_to_its_pdf(monkeypatch):
    serve(monkeypatch, {PDF: pdf_resp()})
    monkeypatch.setattr(nl.http, "get", lambda url, **k: http.requests.get(url))

    document = nl._follow_to_document(Resp(text=wrapper_page(), url=WRAPPER), ["www.bmv.de"])

    assert document is not None and document.url == PDF


def test_the_newsletter_does_not_follow_to_a_host_off_the_allowlist(monkeypatch):
    serve(monkeypatch, {PDF: pdf_resp()})

    assert nl._follow_to_document(Resp(text=wrapper_page(), url=WRAPPER), ["www.bundesanzeiger.de"]) is None
