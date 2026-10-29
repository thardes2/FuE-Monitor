# FuE-Monitor

Collects R&D funding calls from multiple sources, filters them by keywords, funding
rate and deadline, and writes the result to an Excel file.

## Sources

- **EU Funding & Tenders Portal** – fully automatic, via the public search API.
- **German government funding-call announcements** ([gov_bekanntmachungen.py](fue_monitoring/sources/gov_bekanntmachungen.py))
  – a generic, config-driven connector for German federal ministries/agencies.
  Each source in `gov_bekanntmachungen.sources` in [config.yaml](config.yaml) was
  individually checked against its own robots.txt before being added, and uses one
  of two discovery modes:
    - `discovery: listing` – the site's own announcement listing page isn't
      disallowed, so we crawl it directly to find announcement links (e.g. BLE,
      BMFSFJ).
    - `discovery: brave` – the site's listing/search page IS disallowed (e.g.
      bmftr.bund.de's `/SiteGlobals/` path), but individual announcement pages are
      allowed, so we use the Brave Search API (`site:` query) to find them instead
      of ever crawling the disallowed page ourselves. (We originally used Google's
      Custom Search API for this, but Google has closed that API to new
      projects/customers.) One-time setup (~2 min):
      [api-dashboard.search.brave.com](https://api-dashboard.search.brave.com/) →
      sign up, add a payment method (required, but usage-based — ~$5 in free
      credits/month easily covers our volume) → create an API key → paste into
      `gov_bekanntmachungen.brave_api_key` in `config.local.yaml` (see below).
      Leave it empty to skip `brave`-mode sources; `listing`-mode sources still
      work without it.

  A `brave`-mode source searches **two** query sets and takes the union:
  `discovery_terms` (generic words like "Bekanntmachung"/"Förderrichtlinie", which
  enumerate broadly) plus your own `keywords` (which surface niche calls the
  generic terms rank out of reach). Neither alone is complete — measured on
  bmftr.bund.de: generic terms found ~65 announcements but missed a "KI in
  Wertschöpfungsketten" call that only a keyword query returned. Each term is
  paged through `search_pages` result pages, since Brave caps a page at 20 results
  and only some of them are announcements. Without this breadth, an announcement
  can silently drop out between runs when Brave's ranking shifts.

  Each `brave`-mode source respects its own robots.txt `Crawl-delay` between page
  fetches. That makes a cold run slow (~1.5 h for ~110 announcements at 30 s each
  plus an LLM call), which is what the cache below is for.

  Adding a new ministry/agency: check its robots.txt first (which paths, if any,
  are disallowed, and its `Crawl-delay`), find its announcement listing page (or
  confirm the listing page itself is disallowed, in which case use `brave` mode),
  then add an entry to `gov_bekanntmachungen.sources`.
- **German federal/state funding database** (foerderdatenbank.de, also covers NRW) –
  this site is protected by bot-detection and can't be queried automatically.
  Instead: search there in your browser, save the result page completely
  (Cmd+S → "complete webpage") and drop the `.html` file into the `input/` folder.
  It gets picked up automatically on the next run.

## Extraction quality (funding rate / deadline)

Every source except the EU portal is raw scraped text, and German funding-call
text phrases the funding rate and deadline in too many different ways for fixed
regex patterns to catch reliably. To improve on that, those sources optionally
run the scraped text through an LLM (structured JSON output, explicitly told to
return null rather than guess) and use whatever it finds, falling back to the
regex result for anything it doesn't — and for anything the backend fails on, so
an unreachable model never takes a run down.

Configured under `llm` in [config.yaml](config.yaml). `llm.backend` picks exactly
one of two backends — they're never both called:

| | `backend: "gemini"` | `backend: "ollama"` |
|---|---|---|
| Runs | Google's hosted API | local Ollama instance |
| Needs | an API key, subject to Google's quotas | Ollama running locally with the model pulled |
| Privacy | scraped page text is sent to Google | nothing leaves the machine |
| Speed | fast, network-bound | depends on local hardware |

- **Gemini setup** (~1 min, free): [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
  → create an API key → put it in `config.local.yaml` (see below).
- **Ollama setup**: have [Ollama](https://ollama.com) running and the model pulled
  (`ollama pull qwen3.6`), then set `llm.ollama.model` to a tag `ollama list`
  actually shows. `think: false` is set deliberately — with a reasoning model like
  qwen3.6, leaving thinking on spends most of the time and tokens on a thinking
  pass this extraction task doesn't need.

Set `llm.enabled: false` to skip the LLM entirely and use regex-only extraction.

**A missing deadline doesn't mean one thing.** 35 % of scraped pages state no
submission date, and `open_deadlines_only` keeps all of them, because a missing date
normally means "unknown" and excluding those would hide genuinely open programmes.
But some pages name no date for a reason: no application can be made on them at all.
A *Rahmenbekanntmachung* sets the frame and announces separate Förderaufrufe, and
those carry the real deadlines. So extraction also classifies `submission_mode`:

| | |
|---|---|
| `dated` | names submission dates (whether or not they have passed) |
| `continuous` | applications accepted at any time, no date given |
| `framework` | takes no applications itself — separate Förderaufrufe follow |

A `framework` page with no date is not treated as an open call and never costs an
evaluation. It isn't dropped either: each profile report ends with a short list of
the framework announcements that match the profile's themes, since those are exactly
the documents whose follow-up calls are worth watching. Relevance there is decided by
the cheap prefilter alone — listing all of them under every profile would move the
noise rather than remove it. A mode the model returns outside those three values is
read as unknown, which behaves like the old "keep it" default.

**Which date is "the" deadline is decided on read, not on extraction.** The model
is asked for *every* submission date a document names, and the one reported is the
next one still open — falling back to the last one if they have all passed, so the
`open_deadlines_only` filter recognises an expired call instead of treating it as
"deadline unknown". Two defects from one real run drove this:

- A call was reported as due **31.12.2036**. That was its `Geltungsdauer` — "ist bis
  zum Ablauf des 31. Dezember 2036 gültig" — while the actual deadline, 31 August
  2026, had already passed. The call read as open, and would have for ten years. The
  prompt now names the date kinds that are *not* deadlines: validity periods, the
  Bundesanzeiger publication date, project runtimes, and deadlines only selected
  applicants reach.
- A call stating "bis 30. November 2026 beziehungsweise 31. Mai 2028" was reported
  as 2028, hiding a deadline four weeks out from every urgency marker. An earlier
  prompt asked for the latest of several dates; that is the wrong one to show in a
  monitor whose purpose is telling you what to act on now.

Because the choice depends on today's date, the date list is what gets cached and
the pick is repeated on every read — otherwise a recurring call would drop out when
its next Stichtag passed and stay out until the entry expired.

**The extraction prompt has a version, and the page cache records it.** Entries note
`gemini/gemini-flash-lite-latest#p2` rather than the bare model: a corrected prompt
makes the stored fields *wrong*, not merely different, so those entries have to be
re-derived from their stored text. Bumping `EXTRACTION_PROMPT_VERSION` therefore
costs one re-extraction sweep (~30 min for a full source set, no re-crawling) and
nothing else. The match cache deliberately keys on the bare model instead: an
evaluation costs an LLM call and doesn't depend on the extraction prompt, so an
extraction fix must not throw those away.

**Transient backend errors are retried, not surrendered to.** A hosted backend
answers `503 UNAVAILABLE` when the shared capacity behind it is momentarily
exhausted — measured here at roughly one request in seven, evenly spread across a
run and with no `429` alongside, i.e. caused by Google's load rather than by our
request rate. Treating that as final cost the whole extraction for that page, so
`429` and `503` are now retried with exponential backoff (2 s, 6 s, 18 s) before
the regex fallback kicks in.

**The API key is sent as a header, never in the URL.** `requests` folds the query
string into the exception message, so a key passed as a query parameter ends up
in the run log on every backend error — it appeared 13 times in one measured run
before this was changed. If you have older logs lying around, check them.

**A quote is checked against the source, but not character by character.** Each
evaluation has to quote the passage it relies on, and that quote is looked for in
the scraped text — first exactly, then as a near match above a similarity threshold.
A quote that is off by a character or two is still a quote: one real report flagged
a passage as unsupported because the model wrote "nichtstatliche" where the source
says "nichtstaatliche", one missing letter in ~210 characters. That sends the reader
hunting through the document for a passage that is really there, which costs more
trust than it saves. An invented quote scores far below the threshold and is still
flagged, and a quote too short for a ratio to mean anything must still match exactly.

**Hallucination guard:** an extracted funding rate is discarded unless that exact
number actually appears next to a `%`/"Prozent" in the scraped text. An invented
rate would be worse than a blank one — a blank gets flagged as "not detected" for
manual review, while a made-up number looks like a real answer. Local models are
also non-deterministic, so repeated runs of the same document can differ; treat
the LLM-filled columns as a strong hint, not as verified fact.

## Marking calls as dealt with

The point of the **Review** column (first column of the Excel) is that you don't
have to work through the whole table again on every run. Pick `not relevant`
next to a call you've assessed and don't want to see again — the next run
remembers it, matched by the call's link, and leaves the row out entirely.

- The column has a **dropdown** offering the values from `review.dismissed_values`
  (default: `not relevant`, `done`, `no`, `x`), matched case-insensitively. The
  options live on a hidden `ReviewOptions` sheet, so long values or ones
  containing a comma work fine (an inline Excel list would break on both).
- The dropdown does **not** restrict input — you can still type anything. Other
  text (`applied`, `asking the department`, …) is kept and stays visible, so the
  column doubles as a notes field. That's why validation is added without an error
  message; making the list strict would take the notes function away.
- Renaming a value in `dismissed_values` orphans marks already made with the old
  wording — those rows reappear. Fixing this means editing the wording in **both**
  the Excel's Review column and `state/review.json`: the Excel wins on every run
  (it's what you last typed), so changing only the JSON gets overwritten straight
  back. Keeping the old wording in the list alongside the new one also works.
- An **empty** Review cell is what "not looked at yet" means. Each run prints how
  many of those are left.
- Set `review.hide_dismissed: false` to keep dismissed rows in the table instead
  of leaving them out.

Marks are mirrored into `state/review.json` (gitignored), because the Excel is
overwritten on every run — that file is what makes them survive a deleted, moved
or renamed spreadsheet. Delete it (and clear the column) to start over.

## Firmenprofile (company profiles)

Beyond the global table, calls can be matched against individual companies (SICP
members), producing one report per company.

```bash
python -m fue_monitoring.main --profiles           # every profile
python -m fue_monitoring.main --profile musterwerk # just this one
```

**One search pass, matched per profile.** Discovery uses the union of the running
profiles' themes (weight 2 and up, wildcards stripped) in place of the global
`keywords`; the sources' own `discovery_terms` still apply on top. Searching per
profile would repeat the source queries for every company, and those queries —
Brave in particular — are the metered part of a run, while page downloads are
already shared through the page cache.

No company sees calls it shouldn't: the prefilter admits a call only if that
profile's own themes match it, regardless of which term surfaced it. Profiles
skipped for lack of consent contribute no terms and so cost no queries. Worth
knowing: since the corpus depends on which profiles run together, a company's
report can differ between `--profile X` alone and `--profiles`.

The global Excel is **not** written in a profile run: it would be built from
profile-specific searches and would overwrite the real global table. Run the
command without either flag for that. Without either flag nothing in this section
runs.

### Profiles

One YAML per company in `profiles/`, modelled on
[profiles/example.yaml](profiles/example.yaml). **These files hold confidential
member data and are gitignored** — only the made-up `example.yaml` is in the
repository. Fields: `id`, `name`, `groesse` (`kmu`|`grossunternehmen`), `rolle`,
`themen` (each `begriff` + `gewicht` 1–3), `ausschluss`, `programme_bevorzugt`,
`eigenanteil_max_prozent`, `sicp_ansprechpartner`, `cloud_erlaubt`. A broken profile aborts the
run rather than being skipped, because a silently skipped company is
indistinguishable from one that simply had no matches.

### How matching works

**Stage 1 — prefilter (free).** Profile terms are matched on **word boundaries**,
unlike the global keyword list which matches substrings. That difference is the
point: `KI` as a substring also hits "Kinder" and "Kilometer", which is tolerable
in a table you scan yourself but not in a report that goes to a company. A
trailing `*` opts into German compounds — `Mobilität*` matches
"Mobilitätskonzept", plain `Mobilität` only the standalone word. Terms under
`ausschluss` drop a call outright, before any model sees it. The global keyword
list is deliberately **not** applied here: a company's themes are its own.

**Stage 2 — LLM evaluation (one local inference per surviving call).** The model
returns a fit score (0–100, weighted by your `gewicht`), a short German
rationale, an eligibility verdict (`ja`/`nein`/`unklar` with the concrete reason —
SME-only, mandatory consortium, own contribution too high) and a **verbatim quote**
backing that verdict. The quote is checked against the call text; one that can't
be found is reported as *unbelegt* rather than passed off as evidence, the same
stance the funding-rate guard takes.

**Confidentiality — consent is per profile.** Matching only runs on the local
`ollama` backend unless a profile opts in with `cloud_erlaubt: true`; such a
profile then uses whatever `llm.backend` is set to (e.g. `gemini`), and the run
states by name whose data goes to the hosted API.

With a hosted backend, profiles that haven't opted in are **skipped, not fatal** —
one company withholding consent doesn't block the others' reports. Skipped
profiles are always named (silently dropping one would read exactly like "no
matches this week") and they don't drive discovery either, so they cost no search
queries. Two cases still abort rather than skip: no usable backend at all (a
configuration fault, not a consent decision), and *no* selected profile permitting
the configured backend — otherwise the command would appear to succeed while
producing nothing.

**Cost:** one inference per (call, profile) pair that survives the prefilter.
Evaluations are cached per call link + profile fingerprint + prompt version, so
only genuinely new pairs cost time. The fingerprint covers the matching-relevant
fields only — renaming a contact keeps the cache, editing a theme invalidates it.
`profiles.max_evaluations_per_profile` caps a run; candidates are evaluated in
descending prefilter weight, so a cap spends the budget on the best ones. The
scoring prompt gets `profiles.max_text_chars` of the call (default 20000, not the
full document): measured ~24 s per evaluation at that size versus ~85 s at ~98000,
and fit and eligibility rest on the opening sections. Deadline and funding rate
are unaffected — those come from the extraction step, which reads the whole text.

### Output and marking

`output/<profil-id>/report.md` and `report.html` (self-contained, no external
assets). Marking works like the Excel's Review column, but via CLI since a report
isn't editable:

```bash
python -m fue_monitoring.main --mark musterwerk "https://…" "not relevant"
python -m fue_monitoring.main --mark musterwerk "https://…" ""   # clear it
```

State lives in `state/review_<profil-id>.json` and uses the same
`review.dismissed_values` as the global table.

### Programmes that can't be collected

[programmes_static.yaml](programmes_static.yaml) is a hand-maintained catalogue
for programmes behind bot-detection — currently the BMWE "DNS der zukunftsfähigen
Mobilität". Entries become ordinary calls and go through matching like the rest;
recurring cut-offs (`31.03.`, `30.09.`) resolve to the next one still inside
`gueltig_bis`. Keep `beschreibung` factual: it is what the model quotes from.

Note that these entries are prefiltered like any other call, so a profile only
sees one if its own themes appear in `beschreibung`. If a programme you added
here for a specific company doesn't reach it, that's why — either extend the
description (truthfully) or add a matching term to the profile.

### Tests

```bash
./.venv/bin/python3 -m pip install -r requirements-dev.txt
./.venv/bin/python3 -m pytest tests/ -q
```

## Cache

Announcement pages are static documents, but each uncached one costs its site's
robots.txt `Crawl-delay` plus an LLM call — so the first broad run takes about
1.5 hours, and almost all of that would be repeated on every subsequent run.
[cache.py](fue_monitoring/cache.py) stores fetched pages in `cache/pages.json`
(gitignored), which brings a follow-up run down to ~3 minutes: measured 100 of
102 announcements served from cache, only genuinely new ones downloaded.

Cached are the page text and the extracted fields, deliberately **not** the
keyword matches — those are recomputed every run, so editing `keywords` takes
effect immediately without invalidating the cache. Entries expire after
`cache.max_age_days`; delete `cache/pages.json` to force a full refresh (needed
if you change the LLM prompt or model, since extracted values are cached).

**Results are tied to the model that produced them.** Evaluations are keyed by
backend and model (`ollama/qwen3.8:latest`, `gemini/gemini-flash-lite-latest`, …)
alongside call link, profile fingerprint and prompt version — different models
score the same call differently, so without that a backend switch would blend two
models' verdicts into one report and switching back would resurrect the old ones.

Page-cache entries record their model too, but it is deliberately *not* part of
the key: the stored page text doesn't depend on the model, so after a switch the
fields are re-derived from that text instead of re-crawling every page at its
site's `Crawl-delay`. Such a sweep is throttled, since a hosted backend otherwise
runs into per-minute rate limits. Expect one such pass the first time you run
after a model change (no downloads, a few minutes).

**Two runs sharing a cache file don't erase each other.** Both caches read the
whole file at start, so writing it back wholesale meant the run that finished last
silently reverted the other one — including an interrupted one, since `save()` runs
from a `finally` block and Ctrl-C still reaches it. Observed in practice: a parallel
run that was cancelled cost ~30 freshly extracted entries their recorded model.
`save()` therefore re-reads the file and applies only the entries that run actually
wrote, and writes through a temp file so an interrupt mid-write can't truncate the
cache. This matters most for the match cache: two profiles evaluated concurrently
write disjoint keys, and an evaluation costs an LLM call, unlike a page that could
simply be fetched again.

**A failed LLM call is never cached.** When the backend is unreachable the
pipeline falls back to regex, and the regex can't read written-out dates like
"30. April 2026" — so the deadline comes out empty. Caching that would freeze a
transient outage in place for `cache.max_age_days`, and since a call with no
deadline passes the `open_deadlines_only` filter, an expired call would keep
surfacing as "Frist unbekannt". Observed in practice, hence the rule: such a page
is simply re-fetched next run. (If your cache predates this, re-run extraction
over the cached text to fill the gaps in — no re-crawl needed.)

A page whose re-extraction keeps failing is given up on after three attempts
(counted per entry in `llm_failures`). Without that it would queue again on every
single run, because its recorded model never catches up with the configured one.
Its previously extracted fields are kept rather than overwritten with regex-only
output — stale beats blank, for the reason just above.

## Secrets (`config.local.yaml`)

API keys (Brave, Gemini) never go into the committed [config.yaml](config.yaml).
Instead, create a `config.local.yaml` next to it (already gitignored) with just
the keys you want to override, e.g.:

```yaml
gov_bekanntmachungen:
  brave_api_key: "..."
llm:
  gemini:
    api_key: "..."
```

It's deep-merged on top of `config.yaml` at load time — see
[fue_monitoring/config.py](fue_monitoring/config.py). The `ollama` backend needs
nothing here, since it uses no credentials.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Configuration

All filter criteria live in [config.yaml](config.yaml): keywords, minimum funding
rate, whether expired deadlines are excluded, and after how many days a deadline is
marked as "urgent".

## Running

```bash
./.venv/bin/python3 -m fue_monitoring.main
```

The result is written to `output/funding_calls.xlsx` (colour-coded by deadline
status: expired/urgent/open/unknown, with an autofilter on every column).

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).
