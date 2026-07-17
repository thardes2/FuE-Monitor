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

  Each `brave`-mode source respects its own robots.txt `Crawl-delay` between page
  fetches, so a run with several hits from those sources takes longer.

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
run the scraped text through Gemini (structured JSON output, explicitly told to
return null rather than guess) and use whatever it finds, falling back to the
regex result for anything it doesn't. Setup (~1 min, free):
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) → create an API
key → paste into `gemini.api_key` in `config.local.yaml` (see below). Leave it
empty to use the regex-only extraction.

## Secrets (`config.local.yaml`)

API keys (Brave, Gemini) never go into the committed [config.yaml](config.yaml).
Instead, create a `config.local.yaml` next to it (already gitignored) with just
the keys you want to override, e.g.:

```yaml
gov_bekanntmachungen:
  brave_api_key: "..."
gemini:
  api_key: "..."
```

It's deep-merged on top of `config.yaml` at load time — see
[fue_monitoring/config.py](fue_monitoring/config.py).

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
