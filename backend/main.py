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
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

# ── Step 2 (sector classification) artifacts ─────────────────────────────────────
# Step 2 shares JOBS_DIR with the other steps. A hidden marker tags a job dir as a
# Step 2 job so it reloads correctly after a restart. Batch jobs leave batch_id.txt
# behind so an interrupted server can resume by re-checking results.
STEP2_MARKER = ".step2"                       # marker file written when a Step 2 job is created
STEP2_CHECKPOINT = "sgai_url_checkpoint.json" # copied from the linked Step 1 job (optional)
STEP2_BATCH_ID = "batch_id.txt"               # written by batch submit (resume marker)
STEP2_OUTPUT = "sector_classified.xlsx"       # final output (served for download)

# ── Step 3 (email finder) artifacts ──────────────────────────────────────────────
# Step 3 shares JOBS_DIR with the other steps. A hidden marker tags a job dir as a
# Step 3 job so it reloads correctly after a restart. The Tomba checkpoint is
# deleted on a fully successful run, so its presence (with no output) means the
# job was interrupted and can be resumed.
STEP3_MARKER = ".step3"                       # marker file written when a Step 3 job is created
STEP3_CHECKPOINT = "input_tomba_checkpoint.json"  # per-row checkpoint (resumable)
STEP3_OUTPUT = "input_tomba.xlsx"             # final output (served for download)

# Haiku 4.5 batch pricing (matches extract_addresses.py) used for cost tracking.
_INPUT_COST_PER_TOKEN = 0.50 / 1_000_000   # $0.50 per 1 M input tokens
_OUTPUT_COST_PER_TOKEN = 2.50 / 1_000_000  # $2.50 per 1 M output tokens


def _cost(input_tokens: int, output_tokens: int) -> float:
    return input_tokens * _INPUT_COST_PER_TOKEN + output_tokens * _OUTPUT_COST_PER_TOKEN


# ── job state ───────────────────────────────────────────────────────────────────


@dataclass
class JobState:
    job_id: str
    status: str  # "queued" | "running" | "batch_submitted" | "complete" | "error" | "interrupted"
    message: str
    step: str = "step4"  # "step4" (address validation) | "step1url" (URL discovery) | "step2" (sector classification)
    output_path: Path | None = None
    error: str | None = None
    summary: dict | None = None  # results breakdown + cost tracking (set on completion)
    mode: str | None = None  # Step 2 only: "test" | "batch"
    batch_id: str | None = None  # Step 2 batch mode: Anthropic batch id (set after submission)
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


def _is_step2_job(job_dir: Path) -> bool:
    """A job dir belongs to the Step 2 pipeline if it carries the Step 2 marker."""
    return (job_dir / STEP2_MARKER).exists()


def _find_step2_output(job_dir: Path) -> Path | None:
    """Return the classified workbook if it has been produced, else None."""
    out = job_dir / "output" / STEP2_OUTPUT
    return out if out.exists() else None


def _step2_summary(output_path: Path, mode: str) -> dict:
    """Tally the six sector sheets of a completed classification workbook."""
    xl = pd.ExcelFile(output_path)

    def count(sheet: str) -> int:
        if sheet not in xl.sheet_names:
            return 0
        return len(pd.read_excel(xl, sheet_name=sheet, dtype=str))

    security = count("security")
    msp = count("MSP V")
    integration = count("integration")
    support = count("support")
    infrastructure = count("infrastructure")
    other = count("other")
    return {
        "mode": mode,
        "rows_classified": security + msp + integration + support + infrastructure + other,
        "security": security,
        "msp": msp,
        "integration": integration,
        "support": support,
        "infrastructure": infrastructure,
        "other": other,
    }


def _is_step3_job(job_dir: Path) -> bool:
    """A job dir belongs to the Step 3 pipeline if it carries the Step 3 marker."""
    return (job_dir / STEP3_MARKER).exists()


def _find_step3_output(job_dir: Path) -> Path | None:
    """Return the Tomba workbook if it has been produced, else None."""
    out = job_dir / "output" / STEP3_OUTPUT
    return out if out.exists() else None


def _step3_summary(output_path: Path) -> dict:
    """Tally the {sheet}_email / {sheet}_no_email pairs of a completed workbook."""
    xl = pd.ExcelFile(output_path)
    sheets_processed = total_rows = emails_found = no_email = 0
    for sheet in xl.sheet_names:
        n = len(pd.read_excel(xl, sheet_name=sheet, dtype=str))
        total_rows += n
        if sheet.endswith("_no_email"):
            no_email += n
        elif sheet.endswith("_email"):
            emails_found += n
            sheets_processed += 1  # one source sheet per *_email sheet
    return {
        "sheets_processed": sheets_processed,
        "total_rows": total_rows,
        "emails_found": emails_found,
        "no_email": no_email,
    }


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


def _reload_step2_job(job_id: str, job_dir: Path, created: datetime) -> None:
    output = _find_step2_output(job_dir)
    batch_id_file = job_dir / STEP2_BATCH_ID
    if output is not None:
        # A batch_id.txt means it ran as a batch; otherwise it was a test run.
        mode = "batch" if batch_id_file.exists() else "test"
        try:
            summary = _step2_summary(output, mode)
        except Exception:  # noqa: BLE001 — summary is best-effort on reload
            summary = None
        jobs[job_id] = JobState(
            job_id=job_id,
            status="complete",
            message="Complete",
            step="step2",
            mode=mode,
            output_path=output,
            summary=summary,
            created_at=created,
        )
    elif batch_id_file.exists():
        # Batch was submitted but no output yet → resume by re-checking results.
        batch_id = batch_id_file.read_text(encoding="utf-8").strip()
        jobs[job_id] = JobState(
            job_id=job_id,
            status="batch_submitted",
            message=f"Batch submitted — ID: {batch_id}",
            step="step2",
            mode="batch",
            batch_id=batch_id,
            created_at=created,
        )
    elif (job_dir / "input.xlsx").exists():
        jobs[job_id] = JobState(
            job_id=job_id,
            status="interrupted",
            message="Interrupted — resume available",
            step="step2",
            created_at=created,
        )


def _reload_step3_job(job_id: str, job_dir: Path, created: datetime) -> None:
    output = _find_step3_output(job_dir)
    if output is not None:
        try:
            summary = _step3_summary(output)
        except Exception:  # noqa: BLE001 — summary is best-effort on reload
            summary = None
        jobs[job_id] = JobState(
            job_id=job_id,
            status="complete",
            message="Complete",
            step="step3",
            output_path=output,
            summary=summary,
            created_at=created,
        )
    elif (job_dir / "input.xlsx").exists():
        # Has an input (and possibly a checkpoint) but no finished output → the job
        # was interrupted and can be resumed from its Tomba checkpoint.
        jobs[job_id] = JobState(
            job_id=job_id,
            status="interrupted",
            message="Interrupted — resume available",
            step="step3",
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

        # Step 3 jobs share JOBS_DIR — classify them with their own marker first.
        if _is_step3_job(job_dir):
            _reload_step3_job(job_id, job_dir, created)
            log.info("Reloaded job %s → %s (step3)", job_id, jobs.get(job_id).status if job_id in jobs else "?")
            continue

        # Step 2 jobs share JOBS_DIR — classify them with their own marker first.
        if _is_step2_job(job_dir):
            _reload_step2_job(job_id, job_dir, created)
            log.info("Reloaded job %s → %s (step2)", job_id, jobs.get(job_id).status if job_id in jobs else "?")
            continue

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


# ── Step 2 (sector classification) pipeline ──────────────────────────────────────


def _configure_sector_classifier(job_dir: Path):
    """Import sector_classifier and point its module-level paths at this job dir.

    The classifier reads INPUT_XLSX / INPUT_SHEET / CHECKPOINT_JSONL / OUTPUT_XLSX
    as module globals; we override them per job. The input sheet name is detected
    from the uploaded workbook (it need not be named "Sheet1_with_url").
    """
    import sector_classifier  # importable via PIPELINE_DIR on sys.path

    input_xlsx = job_dir / "input.xlsx"
    detected_sheet = pd.ExcelFile(input_xlsx).sheet_names[0]

    sector_classifier.INPUT_XLSX = str(input_xlsx)
    sector_classifier.INPUT_SHEET = detected_sheet
    sector_classifier.CHECKPOINT_JSONL = str(job_dir / STEP2_CHECKPOINT)
    sector_classifier.OUTPUT_XLSX = str(job_dir / "output" / STEP2_OUTPUT)
    # The module reads the key at import time from pipeline/.env; make sure it picks
    # up whatever the backend loaded from backend/.env.
    sector_classifier.ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    return sector_classifier


def _execute_step2_test(job_id: str) -> None:
    """Run a live test classification (first 20 rows) for a Step 2 job."""
    job = jobs[job_id]
    job_dir = JOBS_DIR / job_id
    prev_cwd = os.getcwd()
    try:
        job.status = "running"
        job.message = "Classifying 20 rows (live API)…"
        job.error = None

        # Batch/output files are relative paths — run inside the job dir.
        os.chdir(job_dir)
        sc = _configure_sector_classifier(job_dir)
        sc.cmd_test()

        output = _find_step2_output(job_dir)
        if output is None:
            raise RuntimeError("test run finished but produced no classified workbook")

        job.output_path = output
        job.summary = _step2_summary(output, "test")
        job.status = "complete"
        job.message = "Complete"
        log.info("Step 2 test job %s complete → %s", job_id, output)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — surface any failure to the user
        log.exception("Step 2 test job %s failed", job_id)
        job.status = "error"
        job.error = str(exc)
        job.message = f"Error: {exc}"
    finally:
        os.chdir(prev_cwd)


def _execute_step2_submit(job_id: str) -> None:
    """Submit the full classification job to the Anthropic Batch API (50% off)."""
    job = jobs[job_id]
    job_dir = JOBS_DIR / job_id
    prev_cwd = os.getcwd()
    try:
        job.status = "running"
        job.message = "Submitting batch…"
        job.error = None

        os.chdir(job_dir)
        sc = _configure_sector_classifier(job_dir)
        sc.cmd_batch_submit()

        batch_id_file = job_dir / STEP2_BATCH_ID
        if not batch_id_file.exists():
            raise RuntimeError("batch submit finished but wrote no batch_id.txt")
        batch_id = batch_id_file.read_text(encoding="utf-8").strip()

        job.batch_id = batch_id
        job.status = "batch_submitted"
        job.message = f"Batch submitted — ID: {batch_id}"
        log.info("Step 2 batch job %s submitted → %s", job_id, batch_id)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — surface any failure to the user
        log.exception("Step 2 batch submit %s failed", job_id)
        job.status = "error"
        job.error = str(exc)
        job.message = f"Error: {exc}"
    finally:
        os.chdir(prev_cwd)


def _execute_step2_check(job_id: str) -> None:
    """Poll the batch and, if it has ended, download results + write the workbook."""
    job = jobs[job_id]
    job_dir = JOBS_DIR / job_id
    prev_cwd = os.getcwd()
    try:
        job.status = "running"
        job.message = "Checking batch results…"
        job.error = None

        os.chdir(job_dir)
        sc = _configure_sector_classifier(job_dir)
        sc.cmd_batch_results()  # writes the workbook only once the batch has ended

        output = _find_step2_output(job_dir)
        if output is not None:
            job.output_path = output
            job.summary = _step2_summary(output, "batch")
            job.status = "complete"
            job.message = "Complete"
            log.info("Step 2 batch job %s complete → %s", job_id, output)
        else:
            # Still processing on Anthropic's side — back to the waiting state.
            job.status = "batch_submitted"
            job.message = (
                f"Batch still processing — ID: {job.batch_id}. Check again shortly."
                if job.batch_id else "Batch still processing — check again shortly."
            )
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — surface any failure to the user
        log.exception("Step 2 batch check %s failed", job_id)
        job.status = "error"
        job.error = str(exc)
        job.message = f"Error: {exc}"
    finally:
        os.chdir(prev_cwd)


def _launch_step2(job_id: str, mode: str) -> None:
    """Schedule a Step 2 run (test or batch submit) on a worker thread."""
    fn = _execute_step2_test if mode == "test" else _execute_step2_submit
    asyncio.create_task(asyncio.to_thread(fn, job_id))


def _launch_step2_check(job_id: str) -> None:
    """Schedule a Step 2 batch results check on a worker thread."""
    asyncio.create_task(asyncio.to_thread(_execute_step2_check, job_id))


# ── Step 3 (email finder) pipeline ───────────────────────────────────────────────


def _execute_step3_pipeline(job_id: str) -> None:
    """Run the Tomba email-finder pipeline for a job in its own working dir.

    The pipeline checkpoints every processed row to input_tomba_checkpoint.json and
    deletes it on a fully successful run, so an interrupted job resumes by simply
    pointing main() at the same job directory again.
    """
    job = jobs[job_id]
    job_dir = JOBS_DIR / job_id
    input_file = job_dir / "input.xlsx"
    prev_cwd = os.getcwd()

    try:
        job.status = "running"
        job.message = "Finding emails…"
        job.error = None

        # The pipeline writes its checkpoint/output relative to the cwd; switch into
        # the job dir so everything lands inside jobs/{job_id}/.
        os.chdir(job_dir)
        (job_dir / "output").mkdir(parents=True, exist_ok=True)

        import tomba_email_finder  # importable via PIPELINE_DIR on sys.path

        # Point the module at this job's paths and make sure it sees the Tomba keys
        # the backend loaded from backend/.env (set on os.environ before launch).
        tomba_email_finder.INPUT_PATH = input_file
        tomba_email_finder.OUTPUT_PATH = job_dir / "output" / STEP3_OUTPUT
        tomba_email_finder.CHECKPOINT_PATH = job_dir / STEP3_CHECKPOINT
        tomba_email_finder.TOMBA_API_KEY = os.environ.get("TOMBA_API_KEY")
        tomba_email_finder.TOMBA_SECRET = os.environ.get("TOMBA_SECRET")

        def progress_cb(current, total, label):
            job.message = f"Processing: {current} / {total} ({label})"

        summary = tomba_email_finder.main(progress_cb=progress_cb)

        output = _find_step3_output(job_dir)
        if output is None:
            raise RuntimeError("pipeline finished but produced no output workbook")

        job.output_path = output
        job.summary = summary
        job.status = "complete"
        job.message = "Complete"
        log.info("Step 3 job %s complete → %s", job_id, output)

    except (Exception, SystemExit) as exc:  # noqa: BLE001 — surface any failure to the user
        # main() calls sys.exit() on fatal input errors; SystemExit isn't an
        # Exception, so catch it explicitly to avoid leaving the job wedged.
        log.exception("Step 3 job %s failed", job_id)
        job.status = "error"
        job.error = str(exc)
        job.message = f"Error: {exc}"
    finally:
        os.chdir(prev_cwd)


def _launch_step3(job_id: str) -> None:
    """Schedule the email-finder pipeline on a worker thread."""
    asyncio.create_task(asyncio.to_thread(_execute_step3_pipeline, job_id))


# ── app ──────────────────────────────────────────────────────────────────────


app = FastAPI(title="Step 1 — Address Validation Pipeline")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _add_ngrok_skip_header(request, call_next):
    """Stop ngrok's free-tier interstitial from intercepting page loads / API calls."""
    response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response


# Serve the frontend. The directory is mori/frontend/, resolved from this file's
# location so it works regardless of the directory uvicorn is launched from.
FRONTEND_DIR = BASE_DIR.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def serve_index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


# The pages link to each other with relative paths (e.g. href="step1.html"), which
# resolve to /step1.html etc. — serve each HTML file explicitly so those links work.
@app.get("/index.html")
async def serve_index_html():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/step1.html")
async def serve_step1_html():
    return FileResponse(str(FRONTEND_DIR / "step1.html"))


@app.get("/step2.html")
async def serve_step2_html():
    return FileResponse(str(FRONTEND_DIR / "step2.html"))


@app.get("/step3.html")
async def serve_step3_html():
    return FileResponse(str(FRONTEND_DIR / "step3.html"))


@app.get("/step4.html")
async def serve_step4_html():
    return FileResponse(str(FRONTEND_DIR / "step4.html"))


# The HTML pages also reference these assets with relative paths (href="style.css",
# src="app.js"), so serve them at the root too — otherwise pages load unstyled and
# step4.html's logic never loads.
@app.get("/style.css")
async def serve_style_css():
    return FileResponse(str(FRONTEND_DIR / "style.css"))


@app.get("/app.js")
async def serve_app_js():
    return FileResponse(str(FRONTEND_DIR / "app.js"))


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


# ── Step 2 (sector classification) endpoints — prefix /api/step2/ ────────────────


@app.get("/api/step2/step1-jobs")
async def list_step1_jobs_for_step2():
    """Completed Step 1 (URL discovery) jobs, for the Step 2 linking dropdown."""
    return [
        {
            "job_id": j.job_id,
            "created_at": j.created_at.isoformat(),
        }
        for j in sorted(jobs.values(), key=lambda x: x.created_at, reverse=True)
        if j.step == "step1url" and j.status == "complete"
    ]


@app.post("/api/step2/run")
async def run_step2(
    file: UploadFile = File(...),
    step1_job_id: str = Form(...),
    mode: str = Form(...),
):
    if _is_running():
        raise HTTPException(status_code=409, detail="Another job is currently running.")

    if mode not in ("test", "batch"):
        raise HTTPException(status_code=400, detail="mode must be 'test' or 'batch'.")
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx file.")

    job_id = str(uuid.uuid4())
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Tag this job dir as a Step 2 job so it reloads correctly after a restart.
    (job_dir / STEP2_MARKER).write_text("step2", encoding="utf-8")

    input_path = job_dir / "input.xlsx"
    with input_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    # Link the selected Step 1 job by copying its checkpoint. If it's missing the
    # classifier still works — it falls back to name + url + title signals.
    src_checkpoint = JOBS_DIR / step1_job_id / STEP2_CHECKPOINT
    if src_checkpoint.exists():
        shutil.copyfile(src_checkpoint, job_dir / STEP2_CHECKPOINT)
        log.info("Step 2 job %s linked checkpoint from Step 1 job %s", job_id, step1_job_id)
    else:
        log.info("Step 2 job %s: no checkpoint in Step 1 job %s — name+url+title only",
                 job_id, step1_job_id)

    jobs[job_id] = JobState(
        job_id=job_id, status="queued", message="Queued", step="step2", mode=mode
    )
    _launch_step2(job_id, mode)
    return {"job_id": job_id, "status": "queued", "mode": mode}


@app.post("/api/step2/check-results/{job_id}")
async def check_results_step2(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if _is_running():
        raise HTTPException(status_code=409, detail="Another job is currently running.")

    job_dir = JOBS_DIR / job_id
    if not (job_dir / STEP2_BATCH_ID).exists():
        raise HTTPException(status_code=400, detail="No batch was submitted for this job.")

    job.status = "queued"
    job.message = "Checking batch results…"
    _launch_step2_check(job_id)
    return {"job_id": job_id, "status": "queued", "mode": job.mode}


@app.get("/api/step2/status/{job_id}")
async def status_step2(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "message": job.message,
        "mode": job.mode,
        "batch_id": job.batch_id,
        "summary": job.summary,
    }


@app.get("/api/step2/download/{job_id}")
async def download_step2(job_id: str):
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


@app.get("/api/step2/jobs")
async def list_step2_jobs():
    return [
        {
            "job_id": j.job_id,
            "status": j.status,
            "message": j.message,
            "mode": j.mode,
            "batch_id": j.batch_id,
            "summary": j.summary,
            "created_at": j.created_at.isoformat(),
        }
        for j in sorted(jobs.values(), key=lambda x: x.created_at, reverse=True)
        if j.step == "step2"
    ]


# ── Step 3 (email finder) endpoints — prefix /api/step3/ ─────────────────────────


def _tomba_keys_present() -> bool:
    """Both Tomba credentials must be available in the environment (from .env)."""
    return bool(os.environ.get("TOMBA_API_KEY") and os.environ.get("TOMBA_SECRET"))


def _mark_missing_keys(job: JobState) -> None:
    job.status = "error"
    job.error = "TOMBA_API_KEY or TOMBA_SECRET not set in .env"
    job.message = "TOMBA_API_KEY or TOMBA_SECRET not set in .env"


@app.post("/api/step3/run")
async def run_step3(file: UploadFile = File(...)):
    if _is_running():
        raise HTTPException(status_code=409, detail="Another job is currently running.")

    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx file.")

    job_id = str(uuid.uuid4())
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Tag this job dir as a Step 3 job so it reloads correctly after a restart.
    (job_dir / STEP3_MARKER).write_text("step3", encoding="utf-8")

    input_path = job_dir / "input.xlsx"
    with input_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    jobs[job_id] = JobState(job_id=job_id, status="queued", message="Queued", step="step3")

    if not _tomba_keys_present():
        _mark_missing_keys(jobs[job_id])
        return {"job_id": job_id, "status": "error"}

    _launch_step3(job_id)
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/step3/resume/{job_id}")
async def resume_step3(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if _is_running():
        raise HTTPException(status_code=409, detail="Another job is currently running.")

    job_dir = JOBS_DIR / job_id
    if not (job_dir / "input.xlsx").exists():
        raise HTTPException(status_code=404, detail="Job input file missing on disk.")

    if not _tomba_keys_present():
        _mark_missing_keys(job)
        return {"job_id": job_id, "status": "error"}

    job.status = "queued"
    job.message = "Queued (resume)"
    job.error = None
    _launch_step3(job_id)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/step3/status/{job_id}")
async def status_step3(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "message": job.message,
        "summary": job.summary,
    }


@app.get("/api/step3/download/{job_id}")
async def download_step3(job_id: str):
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


@app.get("/api/step3/jobs")
async def list_step3_jobs():
    return [
        {
            "job_id": j.job_id,
            "status": j.status,
            "message": j.message,
            "summary": j.summary,
            "created_at": j.created_at.isoformat(),
        }
        for j in sorted(jobs.values(), key=lambda x: x.created_at, reverse=True)
        if j.step == "step3"
    ]
