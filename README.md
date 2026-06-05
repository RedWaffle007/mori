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
│   ├── main.py              # FastAPI app
│   ├── requirements.txt
│   ├── .env                 # ANTHROPIC_API_KEY
│   ├── jobs/                # per-job working directories (created at runtime)
│   └── pipeline/            # the four-phase pipeline
└── frontend/
    ├── index.html
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

Start the backend from the `mori/` directory:

```bash
uvicorn backend.main:app --reload --app-dir mori
```

> If you are **already inside** the `mori/` directory, drop `--app-dir`:
>
> ```bash
> uvicorn backend.main:app --reload
> ```

The API is served at `http://localhost:8000`.

Then open the frontend — it talks to `http://localhost:8000` (CORS is open):

```bash
# simplest: just open the file
open frontend/index.html            # macOS
xdg-open frontend/index.html        # Linux

# or serve it on its own port
python -m http.server 5500 --directory frontend
# then visit http://localhost:5500
```

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
