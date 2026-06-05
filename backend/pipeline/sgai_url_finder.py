#!/usr/bin/env python3
"""
sgai_url_finder.py
──────────────────
ScrapeGraphAI URL discovery pipeline for SIC codes.

Workflow:
1. Read input Excel
2. For each company, call ScrapeGraph Search (numResults=2, locationGeoCode="gb")
3. Validate the returned URL against blacklist + TLD rules + name-token matching
4. Write checkpoint after every row (safe to interrupt & resume)
5. Save homepage content from ScrapeGraph response alongside URL
   (free — already returned by the API, useful for downstream address extraction)
6. On completion, write output Excel with 2 sheets:
   {sheet}_with_url — HIGH/MEDIUM confidence URLs
   {sheet}_no_url — DISCARD / no result
7. Auto-grow the blacklist: unknown domains with 0 name-token match get
   written to blacklist_candidates.txt for your review.

Usage:
    pip install pandas openpyxl requests python-dotenv tqdm
    python sgai_url_finder.py path/to/data.xlsx --sheet 80200 --col "Business Name"

    # After reviewing blacklist_candidates.txt, promote entries:
    python sgai_url_finder.py --add-blacklist "somesite.com" "anothersite.co.uk"

Environment:
    SGAI_API_KEY=your_scrapegraphai_api_key (or in .env file)

Resume:
    Re-run the same command — already-processed rows are skipped automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import html
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pandas as pd
import requests
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

SGAI_API_KEY = os.getenv("SGAI_API_KEY", "")
SGAI_SEARCH_URL = "https://v2-api.scrapegraphai.com/api/search"

INPUT_SHEET = "Sheet1"
COL_NAME = "Business Name"
CHECKPOINT_FILE = "sgai_url_checkpoint.json"
BLACKLIST_FILE = "blacklist.json"
CANDIDATES_FILE = "blacklist_candidates.txt"
OUTPUT_SUFFIX = "_sgai_urls.xlsx"

RATE_LIMIT_SLEEP = 1.2  # seconds between API calls
MAX_RETRIES = 3
RETRY_SLEEP = 5

# ── BLACKLIST ─────────────────────────────────────────────────────────────────

SEED_BLACKLIST = [
    # Job / professional networks
    "linkedin.com",
    "glassdoor.com",
    "reed.co.uk",
    "totaljobs.com",
    "indeed.com",
    "cv-library.co.uk",

    # Business directories & enrichment
    "yell.com",
    "thomsonlocal.com",
    "checkatrade.com",
    "trustatrader.com",
    "hotfrog.co.uk",
    "cylex.co.uk",
    "192.com",
    "find-us-here.com",
    "bizify.co.uk",
    "freeindex.co.uk",
    "serchen.co.uk",
    "businessnetwork.co.uk",

    # Company registries & data providers
    "companieshouse.gov.uk",
    "find-and-update.company-information.service.gov.uk",  # CH subdomain
    "endole.co.uk",
    "opencorporates.com",
    "duedil.com",
    "credencedata.com",
    "firstreport.co.uk",
    "screener.in",
    "ukcompanies.lursoft.lv",
    "tinytax.co.uk",

    # Finance & research
    "finance.yahoo.com",
    "morningstar.com",
    "bloomberg.com",
    "reuters.com",

    # Contact / lead enrichment
    "rocketreach.co",
    "lusha.com",
    "zoominfo.com",
    "apollo.io",
    "hunter.io",
    "clearbit.com",
    "signalhire.com",

    # Social / media / misc
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "spotify.com",
    "crunchbase.com",
    "wikipedia.org",
    "wikidata.org",

    # Design / SaaS tools (wrong company hits)
    "figma.com",

    # Security vendor products (not company sites)
    "securityscorecard.com",

    # Review sites
    "trustpilot.com",
    "reviews.io",
    "g2.com",
    "capterra.com",
    "getapp.com",
]

# TLDs that are definitively non-UK country codes
REJECTED_COUNTRY_TLDS = [
    ".com.au", ".co.au", ".net.au",
    ".co.us", ".us",
    ".co.ca", ".ca",
    ".co.nz", ".net.nz",
    ".co.za",
    ".co.in", ".net.in",
    ".de", ".fr", ".es", ".it",
    ".nl", ".be", ".pl", ".se",
    ".no", ".dk", ".fi", ".pt",
    ".ru", ".cn", ".jp", ".kr",
    ".br", ".mx", ".ar",
    ".ae", ".sa",
    ".com.ua",  # added — caught octopussecurity.com.ua
]

STRIP_WORDS = {
    "ltd", "limited", "llp", "plc", "inc", "corp", "co",
    "uk", "gb", "england", "scotland", "wales",
    "the", "and", "&", "of", "for",
}

WEAK_WORDS = {
    "services", "solutions", "technologies", "technology",
    "consulting", "consultancy", "cyber", "security", "group", "holdings",
    "systems", "global", "international", "digital", "fire", "alarm", "guard",
    "guards", "monitoring", "safes", "safe",
    "protection", "patrol", "surveillance",
}

# ── BLACKLIST MANAGEMENT ──────────────────────────────────────────────────────

def load_blacklist() -> set[str]:
    bl = set(SEED_BLACKLIST)
    if Path(BLACKLIST_FILE).exists():
        with open(BLACKLIST_FILE) as f:
            extra = json.load(f)
            bl.update(extra.get("domains", []))
    return bl

def save_blacklist_additions(new_domains: list[str]) -> None:
    existing = {}
    if Path(BLACKLIST_FILE).exists():
        with open(BLACKLIST_FILE) as f:
            existing = json.load(f)
    domains = list(set(existing.get("domains", []) + new_domains))
    with open(BLACKLIST_FILE, "w") as f:
        json.dump({"domains": sorted(domains)}, f, indent=2)
    print(f" Added {len(new_domains)} domain(s) to {BLACKLIST_FILE}")

def remove_blacklist_domains(domains: list[str]) -> None:
    if not Path(BLACKLIST_FILE).exists():
        print("No blacklist file found, nothing to remove.")
        return
    with open(BLACKLIST_FILE) as f:
        existing = json.load(f)
    before = set(existing.get("domains", []))
    after = before - {d.lower().lstrip("www.") for d in domains}
    removed = before - after
    with open(BLACKLIST_FILE, "w") as f:
        json.dump({"domains": sorted(after)}, f, indent=2)
    print(f" Removed {len(removed)} domain(s): {sorted(removed)}")
    print(f" Blacklist size now: {len(load_blacklist())}")

def flag_candidate(domain: str, business_name: str, url: str) -> None:
    if Path(CANDIDATES_FILE).exists():
        existing_lines = Path(CANDIDATES_FILE).read_text().splitlines()
        existing_domains = {line.split("|")[0].strip() for line in existing_lines if line}
        if domain in existing_domains:
            return
    line = f"{domain} | business='{business_name}' | url={url}\n"
    with open(CANDIDATES_FILE, "a") as f:
        f.write(line)

# ── CHECKPOINT MANAGEMENT ─────────────────────────────────────────────────────

def load_checkpoint() -> dict:
    checkpoint = {}
    has_duplicates = False

    if Path(CHECKPOINT_FILE).exists():
        with open(CHECKPOINT_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    idx = entry["idx"]
                    if idx in checkpoint:
                        has_duplicates = True
                    checkpoint[idx] = entry

    if has_duplicates:
        print(" Compacting checkpoint file (duplicates found)...")
        _compact_checkpoint(checkpoint)

    return checkpoint

def _compact_checkpoint(checkpoint: dict) -> None:
    with open(CHECKPOINT_FILE, "w") as f:
        for entry in checkpoint.values():
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def save_checkpoint(row_key: str, result: dict) -> None:
    entry = {"idx": row_key, **result}
    with open(CHECKPOINT_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

# ── URL HELPERS ───────────────────────────────────────────────────────────────

def clean_url(url: str) -> str:
    """Strip query strings and fragments — removes tracking params like ?srsltid=..."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        # Replace query and fragment with empty strings
        cleaned = urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            "",  # query — stripped
            "",  # fragment — stripped
        ))
        return cleaned
    except Exception:
        return url


def extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url if url.startswith("http") else "https://" + url)
        return parsed.netloc.lower().lstrip("www.")
    except Exception:
        return ""

def extract_path_depth(url: str) -> int:
    try:
        parsed = urlparse(url if url.startswith("http") else "https://" + url)
        segments = [s for s in parsed.path.split("/") if s]
        return len(segments)
    except Exception:
        return 0

def is_rejected_tld(domain: str) -> bool:
    return any(domain.endswith(tld) for tld in REJECTED_COUNTRY_TLDS)

# ── NAME TOKEN MATCHING ───────────────────────────────────────────────────────

def name_tokens(business_name: str) -> list[str]:
    business_name = html.unescape(business_name)
    raw = re.sub(r"[^a-zA-Z0-9\s]", " ", business_name.lower())
    tokens = raw.split()
    return [t for t in tokens if t not in STRIP_WORDS and len(t) > 2]

def token_match_score(tokens: list[str], domain: str) -> tuple[int, int]:
    domain_clean = re.sub(r"[^a-z0-9]", "", domain.lower())
    strong = sum(1 for t in tokens if t not in WEAK_WORDS and t in domain_clean)
    weak = sum(1 for t in tokens if t in WEAK_WORDS and t in domain_clean)
    return strong, weak

# ── URL VALIDATION ────────────────────────────────────────────────────────────

def validate_url(
    business_name: str,
    url: str,
    blacklist: set[str],
) -> tuple[str, str]:
    """Returns (confidence, reason) — HIGH | MEDIUM | LOW | DISCARD"""

    if not url or not url.strip():
        return "DISCARD", "no url returned"

    domain = extract_domain(url)
    if not domain:
        return "DISCARD", "unparseable url"

    # 1. Blacklist
    for bl_domain in blacklist:
        if domain == bl_domain or domain.endswith("." + bl_domain):
            return "DISCARD", f"blacklisted: {bl_domain}"

    # 2. Non-UK country TLD
    if is_rejected_tld(domain):
        return "DISCARD", f"non-UK country TLD: {domain}"

    # 3. Deep subpage — likely a directory listing
    depth = extract_path_depth(url)
    if depth >= 3:
        return "DISCARD", f"deep subpage (depth={depth}), likely directory"

    # 4. Name token matching
    tokens = name_tokens(business_name)
    if len(tokens) == 0:
        return "LOW", "no meaningful name tokens to match"

    strong, weak = token_match_score(tokens, domain)

    if strong >= 1:
        return "HIGH", f"strong token match ({strong} distinctive word(s) in domain)"
    elif weak >= 2:
        return "MEDIUM", f"multiple weak token match ({weak} generic words in domain)"
    elif weak == 1:
        return "LOW", "single weak token match — flagged as candidate"
    else:
        return "LOW", f"no token match (0/{len(tokens)}) — flagged as candidate"

# ── SCRAPEGRAPH API ───────────────────────────────────────────────────────────

def search_company_url(
    business_name: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
    """
    Returns (url1, title1, content1, url2, title2, content2).
    content1/content2 — raw homepage text returned by ScrapeGraph (free, no extra credits).
    Stored in checkpoint for downstream address extraction without re-scraping.
    """
    headers = {
        "SGAI-APIKEY": SGAI_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "query": f"{business_name} .com OR .co.uk website",
        "numResults": 2,
        "locationGeoCode": "gb",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                SGAI_SEARCH_URL,
                headers=headers,
                json=payload,
                timeout=120,
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                if results:
                    r1 = results[0]
                    r2 = results[1] if len(results) > 1 else {}
                    return (
                        r1.get("url"),
                        r1.get("title"),
                        r1.get("content", ""),  # homepage text — stored free
                        r2.get("url"),
                        r2.get("title"),
                        r2.get("content", ""),
                    )
                return None, None, "", None, None, ""

            elif resp.status_code == 429:
                wait = RETRY_SLEEP * attempt
                print(f"\n Rate limited — waiting {wait}s...")
                time.sleep(wait)

            else:
                print(f"\n HTTP {resp.status_code} for '{business_name}': {resp.text[:200]}")
                return None, None, "", None, None, ""

        except requests.exceptions.Timeout:
            print(f"\n Timeout on attempt {attempt} for '{business_name}'")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP)

        except requests.exceptions.ConnectionError as e:
            print(f"\n Connection error on attempt {attempt} for '{business_name}': {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP)

        except Exception as e:
            print(f"\n Error for '{business_name}': {e}")
            return None, None, "", None, None, ""

    # All retries exhausted on transient failures (timeout / rate limit / connection)
    # — raise so the caller knows NOT to checkpoint this row
    raise NetworkError(f"All {MAX_RETRIES} retries exhausted for '{business_name}'")

def confidence_rank(confidence: str) -> int:
    return {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "DISCARD": 0}.get(confidence, 0)

class NetworkError(Exception):
    """Raised when the API call failed for transient reasons (timeout, rate limit exhaustion).

    Rows that hit this should NOT be checkpointed — they need to be retried on next run.
    """
    pass


def _write_excel(output_path: str, df_with_url, df_no_url, sheet: str) -> None:
    """Write the two output sheets and enforce uniform default row heights."""
    from openpyxl.styles import Alignment

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df_with_url.to_excel(writer, sheet_name=f"{sheet}_with_url", index=False)
        df_no_url.to_excel(writer, sheet_name=f"{sheet}_no_url", index=False)

        for ws in writer.sheets.values():
            # Lock every row to default height (15 pt) and disable wrap-text
            for row in ws.iter_rows():
                ws.row_dimensions[row[0].row].height = 15
                for cell in row:
                    if cell.alignment and cell.alignment.wrap_text:
                        cell.alignment = Alignment(wrap_text=False)

# ── MAIN PIPELINE ─────────────────────────────────────────────────────────────

def run(
    input_path: str,
    limit: int | None = None,
    auto_blacklist: bool = False,
    stop_after: int | None = None,
    max_workers: int = 15,
    export_only: bool = False,
) -> None:
    if not SGAI_API_KEY:
        print(" SGAI_API_KEY not set. Add it to your .env file.")
        sys.exit(1)

    print(f" Loading {input_path}...")
    df = pd.read_excel(input_path, sheet_name=INPUT_SHEET, dtype=str)
    df = df.fillna("")

    # Auto-detect company name column
    if COL_NAME not in df.columns:
        candidates = [
            c for c in df.columns
            if "name" in c.lower() or "company" in c.lower() or "business" in c.lower()
        ]
        if candidates:
            print(f" Column '{COL_NAME}' not found — using '{candidates[0]}'")
            df = df.rename(columns={candidates[0]: COL_NAME})
        else:
            print(f" Could not find company name column. Columns: {list(df.columns)}")
            sys.exit(1)

    if limit:
        df = df.iloc[:limit].copy()
    total = len(df)

    print(f" {total:,} rows loaded from sheet '{INPUT_SHEET}'")

    blacklist = load_blacklist()
    checkpoint = load_checkpoint()
    print(f" {len(checkpoint):,} rows already processed (resuming)")
    print(f" {len(blacklist):,} domains in blacklist")
    print(f" workers={max_workers}\n")

    for col in ["found_url", "url_title", "url_confidence", "url_reason"]:
        if col not in df.columns:
            df[col] = ""

    # ── Thread-safety primitives ──────────────────────────────────────────
    checkpoint_lock = threading.Lock()  # guards checkpoint dict + file writes
    candidates_lock = threading.Lock()  # guards blacklist_candidates.txt
    blacklist_lock = threading.Lock()   # guards in-memory blacklist set
    df_lock = threading.Lock()          # guards df.at[] writes
    pbar_lock = threading.Lock()        # guards tqdm updates
    counter_lock = threading.Lock()     # guards processed_this_run
    stop_event = threading.Event()      # signals all workers to stop

    processed_this_run = 0
    already_done = sum(1 for i in range(len(df)) if str(i) in checkpoint)

    # ── Export-only mode: skip all API calls, just write Excel from checkpoint ──
    if export_only:
        print(" --export-only: skipping API calls, writing Excel from checkpoint...")
        final_checkpoint = load_checkpoint()
        for col in ["found_url", "url_title", "url_confidence", "url_reason"]:
            if col not in df.columns:
                df[col] = ""
        for idx, row in df.iterrows():
            r = final_checkpoint.get(str(idx), {})
            df.at[idx, "found_url"] = r.get("url", "")
            df.at[idx, "url_title"] = r.get("title", "")
            df.at[idx, "url_confidence"] = r.get("confidence", "")
            df.at[idx, "url_reason"] = r.get("reason", "")
        df_with_url = df[df["url_confidence"].isin(["HIGH", "MEDIUM"])].copy()
        df_no_url = df[~df["url_confidence"].isin(["HIGH", "MEDIUM"])].copy()
        output_path = Path(input_path).stem + OUTPUT_SUFFIX
        _write_excel(output_path, df_with_url, df_no_url, INPUT_SHEET)
        print(f"\n Results:")
        print(f"   HIGH confidence  : {len(df[df['url_confidence'] == 'HIGH']):>8,}")
        print(f"   MEDIUM confidence: {len(df[df['url_confidence'] == 'MEDIUM']):>8,}")
        print(f"   LOW              : {len(df[df['url_confidence'] == 'LOW']):>8,}")
        print(f"   DISCARD          : {len(df[df['url_confidence'] == 'DISCARD']):>8,}")
        print(f"\n Output → {output_path}")
        return

    # Build list of rows that still need processing
    pending = [
        (idx, str(row[COL_NAME]).strip())
        for idx, row in df.iterrows()
        if str(idx) not in checkpoint
    ]

    def process_row(idx: int, company_name: str) -> None:
        nonlocal processed_this_run

        if stop_event.is_set():
            return

        row_key = str(idx)

        # Empty name — checkpoint immediately, no API call
        if not company_name:
            result = {
                "url": "", "title": "", "confidence": "DISCARD",
                "reason": "empty name",
            }
            with checkpoint_lock:
                checkpoint[row_key] = result
                save_checkpoint(row_key, result)
            with df_lock:
                for k, v in [("found_url", ""), ("url_title", ""),
                             ("url_confidence", "DISCARD"), ("url_reason", "empty name"),
                             ]:
                    df.at[idx, k] = v
            with pbar_lock:
                pbar.update(1)
            return

        # ── API call ──────────────────────────────────────────────────────
        try:
            url1, title1, content1, url2, title2, content2 = search_company_url(company_name)
        except NetworkError as e:
            # Transient failure — do NOT checkpoint, row stays in pending for next run
            print(f"\n Skipping row {idx} (will retry next run): {e}")
            with pbar_lock:
                pbar.update(1)
            return

        # Clean query strings
        url1 = clean_url(url1) if url1 else url1
        url2 = clean_url(url2) if url2 else url2

        # Pick best result
        url = title = sg_content = ""
        confidence = "DISCARD"
        reason = "no result from search"
        domain = ""
        best_rank = 0

        with blacklist_lock:
            bl_snapshot = set(blacklist)  # snapshot for this row — avoids holding lock during validation

        for candidate_url, candidate_title, candidate_content in [
            (url1, title1, content1),
            (url2, title2, content2),
        ]:
            if not candidate_url:
                continue
            c, r = validate_url(company_name, candidate_url, bl_snapshot)
            rank = confidence_rank(c)
            if rank > best_rank:
                best_rank = rank
                url = candidate_url
                title = candidate_title or ""
                confidence = c
                reason = r
                domain = extract_domain(candidate_url)
                sg_content = candidate_content or ""

        # Flag LOW confidence domains
        if confidence == "LOW" and domain:
            if auto_blacklist:
                with blacklist_lock:
                    save_blacklist_additions([domain])
                    blacklist.add(domain)
            with candidates_lock:
                flag_candidate(domain, company_name, url)

        # Write to df
        with df_lock:
            df.at[idx, "found_url"] = url
            df.at[idx, "url_title"] = title
            df.at[idx, "url_confidence"] = confidence
            df.at[idx, "url_reason"] = reason

        # Checkpoint
        result = {
            "url": url,
            "title": title,
            "confidence": confidence,
            "reason": reason,
            "sg_content": sg_content,
        }
        with checkpoint_lock:
            checkpoint[row_key] = result
            save_checkpoint(row_key, result)

        # Counter + progress + stop check
        with counter_lock:
            processed_this_run += 1
            current = processed_this_run

        with pbar_lock:
            pbar.set_postfix({"last": company_name[:28], "conf": confidence})
            pbar.update(1)

        if stop_after and current >= stop_after:
            stop_event.set()

    # ── Run with thread pool ──────────────────────────────────────────────
    with tqdm(total=total, initial=already_done, unit="co") as pbar:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(process_row, idx, name): idx
                for idx, name in pending
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    idx = futures[future]
                    print(f"\n Unhandled error on row {idx}: {e}")

    # Snapshot counter under lock to avoid stale read
    with counter_lock:
        final_count = processed_this_run

    if stop_event.is_set():
        print(f"\n --stop-after {stop_after} reached (~{final_count} rows) — pausing cleanly.")
        print(" Review blacklist_candidates.txt, add domains, then re-run to continue.")
    else:
        print(f"\n Processing complete — {final_count:,} new rows processed.")

    # ── Output Excel ──────────────────────────────────────────────────────
    # Re-read checkpoint to fill any rows processed in this run
    final_checkpoint = load_checkpoint()
    for idx, row in df.iterrows():
        row_key = str(idx)
        if row_key in final_checkpoint:
            r = final_checkpoint[row_key]
            df.at[idx, "found_url"] = r.get("url", "")
            df.at[idx, "url_title"] = r.get("title", "")
            df.at[idx, "url_confidence"] = r.get("confidence", "")
            df.at[idx, "url_reason"] = r.get("reason", "")

    df_with_url = df[df["url_confidence"].isin(["HIGH", "MEDIUM"])].copy()
    df_no_url = df[~df["url_confidence"].isin(["HIGH", "MEDIUM"])].copy()

    output_path = Path(input_path).stem + OUTPUT_SUFFIX
    _write_excel(output_path, df_with_url, df_no_url, INPUT_SHEET)

    print(f"\n Results:")
    print(f"   HIGH confidence  : {len(df[df['url_confidence'] == 'HIGH']):>8,}")
    print(f"   MEDIUM confidence: {len(df[df['url_confidence'] == 'MEDIUM']):>8,}")
    print(f"   LOW              : {len(df[df['url_confidence'] == 'LOW']):>8,}")
    print(f"   DISCARD          : {len(df[df['url_confidence'] == 'DISCARD']):>8,}")
    print(f"\n Output → {output_path}")

    if Path(CANDIDATES_FILE).exists():
        candidate_count = sum(1 for _ in open(CANDIDATES_FILE))
        if candidate_count > 0:
            print(f"\n {candidate_count} blacklist candidate(s) → '{CANDIDATES_FILE}'")
            print(f" Review and promote with:")
            print(f"   python sgai_url_finder.py --add-blacklist <domain1> <domain2> ...")

# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    global INPUT_SHEET, COL_NAME

    parser = argparse.ArgumentParser(description="ScrapeGraphAI URL finder")
    parser.add_argument("input", nargs="?", help="Path to input Excel file")
    parser.add_argument("--add-blacklist", nargs="+", metavar="DOMAIN")
    parser.add_argument("--remove-blacklist", nargs="+", metavar="DOMAIN")
    parser.add_argument("--show-blacklist", action="store_true")
    parser.add_argument("--sheet", default=INPUT_SHEET,
                        help=f"Sheet name (default: {INPUT_SHEET})")
    parser.add_argument("--col", default=COL_NAME,
                        help=f"Business name column (default: '{COL_NAME}')")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only this many rows")
    parser.add_argument("--auto-blacklist", action="store_true",
                        help="Auto-add LOW confidence domains to blacklist")
    parser.add_argument("--stop-after", type=int, default=None,
                        help="Stop after processing this many NEW rows this run, then exit cleanly")
    parser.add_argument("--workers", type=int, default=15,
                        help="Concurrent API workers (default: 15, matches Growth plan)")
    parser.add_argument("--export-only", action="store_true",
                        help="Skip all API calls — write Excel from existing checkpoint only")
    parser.add_argument("--test", action="store_true",
                        help="Single test API call — prints raw response")
    args = parser.parse_args()

    INPUT_SHEET = args.sheet
    COL_NAME = args.col

    if args.add_blacklist:
        domains = [d.lower().lstrip("www.") for d in args.add_blacklist]
        save_blacklist_additions(domains)
        print("Blacklist size now:", len(load_blacklist()))
        return

    if args.remove_blacklist:
        remove_blacklist_domains(args.remove_blacklist)
        return

    if args.show_blacklist:
        bl = sorted(load_blacklist())
        print(f"Blacklist ({len(bl)} domains):")
        for d in bl:
            print(f"   {d}")
        return

    if args.test:
        print("Running test call with 'Bluedog Cyber Security Limited'...")
        bl = load_blacklist()
        try:
            url1, title1, content1, url2, title2, content2 = search_company_url(
                "Bluedog Cyber Security Limited"
            )
        except NetworkError as e:
            print(f" Network error: {e}")
            return
        for i, (u, t, c) in enumerate([(url1, title1, content1), (url2, title2, content2)], 1):
            print(f"\n R{i}: {u}")
            print(f"   Title: {t}")
            print(f"   Content ({len(c or '')} chars): {(c or '')[:200]}...")
            if u:
                conf, reason = validate_url("Bluedog Cyber Security Limited", u, bl)
                print(f"   Validation: {conf} — {reason}")
        return

    if not args.input:
        parser.print_help()
        sys.exit(1)

    run(args.input, limit=args.limit, auto_blacklist=args.auto_blacklist, stop_after=args.stop_after, max_workers=args.workers, export_only=args.export_only)

if __name__ == "__main__":
    main()
