"""
Four-phase address validation pipeline.

Phase 1 (scrape_websites.py)  — scrapes each company website with ScrapeGraphAI
Phase 2 (extract_addresses.py) — extracts structured addresses via Anthropic Messages API
Phase 3 (uk_filter.py)         — filters non-UK addresses (country + postcode checks)
Phase 4 (compare_addresses.py) — compares/replaces Excel address columns, saves output

Usage:
    python address_pipeline.py data.xlsx
    python address_pipeline.py data.xlsx --skip-scrape  # reuse scraped_content.jsonl
    python address_pipeline.py data.xlsx --dry-run      # preview without API calls
    python address_pipeline.py data.xlsx --resume       # skip already-processed rows

Resume is always enabled by default — if checkpoint files exist in the working
directory the pipeline picks up where it left off rather than starting from scratch.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

import compare_addresses
import extract_addresses
import scrape_websites
import uk_filter

# Haiku 4.5 batch pricing (50 % discount over standard)
_INPUT_COST_PER_TOKEN = 0.50 / 1_000_000   # $0.50 per 1 M input tokens
_OUTPUT_COST_PER_TOKEN = 2.50 / 1_000_000  # $2.50 per 1 M output tokens


def _banner(text: str) -> None:
    print()
    print("=" * 60)
    print(text)
    print("=" * 60)


# ── dry-run helpers ───────────────────────────────────────────────────────────


def _scan_excel(input_file: Path) -> dict:
    """Return a summary dict without making any API calls."""
    df = pd.read_excel(input_file)

    website_col = next(
        (c for c in df.columns if c.strip().lower() == "found_url"),
        None,
    )
    status_col = next(
        (c for c in df.columns if c.strip().lower() == "address_status"),
        None,
    )

    total = len(df)
    has_website = 0
    no_website = 0
    already_processed = 0
    to_scrape = 0

    for idx in df.index:
        has_url = (
            website_col is not None
            and not pd.isna(df.at[idx, website_col])
            and str(df.at[idx, website_col]).strip()
        )
        if not has_url:
            no_website += 1
            continue
        has_website += 1

        already_done = (
            status_col is not None
            and not pd.isna(df.at[idx, status_col])
            and str(df.at[idx, status_col]).strip()
        )
        if already_done:
            already_processed += 1
        else:
            to_scrape += 1

    return {
        "total": total,
        "has_website": has_website,
        "no_website": no_website,
        "already_processed": already_processed,
        "to_scrape": to_scrape,
    }


def dry_run(input_file: Path) -> None:
    _banner("Dry run — no API calls will be made")
    info = _scan_excel(input_file)
    print(f"  Input file    : {input_file}")
    print(f"  Total rows    : {info['total']}")
    print(f"  Has website   : {info['has_website']}")
    print(f"    → to scrape : {info['to_scrape']}")
    print(f"    → already done: {info['already_processed']}")
    print(f"  No website    : {info['no_website']}")
    print()


# ── resume helper ─────────────────────────────────────────────────────────────


def _resume_indices(input_file: Path) -> set[str]:
    """Return row indices (as strings) already completed *for this job*.

    Resume must skip a row only when THIS job has already produced an
    ``address_status`` for it — never because the uploaded input file happens to
    carry an ``address_status`` column (e.g. a re-uploaded workbook from a
    previous job). We therefore read the current job's own validated output
    workbook, not the input file.

    A fresh job has no output workbook yet, so this returns an empty set and
    every website row is scraped/extracted from scratch. A job interrupted
    mid-run also has no completed output workbook, so it falls back to the JSONL
    checkpoints (scraped_content.jsonl / extracted_addresses.jsonl), which drive
    per-row resume internally in phases 1 and 2.
    """
    input_file = Path(input_file)
    validated_name = f"{input_file.stem}_validated.xlsx"
    candidates = [
        input_file.parent / validated_name,            # written next to the input
        input_file.parent / "output" / validated_name,  # moved here on completion
    ]
    output_file = next((p for p in candidates if p.exists()), None)
    if output_file is None:
        return set()

    df = pd.read_excel(output_file)
    status_col = next(
        (c for c in df.columns if c.strip().lower() == "address_status"),
        None,
    )
    if status_col is None:
        return set()
    return {
        str(idx)
        for idx in df.index
        if not pd.isna(df.at[idx, status_col]) and str(df.at[idx, status_col]).strip()
    }


# ── summary ───────────────────────────────────────────────────────────────────


def _print_summary(stats: dict[str, int], token_usage: dict[str, int]) -> None:
    total = stats.get("match", 0) + stats.get("updated", 0) + stats.get("skipped", 0)
    cost = (
        token_usage["input_tokens"] * _INPUT_COST_PER_TOKEN
        + token_usage["output_tokens"] * _OUTPUT_COST_PER_TOKEN
    )
    print()
    print("─" * 40)
    print("Summary")
    print("─" * 40)
    print(f"  Processed : {total}")
    print(f"  Matched   : {stats.get('match', 0)}")
    print(f"  Updated   : {stats.get('updated', 0)}")
    print(f"  Skipped   : {stats.get('skipped', 0)}")
    print(f"  No website: {stats.get('no_website', 0)}")
    print(f"  Tokens    : {token_usage['input_tokens']:,} in / {token_usage['output_tokens']:,} out")
    print(f"  Est. cost : ${cost:.4f}")
    print("─" * 40)


# ── pipeline ──────────────────────────────────────────────────────────────────


def run(
    input_file: str | Path,
    skip_scrape: bool = False,
    resume: bool = True,
    progress_cb=None,
) -> dict:
    input_file = Path(input_file)

    # Per-phase progress wrappers. The phase identity is encoded in the label
    # (as "Phase N — Name|detail") so a single caller-supplied progress_cb can
    # distinguish phases. Callback signature: progress_cb(current, total, label).
    scrape_cb = None
    extract_cb = None
    if progress_cb is not None:
        def scrape_cb(current, total, label):
            progress_cb(current, total, f"Phase 1 — Scraping|{label}")

        def extract_cb(current, total, label):
            progress_cb(current, total, f"Phase 2 — Extracting|{label}")

    skip_indices: set[str] = set()
    if resume:
        skip_indices = _resume_indices(input_file)
        if skip_indices:
            print(f"Resume: skipping {len(skip_indices)} already-processed rows.")

    # ── Phase 1 ───────────────────────────────────────────────────────────
    if not skip_scrape:
        _banner("Phase 1: Scraping websites")
        scrape_websites.run(input_file, skip_indices=skip_indices or None, progress_cb=scrape_cb)
    else:
        print("Phase 1 skipped — using existing scraped_content.jsonl")

    # Total pipeline token usage across all phases. Only Phase 2 is API-token
    # tracked in our code; Phase 1's ScrapeGraphAI tokens are not surfaced, and
    # Phases 3/4 make no API calls — so its Phase 2 contribution is added below.
    total_token_usage = {"input_tokens": 0, "output_tokens": 0}

    # ── Phase 2 ───────────────────────────────────────────────────────────
    _banner("Phase 2: Extracting addresses (Anthropic Messages API)")
    extracted, phase2_token_usage, per_row_costs = extract_addresses.run(
        skip_indices=skip_indices or None,
        progress_cb=extract_cb,
    )
    total_token_usage["input_tokens"] += phase2_token_usage["input_tokens"]
    total_token_usage["output_tokens"] += phase2_token_usage["output_tokens"]

    # ── Phase 3 ───────────────────────────────────────────────────────────
    _banner("Phase 3: UK filtering")
    uk_addresses, skipped_reasons = uk_filter.run(extracted)

    # ── Phase 4 ───────────────────────────────────────────────────────────
    _banner("Phase 4: Comparing and updating Excel")
    output_path, stats = compare_addresses.run(
        input_file,
        extracted=extracted,
        uk_addresses=uk_addresses,
        skipped_reasons=skipped_reasons,
        skip_indices=skip_indices or None,
    )

    print()
    print(f"Pipeline complete → {output_path}")
    _print_summary(stats, total_token_usage)

    return {
        "stats": stats,
        "output_path": output_path,
        "total_token_usage": total_token_usage,
        "phase2_token_usage": phase2_token_usage,
        "per_row_costs": per_row_costs,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Address validation pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input_file", help="Path to input Excel file (.xlsx)")
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip Phase 1 and use an existing scraped_content.jsonl",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan the Excel file and print a summary; make no API calls",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip rows that already have a non-empty address_status value",
    )
    args = parser.parse_args()

    input_path = Path(args.input_file)
    if not input_path.exists():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        dry_run(input_path)
        sys.exit(0)

    run(input_path, skip_scrape=args.skip_scrape, resume=args.resume)
