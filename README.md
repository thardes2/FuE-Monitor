# R&D Funding Call Monitoring

Collects R&D funding calls from three sources, filters them by keywords, funding rate
and deadline, and writes the result to an Excel file.

## Sources

- **EU Funding & Tenders Portal** – fully automatic, via the public search API.
- **BMFTR/BMBF Bekanntmachungen** (German federal R&D call announcements) – fully
  automatic. Their own search page disallows automated crawling (robots.txt), so we
  find current call URLs via the Brave Search API (site-restricted to bmftr.bund.de,
  so we only ever touch pages Brave already indexed) and then fetch each call page
  directly, which robots.txt does allow. (We originally used Google's Custom Search
  API here, but Google has closed that API to new projects/customers.) One-time setup
  (~2 min): [api-dashboard.search.brave.com](https://api-dashboard.search.brave.com/)
  → sign up, add a payment method (required, but usage-based — ~$5 in free credits/month
  easily covers our volume of a few queries/day) → create an API key → paste into
  `brave_api_key` in [config.yaml](config.yaml). Leave it empty to disable this source
  (it's skipped automatically, no error).

  Note: fetching each call page waits 30s in between, per bmftr.bund.de's
  `Crawl-delay`, so a run with several hits takes a bit longer.
- **German federal/state funding database** (foerderdatenbank.de, also covers NRW) –
  this site is protected by bot-detection and can't be queried automatically.
  Instead: search there in your browser, save the result page completely
  (Cmd+S → "complete webpage") and drop the `.html` file into the `input/` folder.
  It gets picked up automatically on the next run.

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
