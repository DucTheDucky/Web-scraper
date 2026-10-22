# Web-scraper

This is our ADY group project for collecting and preparing real estate listing data in Da Nang. We will post project updates in this repository as we make progress.

The current program starts from a Batdongsan.com.vn results page, finds listing links, reads their details, and saves the results for later analysis.

## What it does

- Finds listing URLs and follows pagination automatically.
- Collects titles, asking prices, areas, addresses, dates, and other listing fields.
- Converts supported Vietnamese price formats into numeric VND values while keeping the original text.
- Saves successful listings and crawl progress in SQLite so interrupted runs can continue.
- Exports CSV with 27 columns, including the collection time and source results-page URL.
- Supports direct HTTP requests or browser loading through Playwright.

This version uses SQLite. It does not require SQL Server or an existing dataset to start a new run.

## Project structure

```text
Web-scraper/
|-- main/
|   `-- main.py                 # Scraper and command-line entry point
|-- requirements_danang.txt     # Base dependencies for HTTP mode
|-- requirements_browser.txt    # Base dependencies plus Playwright
|-- .gitignore                  # Excludes local environments and generated files
`-- README.md
```

The setup below creates `.venv/`. The example commands create `data/` automatically. Both stay local and are excluded from Git.

## Setup on Windows

Use Python 3.12 and PowerShell. The browser example also requires Microsoft Edge to be installed.

For a fresh clone:

```powershell
git clone https://github.com/DucTheDucky/Web-scraper.git
cd Web-scraper
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements_browser.txt
```

If you downloaded the repository as a ZIP, extract it and open PowerShell in the folder containing this README, then run the last two commands. Activation is not required because the examples call the environment's Python directly.

Create the environment on your own computer; a `.venv` copied from another computer may reference a missing Python installation.

## First collection

Run this from the repository root to collect up to 20 successful listings using Edge:

```powershell
.\.venv\Scripts\python.exe .\main\main.py `
  --fetcher playwright `
  --browser-channel msedge `
  --list-url "https://batdongsan.com.vn/nha-dat-ban-da-nang" `
  --max-pages 2 `
  --max-items 20 `
  --delay 10 `
  --no-contact-name `
  --state .\data\bds_state.sqlite `
  --output .\data\bds.csv
```

The scraper creates the database and output folder. You do not need to upload or download an old dataset first.

Collection is sequential, with at least 10 seconds between HTML requests. The item count is a cap on successful records, not a guarantee: unavailable listings, access blocks, or the end of pagination can stop collection earlier.

The earlier Task 11 browser configuration also used `--experimental-skip-gtm-bootstrap`. This optional flag suppresses one specific analytics bootstrap script before it is requested. It is disabled by default, and access-block detection remains active.

## Resume, export, or start again

To continue the example run, keep its SQLite file and use the same state/output paths. Browser options should be supplied again:

```powershell
.\.venv\Scripts\python.exe .\main\main.py --resume --fetcher playwright --browser-channel msedge --max-pages 0 --max-items 100 --no-contact-name --state .\data\bds_state.sqlite --output .\data\bds.csv
```

Limits count all completed pages and successful listings already saved in that database. Here, `--max-pages 0` removes the page cap, and `--max-items 100` sets the total success cap to 100.

To export saved results without accessing the website:

```powershell
.\.venv\Scripts\python.exe .\main\main.py --export-only --state .\data\bds_state.sqlite --output .\data\bds.csv
```

To start a separate run, add `--new-run` to the first collection command. It creates timestamped filenames and prints their actual paths. Use those actual paths when resuming or exporting that run.

For all options:

```powershell
.\.venv\Scripts\python.exe .\main\main.py --help
```

Without overrides, the script uses HTTP mode, two completed list pages, and 20 successful listings. Its default output paths are beside `main.py`; the examples above explicitly keep outputs under `data/`.

## Output files

| File | Purpose |
| --- | --- |
| `bds_state.sqlite` | Saved records, discovered URLs, and progress needed to resume. |
| `bds.csv` | Collected records for Excel or Python analysis. |
| `bds.csv.ady-owner.json` | Connects the CSV to its run and checks hashes before replacement. Keep it with the CSV. |
| `bds_issues.csv` | Generated when there are failed, skipped, or other issue records to report. |

Keep your generated files locally. `.gitignore` excludes them from commits; it does not delete them. If a CSV was edited outside the scraper, use a different `--output` filename when exporting again.

## Current status and limits

A previous local run produced 100 distinct listings with 27 CSV columns. That records a past result; it does not guarantee that the website will allow a new run today. The current source passed a syntax check during the latest local review, but a fresh end-to-end browser run was not repeated for this README.

- Prices are advertised asking prices, not confirmed transaction prices.
- Missing fields stay empty. Keyword-derived seller and corner-lot labels can be inaccurate.
- Listing IDs prevent repeated records within a run; different ads for the same property may still appear.
- Website layout changes can break parsing. The scraper stops on detected CAPTCHA or access blocks.
- The current repository does not include an automated test suite.

This is a coursework project in progress. Keep source URLs with the data and check the website's applicable terms before collecting or sharing its content.
