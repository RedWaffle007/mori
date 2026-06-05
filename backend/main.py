"""
FastAPI backend for Step 1 of the data-enrichment pipeline.

Flow:
    1. User uploads an .xlsx file → a job is created with a UUID and an
       on-disk directory under backend/jobs/{job_id}/.
    2. The address-validation pipeline runs in a background thread, writing
       all intermediate checkpoint files into the job directory.
    3. The frontend polls /api/step1/status/{job_id} and, once complete,
       downloads the *_validated.xlsx from /api/step1/download/{job_id}.

State is kept in an in-memory dict plus the on-disk job directories, which
survive server restarts and let interrupted jobs be resumed.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# ── paths ──────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent          # backend/
PIPELINE_DIR = BASE_DIR / "pipeline"
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# Pipeline modules import each other by bare name — make them importable.
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

# Load ANTHROPIC_API_KEY (and anything else) from backend/.env so it's available
# to the pipeline regardless of the working directory we switch into per job.
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("step1.api")

# Intermediate checkpoint files that signal a resumable job.
CHECKPOINT_FILES = [
    "scraped_content.jsonl",
    "extracted_addresses.jsonl",
    "scraped_sources.json",
    "uk_addresses.json",
    "skipped_addresses.json",
]

# ── Step 1 (URL discovery) artifacts ────────────────────────────────────────────
# The two-stage URL pipeline (sgai_url_finder → low_promoter) shares JOBS_DIR with
# the Step 4 pipeline. A hidden marker file tags a job dir as a Step 1 URL job so
# it can be reloaded and classified correctly after a server restart.
URL_STEP_MARKER = ".step1url"            # marker file written when a URL job is created
URL_CHECKPOINT = "sgai_url_checkpoint.json"  # Stage 1 per-row checkpoint (resumable)
URL_STAGE1_OUTPUT = "input_sgai_urls.xlsx"   # Stage 1 output (input.xlsx stem + _sgai_urls.xlsx)
URL_STAGE2_SUFFIX = "_promoted.xlsx"         # Stage 2 output suffix (served for download)

# Haiku 4.5 batch pricing (matches extract_addresses.py) used for cost tracking.
_INPUT_COST_PER_TOKEN = 0.50 / 1_000_000   # $0.50 per 1 M input tokens
_OUTPUT_COST_PER_TOKEN = 2.50 / 1_000_000  # $2.50 per 1 M output tokens


def _cost(input_tokens: int, output_tokens: int) -> float:
    return input_tokens * _INPUT_COST_PER_TOKEN + output_tokens * _OUTPUT_COST_PER_TOKEN


# ── job state ───────────────────────────────────────────────────────────────────


@dataclass
class JobState:
    job_id: str
    status: str  # "queued" | "running" | "complete" | "error" | "interrupted"
    message: str
    step: str = "step4"  # "step4" (address validation) | "step1url" (URL discovery)
    output_path: Path | None = None
    error: str | None = None
    summary: dict | None = None  # results breakdown + cost tracking (set on completion)
    created_at: datetime = field(default_factory=datetime.utcnow)


jobs: dict[str, JobState] = {}


def _is_running() -> bool:
    return any(j.status == "running" for j in jobs.values())


def _find_output(job_dir: Path) -> Path | None:
    """Return the validated workbook if it has been produced, else None."""
    out_dir = job_dir / "output"
    if out_dir.exists():
        found = next(out_dir.glob("*_validated.xlsx"), None)
        if found:
            return found
    # pipeline writes alongside the input first; treat that as complete too
    return next(job_dir.glob("*_validated.xlsx"), None)


def _has_checkpoints(job_dir: Path) -> bool:
    return any((job_dir / f).exists() for f in CHECKPOINT_FILES)


def _is_url_job(job_dir: Path) -> bool:
    """A job dir belongs to the Step 1 URL pipeline if it carries the marker file
    or any of the Stage 1 artifacts (checkpoint / Stage 1 output)."""
    return (
        (job_dir / URL_STEP_MARKER).exists()
        or (job_dir / URL_CHECKPOINT).exists()
        or (job_dir / URL_STAGE1_OUTPUT).exists()
    )


def _find_url_output(job_dir: Path) -> Path | None:
    """Return the promoted Stage 2 workbook if it has been produced, else None."""
    out_dir = job_dir / "output"
    if out_dir.exists():
        found = next(out_dir.glob(f"*{URL_STAGE2_SUFFIX}"), None)
        if found:
            return found
    return next(job_dir.glob(f"*{URL_STAGE2_SUFFIX}"), None)


# ── startup: reload jobs from disk ───────────────────────────────────────────────


def _reload_url_job(job_id: str, job_dir: Path, created: datetime) -> None:
    output = _find_url_output(job_dir)
    if output is not None:
        jobs[job_id] = JobState(
            job_id=job_id,
            status="complete",
            message="Complete",
            step="step1url",
            output_path=output,
            created_at=created,
        )
    elif (job_dir / "input.xlsx").exists():
        # Has an input (and possibly a Stage 1 checkpoint / output) but no finished
        # promoted workbook → the job was interrupted and can be resumed.
        jobs[job_id] = JobState(
            job_id=job_id,
            status="interrupted",
            message="Interrupted — resume available",
            step="step1url",
            created_at=created,
        )


def _reload_jobs() -> None:
    if not JOBS_DIR.exists():
        return
    for job_dir in sorted(JOBS_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        job_id = job_dir.name
        created = datetime.utcfromtimestamp(job_dir.stat().st_mtime)

        # Step 1 URL jobs share JOBS_DIR — classify them with their own artifacts.
        if _is_url_job(job_dir):
            _reload_url_job(job_id, job_dir, created)
            log.info("Reloaded job %s → %s (step1url)", job_id, jobs.get(job_id).status if job_id in jobs else "?")
            continue

        output = _find_output(job_dir)
        if output is not None:
            jobs[job_id] = JobState(
                job_id=job_id,
                status="complete",
                message="Complete",
                output_path=output,
                created_at=created,
            )
        elif _has_checkpoints(job_dir) or (job_dir / "input.xlsx").exists():
            # Checkpoint files (or at least an input) but no finished output →
            # the job was interrupted and can be resumed.
            jobs[job_id] = JobState(
                job_id=job_id,
                status="interrupted",
                message="Interrupted — resume available",
                created_at=created,
            )
        log.info("Reloaded job %s → %s", job_id, jobs.get(job_id).status if job_id in jobs else "?")


# ── pipeline execution (runs in a background thread) ─────────────────────────────


def _build_summary(result: dict) -> dict:
    """Turn the pipeline's return value into the per-job summary with costs.

    Phase 1 tokens are derived as (total − Phase 2). This is accurate because
    Phase 3 and Phase 4 make zero API calls, so any non-Phase-2 tokens in the
    pipeline total belong to Phase 1.
    """
    stats = result["stats"]
    total_tu = result["total_token_usage"]
    phase2_tu = result["phase2_token_usage"]

    phase1_in = total_tu["input_tokens"] - phase2_tu["input_tokens"]
    phase1_out = total_tu["output_tokens"] - phase2_tu["output_tokens"]

    phase1_cost = _cost(phase1_in, phase1_out)
    phase2_cost = _cost(phase2_tu["input_tokens"], phase2_tu["output_tokens"])

    processed = (
        stats.get("match", 0)
        + stats.get("updated", 0)
        + stats.get("original", 0)
        + stats.get("skipped_non_uk", 0)
        + stats.get("skipped_no_address", 0)
    )

    return {
        "results": {
            "processed": processed,
            "matched": stats.get("match", 0),
            "updated": stats.get("updated", 0),
            "original": stats.get("original", 0),
            "skipped_non_uk": stats.get("skipped_non_uk", 0),
            "skipped_no_address": stats.get("skipped_no_address", 0),
            "no_website": stats.get("no_website", 0),
        },
        "total_input_tokens": total_tu["input_tokens"],
        "total_output_tokens": total_tu["output_tokens"],
        "total_cost_usd": phase1_cost + phase2_cost,
    }


def _execute_pipeline(job_id: str) -> None:
    """Run the four-phase pipeline for a job inside its own working directory."""
    job = jobs[job_id]
    job_dir = JOBS_DIR / job_id
    input_file = job_dir / "input.xlsx"
    prev_cwd = os.getcwd()

    try:
        job.status = "running"
        job.message = "Scraping websites & extracting addresses…"
        job.error = None

        # All intermediate checkpoint files are relative paths — switch into the
        # job directory so they land inside jobs/{job_id}/.
        os.chdir(job_dir)

        import address_pipeline  # importable via PIPELINE_DIR on sys.path

        # Per-row progress: address_pipeline encodes the phase in the label as
        # "Phase N — Name|detail"; build a human-readable progress message from it.
        def progress_cb(current, total, label):
            phase, sep, detail = label.partition("|")
            if sep:
                job.message = f"{phase}: {current} / {total} ({detail})"
            else:
                job.message = f"{label}: {current} / {total}"

        # Resume is always on: existing checkpoint files are picked up automatically.
        result = address_pipeline.run(input_file, resume=True, progress_cb=progress_cb)

        # The pipeline writes {stem}_validated.xlsx next to the input. Move it
        # into output/ for the download endpoint.
        produced = next(job_dir.glob("*_validated.xlsx"), None)
        if produced is None:
            raise RuntimeError("pipeline finished but produced no validated workbook")

        out_dir = job_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / produced.name
        shutil.move(str(produced), str(dest))

        job.output_path = dest
        job.summary = _build_summary(result)
        job.status = "complete"
        job.message = "Complete"
        log.info("Job %s complete → %s", job_id, dest)

    except Exception as exc:  # noqa: BLE001 — surface any pipeline failure to the user
        log.exception("Job %s failed", job_id)
        job.status = "error"
        job.error = str(exc)
        job.message = f"Error: {exc}"
    finally:
        os.chdir(prev_cwd)


def _launch(job_id: str) -> None:
    """Schedule the pipeline on a worker thread without blocking the event loop."""
    asyncio.create_task(asyncio.to_thread(_execute_pipeline, job_id))


# ── Step 1 (URL discovery) two-stage pipeline ────────────────────────────────────


def _count_confidence(*xlsx_paths: Path) -> dict:
    """Read one or more URL workbooks and tally the url_confidence column across all
    their sheets. Returns {"HIGH": n, "MEDIUM": n, "LOW": n, "DISCARD": n, "total": n}."""
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "DISCARD": 0, "total": 0}
    for path in xlsx_paths:
        if not path.exists():
            continue
        xl = pd.ExcelFile(path)
        for sheet in xl.sheet_names:
            df = pd.read_excel(xl, sheet_name=sheet, dtype=str).fillna("")
            if "url_confidence" not in df.columns:
                continue
            for value in df["url_confidence"]:
                key = (value or "").strip().upper()
                if key in counts:
                    counts[key] += 1
                    counts["total"] += 1
    return counts


def _stage1_summary(stage1_output: Path) -> dict:
    c = _count_confidence(stage1_output)
    return {
        "high": c["HIGH"],
        "medium": c["MEDIUM"],
        "low": c["LOW"],
        "discard": c["DISCARD"],
        "total": c["total"],
    }


def _stage2_summary(stage2_output: Path) -> dict:
    """Tally the promoted workbook. Promoted rows carry promoted == 'yes'."""
    promoted_high = promoted_medium = remaining_low = discard = final_with_url = 0
    xl = pd.ExcelFile(stage2_output)
    for sheet in xl.sheet_names:
        df = pd.read_excel(xl, sheet_name=sheet, dtype=str).fillna("")
        if "url_confidence" not in df.columns:
            continue
        promoted_col = df["promoted"] if "promoted" in df.columns else None
        for i, value in enumerate(df["url_confidence"]):
            conf = (value or "").strip().upper()
            is_promoted = (
                promoted_col is not None
                and str(promoted_col.iloc[i]).strip().lower() == "yes"
            )
            if conf in ("HIGH", "MEDIUM"):
                final_with_url += 1
                if is_promoted and conf == "HIGH":
                    promoted_high += 1
                elif is_promoted and conf == "MEDIUM":
                    promoted_medium += 1
            elif conf == "LOW":
                remaining_low += 1
            elif conf == "DISCARD":
                discard += 1
    return {
        "promoted_high": promoted_high,
        "promoted_medium": promoted_medium,
        "remaining_low": remaining_low,
        "discard": discard,
        "final_with_url": final_with_url,
    }


def _stage1_progress(job: JobState, job_dir: Path, names_by_idx: dict, total: int, stop_event: threading.Event) -> None:
    """Watch the Stage 1 checkpoint file and surface per-row progress on job.message.

    sgai_url_finder.run() exposes no progress callback (its signature is fixed), so
    we poll its append-only checkpoint instead: the line count is the rows processed
    and the last line carries the most recently resolved row's confidence + idx.
    """
    ckpt = job_dir / URL_CHECKPOINT
    while not stop_event.is_set():
        try:
            if ckpt.exists():
                lines = [ln for ln in ckpt.read_text(encoding="utf-8").splitlines() if ln.strip()]
                done = len(lines)
                name = ""
                conf = ""
                if lines:
                    last = json.loads(lines[-1])
                    name = names_by_idx.get(str(last.get("idx", "")), "")
                    conf = last.get("confidence", "")
                job.message = f"Stage 1 — Searching URLs: {done} / {total} ({name}) — {conf}"
        except Exception:  # noqa: BLE001 — progress is best-effort, never fatal
            pass
        stop_event.wait(2)


def _execute_url_pipeline(job_id: str) -> None:
    """Run the two-stage URL discovery pipeline for a job in its own working dir.

    Stage 1: sgai_url_finder.run(input.xlsx) → input_sgai_urls.xlsx (resumable).
    Stage 2: low_promoter.run(input_sgai_urls.xlsx) → input_sgai_urls_promoted.xlsx.
    The promoted workbook is moved into output/ for download.
    """
    job = jobs[job_id]
    job_dir = JOBS_DIR / job_id
    input_file = job_dir / "input.xlsx"
    stage1_output = job_dir / URL_STAGE1_OUTPUT
    prev_cwd = os.getcwd()

    try:
        job.status = "running"
        job.error = None

        # All checkpoint/output files are relative paths — switch into the job dir.
        os.chdir(job_dir)

        import sgai_url_finder  # importable via PIPELINE_DIR on sys.path
        import low_promoter

        # The pipeline reads SGAI_API_KEY at import time; make sure the module
        # picks up whatever is in the environment (loaded from backend/.env).
        sgai_url_finder.SGAI_API_KEY = os.environ.get("SGAI_API_KEY", "")

        # ── Stage 1 ───────────────────────────────────────────────────────────
        if stage1_output.exists():
            # Resume case: Stage 1 already finished — skip straight to Stage 2.
            job.message = "Stage 1 output found — running Stage 2…"
        else:
            # Stage 1 needs the ScrapeGraphAI key — fail clearly rather than letting
            # the pipeline call sys.exit() deep in a worker thread.
            if not os.environ.get("SGAI_API_KEY"):
                raise RuntimeError("SGAI_API_KEY is not set. Add it to backend/.env.")

            # Detect the uploaded file's sheet name (may be anything, not "Sheet1").
            detected_sheet = pd.ExcelFile(input_file).sheet_names[0]
            sgai_url_finder.INPUT_SHEET = detected_sheet

            # Map row index → company name so the progress monitor can name rows.
            names_by_idx: dict[str, str] = {}
            try:
                df_in = pd.read_excel(input_file, sheet_name=detected_sheet, dtype=str).fillna("")
                col = next(
                    (c for c in df_in.columns
                     if "name" in c.lower() or "company" in c.lower() or "business" in c.lower()),
                    None,
                )
                if col is not None:
                    names_by_idx = {str(i): str(v) for i, v in df_in[col].items()}
                total_rows = len(df_in)
            except Exception:  # noqa: BLE001
                total_rows = 0

            job.message = f"Stage 1 — Searching URLs: 0 / {total_rows}"

            stop_event = threading.Event()
            monitor = threading.Thread(
                target=_stage1_progress,
                args=(job, job_dir, names_by_idx, total_rows, stop_event),
                daemon=True,
            )
            monitor.start()
            try:
                sgai_url_finder.run(str(input_file))
            finally:
                stop_event.set()
                monitor.join(timeout=5)

            if not stage1_output.exists():
                raise RuntimeError("Stage 1 finished but produced no _sgai_urls.xlsx output")

        stage1_summary = _stage1_summary(stage1_output)

        # ── Stage 2 ───────────────────────────────────────────────────────────
        job.message = "Stage 1 complete. Running Stage 2 — re-evaluating LOW confidence rows…"
        low_promoter.run(str(stage1_output))

        produced = next(job_dir.glob(f"*{URL_STAGE2_SUFFIX}"), None)
        if produced is None:
            raise RuntimeError("Stage 2 finished but produced no _promoted.xlsx output")

        out_dir = job_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / produced.name
        shutil.move(str(produced), str(dest))

        stage2_summary = _stage2_summary(dest)

        job.output_path = dest
        job.summary = {"stage1": stage1_summary, "stage2": stage2_summary}
        job.status = "complete"
        job.message = "Complete"
        log.info("URL job %s complete → %s", job_id, dest)

    except (Exception, SystemExit) as exc:  # noqa: BLE001 — surface any failure to the user
        # The pipeline scripts call sys.exit() on fatal input errors (missing key,
        # no name column); SystemExit isn't an Exception, so catch it explicitly to
        # avoid leaving the job wedged in "running" and blocking every later job.
        log.exception("URL job %s failed", job_id)
        job.status = "error"
        job.error = str(exc)
        job.message = f"Error: {exc}"
    finally:
        os.chdir(prev_cwd)


def _launch_url(job_id: str) -> None:
    """Schedule the two-stage URL pipeline on a worker thread."""
    asyncio.create_task(asyncio.to_thread(_execute_url_pipeline, job_id))


# ── app ──────────────────────────────────────────────────────────────────────


app = FastAPI(title="Step 1 — Address Validation Pipeline")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _on_startup() -> None:
    _reload_jobs()
    log.info("Loaded %d existing job(s) from %s", len(jobs), JOBS_DIR)


# ── endpoints ─────────────────────────────────────────────────────────────────


@app.post("/api/step1/run")
async def run_step1(file: UploadFile = File(...)):
    if _is_running():
        raise HTTPException(status_code=409, detail="Another job is currently running.")

    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx file.")

    job_id = str(uuid.uuid4())
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    input_path = job_dir / "input.xlsx"
    with input_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    jobs[job_id] = JobState(job_id=job_id, status="queued", message="Queued")
    _launch(job_id)
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/step1/resume/{job_id}")
async def resume_step1(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if _is_running():
        raise HTTPException(status_code=409, detail="Another job is currently running.")

    job_dir = JOBS_DIR / job_id
    if not (job_dir / "input.xlsx").exists():
        raise HTTPException(status_code=404, detail="Job input file missing on disk.")

    job.status = "queued"
    job.message = "Queued (resume)"
    _launch(job_id)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/step1/status/{job_id}")
async def status_step1(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "message": job.message,
        "summary": job.summary,
    }


@app.get("/api/step1/download/{job_id}")
async def download_step1(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "complete" or job.output_path is None or not job.output_path.exists():
        raise HTTPException(status_code=404, detail="Output not ready.")
    return FileResponse(
        path=str(job.output_path),
        filename=job.output_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/step1/jobs")
async def list_jobs():
    return [
        {
            "job_id": j.job_id,
            "status": j.status,
            "message": j.message,
            "created_at": j.created_at.isoformat(),
        }
        for j in sorted(jobs.values(), key=lambda x: x.created_at, reverse=True)
        if j.step == "step4"
    ]


# ── Step 1 (URL discovery) endpoints — prefix /api/step1url/ ─────────────────────


@app.post("/api/step1url/run")
async def run_step1url(file: UploadFile = File(...)):
    if _is_running():
        raise HTTPException(status_code=409, detail="Another job is currently running.")

    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx file.")

    job_id = str(uuid.uuid4())
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Tag this job dir as a Step 1 URL job so it reloads correctly after a restart.
    (job_dir / URL_STEP_MARKER).write_text("step1url", encoding="utf-8")

    input_path = job_dir / "input.xlsx"
    with input_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    jobs[job_id] = JobState(job_id=job_id, status="queued", message="Queued", step="step1url")
    _launch_url(job_id)
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/step1url/resume/{job_id}")
async def resume_step1url(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if _is_running():
        raise HTTPException(status_code=409, detail="Another job is currently running.")

    job_dir = JOBS_DIR / job_id
    if not (job_dir / "input.xlsx").exists():
        raise HTTPException(status_code=404, detail="Job input file missing on disk.")

    job.status = "queued"
    job.message = "Queued (resume)"
    _launch_url(job_id)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/step1url/status/{job_id}")
async def status_step1url(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "message": job.message,
        "summary": job.summary,
    }


@app.get("/api/step1url/download/{job_id}")
async def download_step1url(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "complete" or job.output_path is None or not job.output_path.exists():
        raise HTTPException(status_code=404, detail="Output not ready.")
    return FileResponse(
        path=str(job.output_path),
        filename=job.output_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/step1url/jobs")
async def list_url_jobs():
    return [
        {
            "job_id": j.job_id,
            "status": j.status,
            "message": j.message,
            "created_at": j.created_at.isoformat(),
        }
        for j in sorted(jobs.values(), key=lambda x: x.created_at, reverse=True)
        if j.step == "step1url"
    ]
