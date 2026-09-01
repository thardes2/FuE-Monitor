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
