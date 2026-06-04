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
import logging
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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


# ── startup: reload jobs from disk ───────────────────────────────────────────────


def _reload_jobs() -> None:
    if not JOBS_DIR.exists():
        return
    for job_dir in sorted(JOBS_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        job_id = job_dir.name
        created = datetime.utcfromtimestamp(job_dir.stat().st_mtime)
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
    ]
