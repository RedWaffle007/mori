# Step 4 — Address Validation Pipeline

A web app for **Step 4** of a data-enrichment pipeline. You upload an Excel file
of companies; the backend scrapes each company website with **ScrapeGraphAI**,
extracts and validates **UK addresses**, and returns a downloadable validated
Excel workbook.

## How it works

```
Upload .xlsx  ──►  Phase 1: scrape websites (ScrapeGraphAI)
                   Phase 2: extract addresses (Anthropic Messages API)
                   Phase 3: filter non-UK addresses (country + postcode)
                   Phase 4: compare/replace Excel columns → *_validated.xlsx
              ◄──  Download validated workbook
```

Each upload becomes a **job** with a UUID and an on-disk directory under
`backend/jobs/{job_id}/`. All intermediate checkpoint files live there, so a job
survives a server restart and can be **resumed** if interrupted. Scraping and
extraction checkpoints are append-only JSONL files (`scraped_content.jsonl`,
`extracted_addresses.jsonl`) — resuming simply re-reads them and continues from
the last completed row.

```
backend/jobs/{job_id}/
├── input.xlsx
├── scraped_content.jsonl
├── extracted_addresses.jsonl
├── scraped_sources.json
├── uk_addresses.json
├── skipped_addresses.json
├── scrape_errors.log
└── output/
    └── input_validated.xlsx
```

## Project layout

```
mori/
├── backend/
│   ├── main.py              # FastAPI app (also serves the frontend)
│   ├── requirements.txt
│   ├── .env                 # ANTHROPIC_API_KEY, SGAI_API_KEY, TOMBA_API_KEY, TOMBA_SECRET
│   ├── jobs/                # per-job working directories (created at runtime)
│   └── pipeline/            # pipeline scripts for Steps 1–4
└── frontend/
    ├── index.html
    ├── step1.html
    ├── step2.html
    ├── step3.html
    ├── step4.html
    ├── style.css
    └── app.js
```

## Install

```bash
cd mori
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
```

> ScrapeGraphAI uses Playwright for the `headless=False` retry path. If you hit a
> browser error during scraping, install the browser binaries once:
>
> ```bash
> playwright install
> ```

## Configure `.env`

Edit `backend/.env` and set your Anthropic API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Run

The frontend is now served **directly by FastAPI** — there is no separate
frontend server to start. Only **one command** is needed:

```bash
uvicorn backend.main:app --reload
```

Then open <http://localhost:8000> in your browser. The backend serves
`index.html` at `/`, each page at `/step1.html` … `/step4.html`, and the shared
assets (`/style.css`, `/app.js`). The whole `frontend/` directory is also mounted
at `/static/` (e.g. `/static/style.css`).

## Sharing externally via ngrok

To share the app with external users, expose the single FastAPI port with
[ngrok](https://ngrok.com). Only **one tunnel** is needed — the frontend and API
are on the same origin:

```bash
ngrok http 8000
```

ngrok prints a public URL (e.g. `https://abcd-1234.ngrok-free.app`); open it in a
browser and the whole app works through it.

- **No client-side configuration is needed.** The
  `ngrok-skip-browser-warning` header middleware is **already built in** to the
  backend, so ngrok's free-tier interstitial warning page never intercepts the
  page load or any API call, on any browser.
- **Free ngrok URLs change on every restart.** Each time you restart `ngrok`,
  send the new URL to anyone who needs access.

## API base URL

The frontend uses `window.location.origin` dynamically as its API base URL, so it
works on **localhost, an ngrok URL, or any other domain** with no code changes —
every request goes back to whatever host served the page.

## Using the app

1. On load, any **interrupted** jobs appear as cards with a **Resume** button.
2. Choose an `.xlsx` file and click **Run Step 4**. The returned **job ID** is
   shown so you can note it.
3. The page polls status every 4 seconds and shows a spinner with the current
   **per-row progress message**, e.g.:

   ```
   Phase 1 — Scraping: 42 / 350 (example.com)
   Phase 2 — Extracting: 120 / 350 (row 15)
   ```

4. When the job is **complete**, a **Download Output** button appears.
5. On **error**, the message is shown in red.

Only **one job runs at a time** — starting/resuming while another is running
returns HTTP `409`.

## Cost tracking

Both API-calling phases are priced at **Haiku 4.5 batch rates**:

| Tokens  | Rate              |
|---------|-------------------|
| Input   | $0.50 per 1M      |
| Output  | $2.50 per 1M      |

- **Phase 2 (extraction)** is tracked **per row**: `extract_addresses.run()`
  records each row's input/output tokens and cost, keyed by row index.
- **Phase 1 (scraping)** cost is **derived** by the backend as
  `total tokens − Phase 2 tokens`. This is accurate because Phases 3 and 4 make
  **zero** API calls, so any non-Phase-2 tokens in the pipeline total belong to
  Phase 1.
- The backend computes **Phase 1 cost**, **Phase 2 cost**, and **total cost**,
  and stores them (with the per-row Phase 2 costs) on the job.

Costs are returned in the `summary` field of the status response (see below) and
shown in the frontend summary card. **Nothing is written to the output Excel.**

## Output summary

When a job reaches `complete`, `GET /api/step1/status/{job_id}` includes a
`summary` object:

```jsonc
{
  "job_id": "…",
  "status": "complete",
  "message": "Complete",
  "summary": {
    "results": {
      "processed": 320,
      "matched": 140,
      "updated": 95,
      "original": 40,
      "skipped_non_uk": 25,
      "skipped_no_address": 20,
      "no_website": 30
    },
    "phase1_cost": 0.0000,
    "phase2_cost": 0.0123,
    "per_row_phase2_costs": {
      "0": { "input_tokens": 850, "output_tokens": 60, "cost": 0.000575 },
      "1": { "input_tokens": 910, "output_tokens": 55, "cost": 0.0006 }
    },
    "total_cost": 0.0123
  }
}
```

The frontend renders the **results breakdown** and the **cost breakdown**
(Phase 1, Phase 2, total) in a card below the Download button. The per-row
Phase 2 costs are available in the response but are **not** shown in the UI.

## Input file expectations

- A `website` column (case-insensitive) is required.
- Optional address columns (`add1`, `add2`, `town`, `county`, `postcode`) are
  compared against the scraped address and replaced when they differ.
- An optional `business name` / `company` / `name` column is used for progress
  labels.

The output workbook adds `address_status` and `skip_reason` columns plus
`Address Summary`, `Ready`, and `Skipped` sheets.

## API reference

| Method | Endpoint                       | Purpose                                  |
|--------|--------------------------------|------------------------------------------|
| POST   | `/api/step1/run`               | Upload `.xlsx`, start a job (`409` if busy) |
| POST   | `/api/step1/resume/{job_id}`   | Resume an interrupted job (`404`/`409`)  |
| GET    | `/api/step1/status/{job_id}`   | `{ job_id, status, message, summary }` — `message` carries live per-row progress; `summary` (results + costs) is present once `complete` |
| GET    | `/api/step1/download/{job_id}` | Stream the validated workbook (`404` if not ready) |
| GET    | `/api/step1/jobs`              | List all Step 4 jobs (for the resume UI)  |

---

# Step 1 — URL Discovery

A second, independent step that runs **before** Step 4. You upload an Excel file of
**business names**; the backend discovers each company's website with
**ScrapeGraphAI Search** and returns a workbook of validated URLs. It is reached
from the landing page (`frontend/index.html`) via the **Step 1 — URL Discovery**
card, which opens `frontend/step1.html`. (Step 4 now lives on its own page,
`frontend/step4.html`; the landing page links to both.)

The two pipeline scripts live in `backend/pipeline/`:

- `sgai_url_finder.py` — Stage 1
- `low_promoter.py` — Stage 2

## `SGAI_API_KEY`

Stage 1 calls the ScrapeGraphAI Search API, which needs an API key:

1. Sign up / sign in at <https://scrapegraphai.com> and copy your API key from the
   dashboard.
2. Add it to `backend/.env` alongside the existing `ANTHROPIC_API_KEY`:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   SGAI_API_KEY=your_scrapegraphai_api_key
   ```

The backend loads `.env` and exports `SGAI_API_KEY` into the environment before
running the pipeline. If the key is missing, Stage 1 stops immediately and the job
ends in **error**.

## The two-stage pipeline

```
Upload .xlsx  ──►  Stage 1 (sgai_url_finder.py)
                     · for each business name, search ScrapeGraphAI (2 results, geo=gb)
                     · validate each candidate URL against a blacklist, non-UK TLD
                       rules, sub-page depth, and company-name token matching
                     · checkpoint every row → resumable
                     · writes input_sgai_urls.xlsx (two sheets)
                   Stage 2 (low_promoter.py)
                     · re-scores ONLY the LOW-confidence rows with an improved
                       tokenizer (alphanumeric splitting, initials, full-name
                       substring, expanded industry "weak" words) — no API calls
                     · rows that now reach HIGH/MEDIUM are moved into the with_url
                       sheet and flagged promoted = "yes"
                     · writes input_sgai_urls_promoted.xlsx
              ◄──  Download input_sgai_urls_promoted.xlsx
```

Like Step 4, each upload becomes a **job** with a UUID directory under
`backend/jobs/{job_id}/`. Stage 1 checkpoints every processed row to
`sgai_url_checkpoint.json`, so an interrupted job **resumes automatically** from
where it stopped. If `input_sgai_urls.xlsx` already exists when a job is resumed,
Stage 1 is **skipped** and only Stage 2 runs.

```
backend/jobs/{job_id}/
├── input.xlsx
├── sgai_url_checkpoint.json          # Stage 1 per-row checkpoint (resume)
├── blacklist.json                    # blacklist additions (if any)
├── blacklist_candidates.txt          # auto-flagged domains to review
├── input_sgai_urls.xlsx             # Stage 1 output (checkpoint artifact)
└── output/
    └── input_sgai_urls_promoted.xlsx # Stage 2 output — served for download
```

The uploaded file's **first sheet** is detected automatically (it does not need to
be named `Sheet1`), and the company-name column is auto-detected from any column
containing "name", "company", or "business".

### Output sheets

Both Stage 1 and Stage 2 write a workbook with two sheets, named from the detected
input sheet:

- `{sheet}_with_url` — rows with a **HIGH** or **MEDIUM** confidence URL.
- `{sheet}_no_url` — rows with **LOW** or **DISCARD** (no usable URL).

Each row carries `found_url`, `url_title`, `url_confidence`, and `url_reason`.

## `url_confidence` values

| Value     | Meaning                                                                                 |
|-----------|-----------------------------------------------------------------------------------------|
| `HIGH`    | A distinctive word from the company name appears in the domain — confident match.       |
| `MEDIUM`  | Only generic/industry ("weak") words match the domain — plausible but verify.           |
| `LOW`     | Weak or no name-token match — re-evaluated in Stage 2, otherwise not used.               |
| `DISCARD` | Rejected outright: blacklisted directory/registry, non-UK country TLD, deep sub-page, or no result. |

## The `promoted` column

Stage 2 adds a `promoted` column to every row:

- `promoted = "yes"` — the row was **LOW** after Stage 1 and the improved matcher
  rescued it to **HIGH** or **MEDIUM**; it has been moved into the `with_url` sheet.
- `promoted = ""` (blank) — the row's confidence was unchanged by Stage 2 (it was
  already HIGH/MEDIUM, or stayed LOW/DISCARD).

## Output summary

When a Step 1 job reaches `complete`, `GET /api/step1url/status/{job_id}` includes a
two-part `summary` computed from the output workbooks:

```jsonc
{
  "job_id": "…",
  "status": "complete",
  "message": "Complete",
  "summary": {
    "stage1": { "high": 0, "medium": 0, "low": 0, "discard": 0, "total": 0 },
    "stage2": {
      "promoted_high": 0,
      "promoted_medium": 0,
      "remaining_low": 0,
      "discard": 0,
      "final_with_url": 0   // HIGH + MEDIUM after both stages combined
    }
  }
}
```

The frontend renders this as a two-part card (Stage 1 / Stage 2) below the
**Download Output** button.

## Step 1 API reference — prefix `/api/step1url/`

| Method | Endpoint                          | Purpose                                                   |
|--------|-----------------------------------|-----------------------------------------------------------|
| POST   | `/api/step1url/run`               | Upload `.xlsx`, run the two-stage pipeline (`409` if any job is busy) |
| POST   | `/api/step1url/resume/{job_id}`   | Resume an interrupted job — Stage 1 resumes from its checkpoint, or is skipped if Stage 1 output already exists |
| GET    | `/api/step1url/status/{job_id}`   | `{ job_id, status, message, summary }` — `message` carries live Stage 1/Stage 2 progress |
| GET    | `/api/step1url/download/{job_id}` | Stream the `*_promoted.xlsx` workbook (`404` if not ready) |
| GET    | `/api/step1url/jobs`              | List all Step 1 URL jobs (for the resume UI)              |

> **One job at a time across all steps.** Starting or resuming any Step 1 or Step 4
> job while another is running returns HTTP `409`.

---

# Step 2 — Sector Classification

A third, independent step that runs **after** Step 1. You upload the **promoted
workbook** produced by Step 1 and the backend classifies every company into one
of four sectors with **Claude**. It is reached from the landing page
(`frontend/index.html`) via the **Step 2 — Sector Classification** card, which
opens `frontend/step2.html`.

The pipeline script lives at `backend/pipeline/sector_classifier.py`.

## What it does and which signals it uses

For each row, Claude is given up to four signals and asked to pick exactly one
sector:

- **Business name** — from the `Business Name` column.
- **URL** — the discovered website (`found_url`).
- **Page title** — the `title` captured in Step 1's scrape.
- **Website content** — the `sg_content` homepage text captured in Step 1
  (capped at 3,000 characters per row).

Page title and website content come from Step 1's `sgai_url_checkpoint.json`,
matched to each row by URL. Rows with no scraped content fall back to
name + url + title; rows with no checkpoint at all fall back to name + url.

**Required columns.** The classifier reads `found_url` and `Company Number`
directly from every row, so both columns must be present in the uploaded
workbook. `Business Name` is read as a signal when present.

## The two modes

| Mode           | Rows        | API          | Speed                | Cost          |
|----------------|-------------|--------------|----------------------|---------------|
| **Test**       | first 20    | live Messages API | instant         | full price    |
| **Full Batch** | all rows    | Batch API    | async (up to 24h)    | **50% off**   |

- **Test** classifies the first 20 rows synchronously so you can sanity-check the
  output before committing to a full run.
- **Full Batch** submits every row to the Anthropic Batch API. Submission returns
  a **batch ID** immediately; results are downloaded later via **Check Results**.

## Step 1 job linking — why the checkpoint matters

When you submit a Step 2 job you provide two things:

1. The **promoted `.xlsx`** from Step 1.
2. A **selected Step 1 job** (from the dropdown of completed Step 1 URL jobs).

Before running, the backend copies `sgai_url_checkpoint.json` from
`jobs/{step1_job_id}/` into the new Step 2 job directory. That checkpoint holds
the **page titles and scraped homepage content** Step 1 gathered, which are the
richest classification signals.

If the selected Step 1 job has **no checkpoint** (e.g. it predates that artifact),
Step 2 proceeds anyway — the classifier simply falls back to **name + url + title**
signals, which is still valid, just less informed.

The uploaded workbook's **first sheet** is detected automatically
(`pd.ExcelFile(path).sheet_names[0]`); it does not need to be named
`Sheet1_with_url`.

## Job directory

```
backend/jobs/{job_id}/
├── .step2                      # marker tagging this as a Step 2 job
├── input.xlsx                  # uploaded promoted workbook from Step 1
├── sgai_url_checkpoint.json    # copied from the linked Step 1 job (optional)
├── batch_requests.jsonl        # written by batch submit
├── batch_id.txt                # written by batch submit (resume marker)
├── batch_idx_mapping.json      # written by batch submit
└── output/
    └── sector_classified.xlsx  # final output — served for download
```

## The four output sectors

The output workbook has one sheet per sector:

| Sheet     | Sector                            | Covers                                                                 |
|-----------|-----------------------------------|------------------------------------------------------------------------|
| `cyber`   | Cybersecurity Services            | MDR/SOC, SIEM, EDR, firewall/network security, IAM/PAM, MFA, zero trust, pen testing, GRC, ISO 27001 / SOC 2 / GDPR compliance. |
| `MSP`     | MSP IT Services                   | Managed IT support/helpdesk, RMM, infrastructure & endpoint management, backup & DR, cloud (M365/Azure/AWS), VDI, vCIO. |
| `telecom` | Telecom / Connectivity Services   | Fibre/broadband connectivity, SD-WAN, MPLS, VoIP, UCaaS, hosted PBX, video conferencing, structured cabling. |
| `other`   | Other                             | Anything that does not clearly fit the three sectors above.            |

Each sheet carries the original columns plus a `Sector` column. There is **no**
old-sector comparison — every row is classified fresh.

## Batch resume behaviour

`batch_id.txt` makes batch jobs crash-safe. The batch runs entirely on
Anthropic's servers, so if the backend **restarts after submission**:

- The job reloads from disk as **`batch_submitted`** with its `batch_id` intact.
- Use **Check Results** to poll: when the batch has `ended`, results are
  downloaded, `output/sector_classified.xlsx` is written, and the job flips to
  **`complete`**.
- If the batch is still processing, the job stays `batch_submitted` — just check
  again later.

## Output summary

When a Step 2 job reaches `complete`, `GET /api/step2/status/{job_id}` includes a
`summary` with per-sector counts (identical shape for both modes):

```jsonc
{
  "job_id": "…",
  "status": "complete",
  "message": "Complete",
  "mode": "test",            // or "batch"
  "batch_id": null,          // set for batch mode
  "summary": {
    "mode": "test",
    "rows_classified": 20,
    "cyber": 0,
    "msp": 0,
    "telecom": 0,
    "other": 0
  }
}
```

The frontend renders this as a sector breakdown card below the **Download Output**
button.

## Step 2 API reference — prefix `/api/step2/`

| Method | Endpoint                              | Purpose                                                                 |
|--------|---------------------------------------|-------------------------------------------------------------------------|
| GET    | `/api/step2/step1-jobs`               | List **completed** Step 1 URL jobs (`job_id`, `created_at`) for the dropdown |
| POST   | `/api/step2/run`                      | Multipart `file` + `step1_job_id` + `mode` (`test`/`batch`); creates the job, copies the checkpoint, runs in the background (`409` if any job is busy) |
| POST   | `/api/step2/check-results/{job_id}`   | Trigger a batch results check in the background (`409` if any job is busy) |
| GET    | `/api/step2/status/{job_id}`          | `{ job_id, status, message, mode, batch_id, summary }`                  |
| GET    | `/api/step2/download/{job_id}`        | Stream `sector_classified.xlsx` (`404` if not ready)                    |
| GET    | `/api/step2/jobs`                     | List all Step 2 jobs (for the existing-jobs / resume UI)                |

> **One job at a time across all steps.** Starting a Step 2 run or a results check
> while any Step 1 / Step 2 / Step 4 job is running returns HTTP `409`. A
> `batch_submitted` job is **not** "running" — the batch is on Anthropic's
> servers — so you can start other work while it processes.

---

# Step 3 — Email Finder

An independent step that finds **work emails** for contacts via the
**[Tomba](https://tomba.io) Email Finder API**. You upload an Excel file of
contacts and the backend looks up each person at their company domain, returning
a workbook split into rows where an email was found and rows where none was. It
is reached from the landing page (`frontend/index.html`) via the
**Step 3 — Email Finder** card, which opens `frontend/step3.html`.

The pipeline script lives at `backend/pipeline/tomba_email_finder.py`. Only
**work/company emails** are kept — Tomba's email-finder endpoint returns work
emails by design, and any personal-domain hits (gmail, yahoo, outlook, …) are
discarded.

## Required input columns

Every sheet in the uploaded workbook is processed automatically. Each sheet needs
three columns (sheets missing any of them are skipped):

| Column      | Purpose                                          |
|-------------|--------------------------------------------------|
| `Fname`     | Contact first name                               |
| `Sname`     | Contact surname                                  |
| `found_url` | Company website/domain (the Step 1 output column)|

Rows with a missing name or an unparseable domain are skipped (counted as
"no email").

## `TOMBA_API_KEY` / `TOMBA_SECRET`

Step 3 calls the Tomba API, which needs **two** credentials. Add them to
`backend/.env` alongside the existing keys:

```
ANTHROPIC_API_KEY=sk-ant-...
SGAI_API_KEY=your_scrapegraphai_api_key
TOMBA_API_KEY=ta_...
TOMBA_SECRET=ts_...
```

The backend loads `.env` and sets both into the environment before running the
pipeline. If **either** is missing, the job ends immediately in **error** with
the message `TOMBA_API_KEY or TOMBA_SECRET not set in .env`.

## Checkpoint & resume

Like the other steps, each upload becomes a **job** with a UUID directory under
`backend/jobs/{job_id}/`. The pipeline writes a per-row checkpoint
(`input_tomba_checkpoint.json`) as it goes, so an interrupted job is **safe to
resume** without re-spending Tomba lookups — resuming simply re-reads the
checkpoint and continues from the last completed row.

The checkpoint is **deleted on a fully successful run**. So a job directory with
a checkpoint but no output means the job was interrupted; the Step 3 page shows
it as a **resumable** card.

```
backend/jobs/{job_id}/
├── .step3                        # marker tagging this as a Step 3 job
├── input.xlsx                    # uploaded workbook (never modified)
├── input_tomba_checkpoint.json   # per-row checkpoint (deleted on success)
└── output/
    └── input_tomba.xlsx          # final output — served for download
```

## Rate limiting

Lookups are throttled with a sliding-window rate limiter, default
**60 requests/min** (the Tomba free tier). Raise it via `RATE_LIMIT_PER_MIN` in
`tomba_email_finder.py` if your plan allows more. Up to `MAX_WORKERS` (5) lookups
run concurrently under that limit.

## Output structure

The input file is **never modified**. The output workbook has **two sheets per
source sheet**:

- `{sheet}_email` — rows where Tomba found a work email.
- `{sheet}_no_email` — rows where Tomba found nothing.

Each row gains **four columns**:

| Column               | Meaning                                             |
|----------------------|-----------------------------------------------------|
| `Tomba_Email`        | The discovered work email.                          |
| `Tomba_Confidence`   | Tomba's confidence score for the email.             |
| `Tomba_Verification` | Verification status returned by Tomba.              |
| `Tomba_Position`     | The contact's job title/position, if returned.      |

## Output summary

When a Step 3 job reaches `complete`, `GET /api/step3/status/{job_id}` includes a
`summary`:

```jsonc
{
  "job_id": "…",
  "status": "complete",
  "message": "Complete",
  "summary": {
    "sheets_processed": 3,
    "total_rows": 350,
    "emails_found": 180,
    "no_email": 170
  }
}
```

The frontend renders this as a results card below the **Download Output** button.

## Step 3 API reference — prefix `/api/step3/`

| Method | Endpoint                       | Purpose                                                                 |
|--------|--------------------------------|-------------------------------------------------------------------------|
| POST   | `/api/step3/run`               | Upload `.xlsx`, start a job (`409` if any job is busy). Returns `error` immediately if Tomba keys are missing |
| POST   | `/api/step3/resume/{job_id}`   | Resume an interrupted job from its checkpoint (`404`/`409`)             |
| GET    | `/api/step3/status/{job_id}`   | `{ job_id, status, message, summary }` — `message` carries live per-row progress; `summary` is `null` until `complete` |
| GET    | `/api/step3/download/{job_id}` | Stream `input_tomba.xlsx` (`404` if not ready)                          |
| GET    | `/api/step3/jobs`              | List all Step 3 jobs (for the resume UI)                                |

> **One job at a time across all steps.** Starting or resuming a Step 3 job while
> any Step 1 / Step 2 / Step 4 job is running returns HTTP `409`.

---

# Required Column Names

Each step expects specific columns in the uploaded `.xlsx`. The Required Columns
info box on each step page mirrors this table.

| Step | Required columns | Optional columns | Matching |
|------|------------------|------------------|----------|
| **Step 1 — URL Discovery** | `Business Name` | — | case-insensitive; the name column is auto-detected from any column containing "name", "company", or "business" |
| **Step 2 — Sector Classification** | `found_url`, `Company Number` | `Business Name` (classification signal) | exact (case-sensitive) |
| **Step 3 — Email Finder** | `Fname`, `Sname`, `found_url` | — | exact (case-sensitive) |
| **Step 4 — Address Validation** | `website` | `add1`, `add2`, `town`, `county`, `postcode` (used for comparison if present) | case-insensitive |

> If a required column is missing or named differently, the job will fail —
> rename the column in your Excel file before uploading. The **matching** column
> above reflects what each pipeline actually does: Steps 1 and 4 match column
> names case-insensitively, while Steps 2 and 3 read exact, case-sensitive names.
