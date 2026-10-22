# Web-scraper

ADY group project for collecting and preparing property listing data in Da Nang.

The program starts from one Batdongsan.com.vn results page, discovers property links, follows pagination, and saves each successful property directly into Microsoft SQL Server. SQL Server also stores the queue and progress needed to resume an interrupted run.

```text
Results page -> discover links -> SQL Server queue
             -> fetch and clean each property
             -> commit property + snapshot + completed status in SQL Server
```

No CSV, SQLite, JSON sidecar, issues CSV, or diagnostic HTML data files are created. Existing files from the earlier version are left untouched and are not automatically imported.

## Features

- Collect many properties in a single run, with per-property commits.
- Extract 25 property fields plus `Scraped_At` and `Source_List_URL`.
- Preserve advertised price text and convert supported Vietnamese prices into numeric VND.
- Save normalized property data into the existing ADY table structure.
- Keep an exact JSON snapshot of all 27 collected fields inside SQL Server for each run.
- Resume pending work by run ID, with independent limits for each run.
- Update existing properties by `Post_ID` while retaining earlier run snapshots.
- Use Playwright with installed Microsoft Edge by default; HTTP remains available explicitly.
- Stop on detected CAPTCHA/access blocks and honor robots rules.
- Provide English help, comments, diagnostics, seller classifications and rental-period labels.

Website matching labels and keywords remain Vietnamese because the source website uses Vietnamese. Original property titles, addresses, price text, contact names, legal/direction labels and keyword evidence retain their source language.

## Files

```text
Web-scraper/
|-- main/
|   |-- main.py                  # Fetching, parsing and command-line entry point
|   |-- sqlserver_store.py       # SQL Server storage and transactions
|   `-- sqlserver_schema.sql     # Additive tables, indexes, columns and data view
|-- tests/
|   `-- test_scraper.py          # Offline and opt-in SQL Server integration tests
|-- requirements_danang.txt      # BeautifulSoup and pyodbc
|-- requirements_browser.txt     # Compatibility requirements entry point
|-- .gitignore
`-- README.md
```

## Setup on Windows

Use Python 3.11 or newer, installed Microsoft Edge, SQL Server, and Microsoft ODBC Driver 18 for SQL Server. The default connection uses Windows Authentication:

| Setting | Default |
| --- | --- |
| Server | `.\SQLEXPRESS` |
| Database | `ADY_Project` |
| Driver | `ODBC Driver 18 for SQL Server` |
| Authentication | Current Windows account |

The database must already exist. For a fresh installation, create it in SSMS using an account with permission:

```sql
CREATE DATABASE ADY_Project;
```

From the project root, install dependencies into your intended Python environment:

```powershell
python -m pip install -r .\requirements_danang.txt
python -c "import pyodbc; print(pyodbc.drivers())"
python .\main\main.py --check-db
```

`--check-db` connects and prepares/checks the additive schema without accessing the website or creating a collection run. The account needs schema creation/alteration permissions for setup and read/write permissions for collection. This local connection requests encryption and accepts the instance's certificate with `TrustServerCertificate=yes`.

For a fresh virtual environment, first run `python -m venv .venv` and use `.\.venv\Scripts\python.exe` instead of `python` in subsequent commands. Recreate environments on the destination computer; copied environments may reference a missing Python installation.

The base requirements include Playwright because browser loading is the default. The older browser requirements filename remains a compatible installation entry point:

```powershell
python -m pip install -r .\requirements_browser.txt
```

Use installed Edge with `--fetcher playwright --browser-channel msedge`. For bundled Chromium instead, install it with `python -m playwright install chromium`.

## Collect many properties

This starts a new run, uses Edge, and attempts up to 100 successful properties across as many listing pages as necessary:

```powershell
python .\main\main.py --new-run --max-pages 0 --max-items 100 --no-contact-name --fetcher playwright --browser-channel msedge
```

For explicit HTTP mode (currently challenged on the list page):

```powershell
python .\main\main.py --new-run --max-pages 0 --max-items 100 --no-contact-name --fetcher http
```

The program prints a UUID `Run ID`. Keep it to resume the same run. A plain invocation also starts a new run; `--new-run` makes that choice explicit.

A plain launch, including an editor Run Python File command, now uses Playwright and installed Microsoft Edge. Defaults are two completed list pages, 20 successful properties and a minimum 10-second interval between HTML request starts. `0` removes the relevant page/item cap. Limits count this run's saved work, including previous invocations of the same run. Collection is sequential; website access, parsing failures and available results can prevent reaching the cap.

Use `--list-url` to select a different supported sale/rental results page. A resumed run must keep its original results URL.

## Resume or inspect runs

Replace `RUN_ID` with the UUID printed by the program:

```powershell
python .\main\main.py --resume RUN_ID --max-pages 0 --max-items 100 --no-contact-name --fetcher playwright --browser-channel msedge
python .\main\main.py --runs
```

Supply the fetcher, browser and privacy options again when resuming. Eligible failed properties are requeued until their stored failed-attempt count reaches three. An access-blocked property/page remains pending.

Use a different SQL Server/database with:

```powershell
python .\main\main.py --server '.\SQLEXPRESS' --database ADY_Project --check-db
```

Supply the same connection options when resuming or listing runs. Only one active process may use a particular run ID. Different runs have independent queues and counters.

The previous `--state`, `--output` and `--export-only` options have been removed. `--resume` now requires a SQL Server run UUID rather than a local state file.

## Inspect collected data in SSMS

```sql
USE ADY_Project;

-- Current property values across all runs, with 27 readable field names:
SELECT * FROM dbo.batdongsan_data;

-- Recent runs:
SELECT * FROM dbo.crawl_runs ORDER BY created_at DESC;

-- Progress for one run:
DECLARE @run_id uniqueidentifier = 'PUT-THE-PRINTED-RUN-UUID-HERE';
SELECT status, COUNT(*) AS ItemCount
FROM dbo.crawl_queue WHERE run_id = @run_id GROUP BY status;

-- Original 27-field snapshots from that run, stored inside SQL Server:
SELECT post_id, scraped_at, source_list_url, data
FROM dbo.crawl_results WHERE run_id = @run_id;
```

The normalized tables are `provinces`, `districts`, `wards`, `legal_statuses`, `directions`, `contacts`, `listings` and `listing_audits`. Progress tables are `crawl_runs`, `crawl_list_pages`, `crawl_queue` and `crawl_results`. Schema setup retains existing property rows and adds missing objects/provenance columns. It is designed for this project's schema, not unrelated databases with conflicting table definitions.

Each successful property, its related normalized records, its run snapshot and its queue completion commit in one transaction. A failure rolls them back together. Previously committed properties survive interruption; resume the same run to retry unfinished work. A new run updates an existing property's current values by `Post_ID` while preserving snapshots from prior runs.

## Tests

Offline tests:

```powershell
python -B -m unittest discover -s tests -v
```

SQL Server integration tests are opt-in. They create and drop a uniquely named `ADY_Task11_Test_<UUID>` database on `.\SQLEXPRESS`, requiring database creation/deletion permissions. They do not insert test properties into `ADY_Project`:

```powershell
$env:ADY_RUN_SQL_TESTS = '1'
python -B -m unittest discover -s tests -v
Remove-Item Env:\ADY_RUN_SQL_TESTS
```

Set `ADY_TEST_SERVER` to use another instance for integration tests.

## Validation and limitations

On 2026-10-22, all 32 tests passed, including 15 tests against a disposable SQL Server database. Checks cover multi-property collection, exact snapshots, field mappings, large VND values, nulls, per-run counters, duplicate handling, rollback, Ctrl+C, exclusive run ownership, fresh-process resume without local dataset files, browser defaults, and exact optional-telemetry response handling.

The actual `ADY_Project` schema was prepared and its 14 existing properties were preserved. A later live browser test with the Australian VPN disconnected read one listing page, discovered 29 property URLs, and committed one real property (Post_ID `46442454`) with all 27 fields directly into SQL Server. That initial checkpoint had 15 properties and 28 pending URLs in its test run. Contact names were omitted. No local dataset files were created.

## Cloudflare 403 and VPN troubleshooting

The successful configuration tested on 2026-10-22 is **VPN disconnected + Playwright + installed Microsoft Edge**:

```powershell
python .\main\main.py --new-run --max-pages 0 --max-items 100 --no-contact-name --fetcher playwright --browser-channel msedge
```

A controlled comparison with the same scraper request headers found:

| Connection / fetcher | robots.txt | Listing page |
| --- | --- | --- |
| Australian VPN, HTTP | Cloudflare challenge / 403 | Cloudflare challenge / 403 |
| Australian VPN, existing Edge fetcher | Cloudflare challenge / 403 | Not attempted by the crawler |
| VPN disconnected, HTTP | 200 text/plain; selected list allowed | Cloudflare challenge / 403 |
| VPN disconnected, existing Edge fetcher | Loaded successfully | Parsed successfully; one detail saved |

The VPN route contributed to the robots access failure. The precise Cloudflare security rule/IP reputation is not exposed in its response. Disabling the VPN alone did not make the plain HTTP listing request work; the tested browser mode was also needed. No user-agent spoofing, CAPTCHA solver, proxy rotation or robots-check removal was introduced. Normal browser loading uses the program's existing identified user agent and isolated context. No account login or existing browser cookies were used.

Cloudflare challenges are identified by `cf-mitigated: challenge`, rather than treated as robots directives. The diagnostic now includes a Ray ID and connection/browser guidance. The scraper still stops on challenges; this successful result does not guarantee future access. Cloudflare's troubleshooting guide recommends temporarily disabling a VPN/proxy for comparison: https://developers.cloudflare.com/cloudflare-challenges/troubleshooting/challenge-solve-issues/

Prices are advertised asking prices. Missing values remain unknown. Seller and corner-lot classifications use keywords and may be inaccurate. Website layout changes can break parsing. Keep source URLs and check applicable website terms before collecting or sharing content.


### Optional analytics response fix

A follow-up test identified a separate error: a late CDP response from the exact optional first-party analytics collector `https://batdongsan.com.vn/gtganalytics/ag/g/c` was being treated as a required-resource failure. Request routing already excluded this collector, but response handling did not apply the same classification.

The response handler now applies the same exact endpoint and resource-type check before classifying access errors. It discards only the recognized optional collector and exact bootstrap responses. Main property/list documents, required scripts and even similar analytics paths still stop the run on access challenges; regression tests cover these boundaries. This does not solve or bypass a challenge on a property page.

Plain launches now default to `--fetcher playwright --browser-channel msedge`; the command below explicitly selects the same settings:

```powershell
python .\main\main.py --new-run --max-pages 0 --max-items 100 --no-contact-name --fetcher playwright --browser-channel msedge
```

Live follow-up validation on 2026-10-22 resumed failed run `9034306e-6faa-494e-bfc2-b1326ff859ec` with no explicit fetcher/channel flags, exercising the new Edge defaults. It saved three real properties with 27-field server snapshots and no contact names, including the previously blocked property `46454429`. At that checkpoint the run had three done and 26 pending URLs, and the database held 17 distinct properties; one of the three was already present and was updated by Post_ID.


### Exact analytics bootstrap is now skipped by default

The related script at exactly `https://batdongsan.com.vn/gtganalytics/` can itself return a Cloudflare 403 even while property HTML is accessible. A live comparison confirmed that loading this analytics script could stop collection, while omitting it allowed the same pending list page and its first three properties to parse and save correctly. The previous fix covered the collector endpoint but left this bootstrap optional and disabled for skipping by default.

Browser mode now skips this exact script by default at request, redirect-target and late-response boundaries. This setting is selected automatically when you run `main.py` from an editor or a terminal. The script must match the exact HTTPS URL and resource type; this is not a general exception for scripts, analytics domains or 403 responses. Actual list/property access challenges still stop collection.

- `--skip-analytics-bootstrap`: explicitly select the default.
- `--experimental-skip-gtm-bootstrap`: older compatible alias for the same setting.
- `--load-analytics-bootstrap`: opt back into loading the script for diagnostics; it may reproduce the analytics failure.

Printed PowerShell resume commands use absolute interpreter/script paths, work from any folder and preserve the selected bootstrap policy. No account login, existing browser cookies, CAPTCHA solver, stealth plugin, proxy rotation or local dataset export is involved.

Latest live verification on 2026-10-22 resumed user-reported failed run `e4bdb1b6-4972-4aa6-b557-6d29e72dc896`. The old explicit bootstrap-skip option first enabled three successful property saves. The new default policy then saved seven additional properties without an explicit analytics/fetcher flag. The run reached ten saved properties, zero failed/skipped properties and 19 pending URLs. All ten server snapshots have 27 fields, matching normalized property identities and blank contact names. The database now contains 24 distinct properties. This is a verified bounded run, not a guarantee against future website challenges.
