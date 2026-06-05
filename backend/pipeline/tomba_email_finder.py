#!/usr/bin/env python3
"""
Tomba Email Finder — generic multi-sheet version
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

For any Excel file where every sheet has a website/domain column:
• Auto-detects all sheets — no hardcoded sheet names.
• Processes every sheet in the file automatically.
• Finds work/company emails via Tomba Email Finder API.
• Personal emails are ignored (Tomba's email-finder endpoint
  returns work emails only by design).
• Checkpoint file — safe to interrupt and resume without re-spending hits.
• Writes a separate output file with 2 sheets per source sheet:
  {sheet}_email — rows where Tomba found an email
  {sheet}_no_email — rows where Tomba found nothing
• Input file is NEVER modified.
• Adds 4 columns: Tomba_Email, Tomba_Confidence,
  Tomba_Verification, Tomba_Position

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This is a web-app friendly fork: INPUT_PATH / OUTPUT_PATH / CHECKPOINT_PATH are
module-level variables that the FastAPI backend (main.py) sets before calling
main() directly. main() also accepts a progress_cb(current, total, label)
callback and returns a summary dict on completion.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Usage (CLI — overrides INPUT_PATH below):
    python tomba_email_finder.py path/to/your_file.xlsx
"""

import os
import sys
import json
import time
import threading
from collections import deque
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import pandas as pd
from tqdm import tqdm
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════
# ── Paths (overrideable — main.py sets these before calling main()) ───────
INPUT_PATH = Path("input.xlsx")
OUTPUT_PATH = Path("output/input_tomba.xlsx")
CHECKPOINT_PATH = Path("input_tomba_checkpoint.json")
# ══════════════════════════════════════════════════════════════════════════

# Column names — edit if your file uses different headers
COL_FNAME = "Fname"
COL_SNAME = "Sname"
COL_WEBSITE = "found_url"  # column containing the company URL/domain

# Output column names (added to each sheet)
COL_EMAIL = "Tomba_Email"
COL_CONFIDENCE = "Tomba_Confidence"
COL_VERIFICATION = "Tomba_Verification"
COL_POSITION = "Tomba_Position"

# Tomba settings
TOMBA_API_KEY = os.getenv("TOMBA_API_KEY")
TOMBA_SECRET = os.getenv("TOMBA_SECRET")
RATE_LIMIT_PER_MIN = 60  # Tomba free: 60/min; raise if your plan allows more
MAX_WORKERS = 5
CHECKPOINT_EVERY = 100  # save progress every N completed rows


# ── Checkpoint helpers ─────────────────────────────────────────────────────
def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        try:
            return json.loads(CHECKPOINT_PATH.read_text())
        except Exception as e:
            print(f"  [warn] Could not read checkpoint ({e}) — starting fresh.")
    return {}


def save_checkpoint(data: dict):
    tmp = CHECKPOINT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(CHECKPOINT_PATH)


# ── Rate limiter ───────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, max_calls: int, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= self.period:
                    self.calls.popleft()
                if len(self.calls) < self.max_calls:
                    self.calls.append(now)
                    return
                sleep_for = self.period - (now - self.calls[0]) + 0.01
            time.sleep(sleep_for)


limiter = RateLimiter(RATE_LIMIT_PER_MIN)


# ── Helpers ────────────────────────────────────────────────────────────────
def extract_domain(website: str) -> str:
    s = str(website).strip()
    if not s or s.lower() in ("nan", "none", ""):
        return ""
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    try:
        netloc = urlparse(s).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def query_tomba(first_name: str, last_name: str, domain: str) -> dict:
    empty = {
        "email": "", "confidence": "",
        "verification": "", "position": ""
    }
    if not (TOMBA_API_KEY and TOMBA_SECRET):
        return {**empty, "email": "NO_KEY"}
    if not all([domain, first_name, last_name]):
        return empty

    limiter.acquire()
    try:
        r = requests.get(
            "https://api.tomba.io/v1/email-finder",
            headers={
                "X-Tomba-Key": TOMBA_API_KEY,
                "X-Tomba-Secret": TOMBA_SECRET,
            },
            params={
                "domain": domain,
                "first_name": first_name,
                "last_name": last_name,
            },
            timeout=45,
        )
        if r.status_code == 200:
            data = r.json().get("data", {}) or {}
            ver = data.get("verification", {}) or {}

            email = data.get("email", "") or ""

            # Skip personal emails — only keep work/company emails.
            personal_domains = {
                "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                "icloud.com", "live.com", "me.com", "aol.com",
                "protonmail.com", "mail.com",
            }
            if email:
                email_domain = email.split("@")[-1].lower() if "@" in email else ""
                if email_domain in personal_domains:
                    return empty  # discard personal email

            return {
                "email": email,
                "confidence": data.get("score", ""),
                "verification": ver.get("status", "") if isinstance(ver, dict) else str(ver),
                "position": data.get("position", "") or "",
            }
        elif r.status_code == 429:
            time.sleep(3)
            return query_tomba(first_name, last_name, domain)
        else:
            return empty
    except Exception as e:
        tqdm.write(f"  [error] {first_name} {last_name} @ {domain}: {e}")
        return empty


# ── Per-sheet enrichment ───────────────────────────────────────────────────
def enrich_sheet(df: pd.DataFrame, sheet_name: str,
                 checkpoint: dict, progress_cb=None) -> pd.DataFrame:
    df = df.copy()

    # Validate required columns
    missing = [c for c in (COL_FNAME, COL_SNAME, COL_WEBSITE) if c not in df.columns]
    if missing:
        print(f"  [{sheet_name}] WARNING: missing columns {missing} — skipping sheet.")
        return df

    # Initialise output columns
    for col in (COL_EMAIL, COL_CONFIDENCE, COL_VERIFICATION, COL_POSITION):
        df[col] = ""

    # Load cached results for this sheet from checkpoint
    sheet_cp: dict = checkpoint.get(sheet_name, {})  # {str(idx): result_dict}
    already_done = set(sheet_cp.keys())

    # Apply cached results immediately to the dataframe
    for idx_str, res in sheet_cp.items():
        try:
            idx_int = int(idx_str)
            if res.get("email"):
                df.at[idx_int, COL_EMAIL] = res.get("email", "")
                df.at[idx_int, COL_CONFIDENCE] = res.get("confidence", "")
                df.at[idx_int, COL_VERIFICATION] = res.get("verification", "")
                df.at[idx_int, COL_POSITION] = res.get("position", "")
        except Exception:
            pass

    # Build task list, skipping rows already in checkpoint
    tasks, skipped = [], 0
    for idx, row in df.iterrows():
        if str(idx) in already_done:
            continue

        fname = str(row.get(COL_FNAME, "")).strip()
        sname = str(row.get(COL_SNAME, "")).strip()
        domain = extract_domain(str(row.get(COL_WEBSITE, "")))

        if (
            not fname or fname.lower() in ("nan", "none", "") or
            not sname or sname.lower() in ("nan", "none", "") or
            not domain
        ):
            skipped += 1
            continue

        tasks.append((idx, fname, sname, domain))

    cached_hits = sum(1 for r in sheet_cp.values() if r.get("email"))
    print(f"\n  [{sheet_name}] Rows total      : {len(df):,}")
    print(f"  [{sheet_name}] From checkpoint : {len(already_done):,} "
          f"({cached_hits:,} hits cached)")
    print(f"  [{sheet_name}] Queued          : {len(tasks):,}")
    print(f"  [{sheet_name}] Skipped         : {skipped:,} (missing name or domain)")
    if tasks:
        print(f"  [{sheet_name}] Est. time       : ~{len(tasks) / RATE_LIMIT_PER_MIN:.1f} min")

    if not tasks:
        print(f"  [{sheet_name}] Nothing new to enrich.")
        return df

    # idx → (fname, sname, domain) for building progress labels
    task_meta = {idx: (f, s, d) for idx, f, s, d in tasks}

    hits, misses, completed = 0, 0, 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(query_tomba, f, s, d): idx
            for idx, f, s, d in tasks
        }

        with tqdm(total=len(tasks), desc=f"  [{sheet_name}]", unit="contact") as pbar:
            for fut in as_completed(futures):
                idx = futures[fut]
                res = fut.result()

                if res["email"] and res["email"] != "NO_KEY":
                    df.at[idx, COL_EMAIL] = res["email"]
                    df.at[idx, COL_CONFIDENCE] = res["confidence"]
                    df.at[idx, COL_VERIFICATION] = res["verification"]
                    df.at[idx, COL_POSITION] = res["position"]
                    hits += 1
                else:
                    misses += 1

                # Persist this row's result to checkpoint dict
                sheet_cp[str(idx)] = {
                    "email": res.get("email", "") if res.get("email") != "NO_KEY" else "",
                    "confidence": res.get("confidence", ""),
                    "verification": res.get("verification", ""),
                    "position": res.get("position", ""),
                }
                checkpoint[sheet_name] = sheet_cp

                completed += 1
                pbar.update(1)
                rpm = completed / max(time.time() - start, 0.1) * 60
                pbar.set_postfix(hits=hits, misses=misses, rpm=f"{rpm:.0f}")

                # Report per-row progress to the caller (web backend), if asked.
                if progress_cb is not None:
                    f, s, d = task_meta.get(idx, ("", "", ""))
                    progress_cb(completed, len(tasks), f"{f} {s} @ {d}")

                # Persist checkpoint to disk every N rows
                if completed % CHECKPOINT_EVERY == 0:
                    save_checkpoint(checkpoint)
                    tqdm.write(
                        f"  [{sheet_name}] checkpoint saved @ {completed} — "
                        f"{hits} hits / {misses} misses"
                    )

    # Final checkpoint save for this sheet
    save_checkpoint(checkpoint)

    elapsed = time.time() - start
    print(
        f"\n  [{sheet_name}] Done — {hits:,} new emails found "
        f"({hits / len(tasks) * 100:.1f}%) in {elapsed / 60:.1f} min"
    )
    return df


# ── Main ───────────────────────────────────────────────────────────────────
def main(progress_cb=None) -> dict:
    if not TOMBA_API_KEY or not TOMBA_SECRET:
        sys.exit("ERROR: TOMBA_API_KEY / TOMBA_SECRET not set in .env")
    if not INPUT_PATH.exists():
        sys.exit(f"ERROR: file not found: {INPUT_PATH}")

    print(f"Input      : {INPUT_PATH.name}")
    print(f"Output     : {OUTPUT_PATH.name} (input never modified)")
    print(f"Checkpoint : {CHECKPOINT_PATH.name}")

    checkpoint = load_checkpoint()
    if checkpoint:
        print(f"  ↺ Resuming — checkpoint has data for sheets: {list(checkpoint.keys())}")

    xl = pd.ExcelFile(INPUT_PATH)
    sheet_names = xl.sheet_names
    print(f"Sheets     : {sheet_names}")

    enriched: dict[str, pd.DataFrame] = {}
    for sheet in sheet_names:
        print(f"\n{'═' * 55}")
        print(f"  Sheet: '{sheet}'")
        print(f"{'═' * 55}")
        try:
            df_raw = pd.read_excel(INPUT_PATH, sheet_name=sheet, dtype=str)
        except Exception as e:
            print(f"  [warn] Could not read '{sheet}': {e} — skipping.")
            continue
        enriched[sheet] = enrich_sheet(df_raw, sheet, checkpoint, progress_cb)

    if not enriched:
        sys.exit("No sheets were successfully processed.")

    # Write output: 2 sheets per source sheet
    print(f"\n{'─' * 55}")
    print(f"Writing {OUTPUT_PATH.name} …")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    total_rows = emails_found = no_email = 0
    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        for sheet, df in enriched.items():
            has_email = df[COL_EMAIL].str.strip() != ""
            df_email = df[has_email].copy()
            df_no_email = df[~has_email].copy()

            # Excel sheet names max 31 chars
            name_email = f"{sheet}_email"[:31]
            name_no_email = f"{sheet}_no_email"[:31]

            df_email.to_excel(writer, sheet_name=name_email, index=False)
            df_no_email.to_excel(writer, sheet_name=name_no_email, index=False)

            total_rows += len(df)
            emails_found += len(df_email)
            no_email += len(df_no_email)

            print(f"  {name_email:<32} → {len(df_email):,} rows")
            print(f"  {name_no_email:<32} → {len(df_no_email):,} rows")

    print(f"\nOutput saved: {OUTPUT_PATH}")

    # Clean up checkpoint only on a fully successful run
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print("Checkpoint deleted (run completed successfully).")

    return {
        "sheets_processed": len(enriched),
        "total_rows": total_rows,
        "emails_found": emails_found,
        "no_email": no_email,
    }


if __name__ == "__main__":
    # CLI overrides the module-level paths from argv before running.
    if len(sys.argv) > 1:
        INPUT_PATH = Path(sys.argv[1]).expanduser().resolve()
        OUTPUT_PATH = INPUT_PATH.with_name(INPUT_PATH.stem + "_tomba.xlsx")
        CHECKPOINT_PATH = INPUT_PATH.with_name(INPUT_PATH.stem + "_tomba_checkpoint.json")
    main()
