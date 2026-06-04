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
| GET    | `/api/step1/jobs`              | List all known jobs (for the resume UI)  |
