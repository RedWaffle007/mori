#!/usr/bin/env python3
"""
low_repromoter.py
──────────────────────
Second-pass evaluator for the LOW confidence rows from url_finder_new.py.

WHAT IT DOES:
- Reads the output Excel from url_finder_new.py (both sheets)
- For each row in the *_no_url sheet with url_confidence == "LOW":
    * Re-runs URL validation with an IMPROVED tokenizer
    * Tokenizer adds: alphanumeric boundary splitting, initials-as-substring
      matching, full-name substring detection, expanded security WEAK_WORDS
- Rows that now pass (HIGH or MEDIUM) are moved into the with_url sheet
  with an added "promoted" column flag = "yes"
- Existing with_url rows get "promoted" = "" (blank)
- DISCARD rows are untouched
- No API calls, no credits used

USAGE:
    python low_promoter.py url_finder_results.xlsx

OUTPUT:
    80200_sgai_results_promoted.xlsx
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

# ── BLACKLIST + REJECTED TLDS (copied verbatim from url_finder_new.py) ──────
# We re-validate against these in case the original blacklist has grown since.

import json

BLACKLIST_FILE = "blacklist.json"

SEED_BLACKLIST = [
    "linkedin.com", "glassdoor.com", "reed.co.uk", "totaljobs.com",
    "indeed.com", "cv-library.co.uk", "yell.com", "thomsonlocal.com",
    "checkatrade.com", "trustatrader.com", "hotfrog.co.uk", "cylex.co.uk",
    "192.com", "find-us-here.com", "bizify.co.uk", "freeindex.co.uk",
    "serchen.co.uk", "businessnetwork.co.uk",
    "companieshouse.gov.uk",
    "find-and-update.company-information.service.gov.uk",
    "endole.co.uk", "opencorporates.com", "duedil.com", "credencedata.com",
    "firstreport.co.uk", "screener.in", "ukcompanies.lursoft.lv", "tinytax.co.uk",
    "finance.yahoo.com", "morningstar.com", "bloomberg.com", "reuters.com",
    "rocketreach.co", "lusha.com", "zoominfo.com", "apollo.io",
    "hunter.io", "clearbit.com", "signalhire.com",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "spotify.com", "crunchbase.com", "wikipedia.org", "wikidata.org",
    "figma.com", "securityscorecard.com",
    "trustpilot.com", "reviews.io", "g2.com", "capterra.com", "getapp.com",
]

REJECTED_COUNTRY_TLDS = [
    ".com.au", ".co.au", ".net.au", ".co.us", ".us", ".co.ca", ".ca",
    ".co.nz", ".net.nz", ".co.za", ".co.in", ".net.in",
    ".de", ".fr", ".es", ".it", ".nl", ".be", ".pl", ".se",
    ".no", ".dk", ".fi", ".pt", ".ru", ".cn", ".jp", ".kr",
    ".br", ".mx", ".ar", ".ae", ".sa", ".com.ua",
]

STRIP_WORDS = {
    "ltd", "limited", "llp", "plc", "inc", "corp", "co",
    "uk", "gb", "england", "scotland", "wales",
    "the", "and", "of", "for",
}

# ── EXPANDED WEAK_WORDS — adds security industry vocabulary ─────────────────
# Anything in this set, even if it appears in the domain, is NOT enough on its
# own to be a strong match. Catches "logicfireandsecurity.com" for KIRA SECURITY.
WEAK_WORDS = {
    # Original generic words
    "services", "solutions", "technologies", "technology",
    "consulting", "consultancy", "cyber", "security", "group", "holdings",
    "systems", "global", "international", "digital",
    # Security industry additions (from SIC 80200 analysis)
    "fire", "alarm", "alarms", "guard", "guards", "guarding",
    "monitoring", "protection", "protect", "protective",
    "safes", "safe", "safety", "surveillance", "patrol",
    "locksmith", "locksmiths", "locks", "lock",
    "cctv", "fm", "installations", "installation", "electrical",
    "electronics", "electronic", "service", "maintenance",
    "network", "networks", "tech", "solution",
}

def load_blacklist() -> set[str]:
    bl = set(SEED_BLACKLIST)
    if Path(BLACKLIST_FILE).exists():
        with open(BLACKLIST_FILE) as f:
            extra = json.load(f)
            bl.update(extra.get("domains", []))
    return bl

# ── URL HELPERS (copied verbatim) ────────────────────────────────────────────

def extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url if url.startswith("http") else "https://" + url)
        return parsed.netloc.lower().lstrip("www.")
    except Exception:
        return ""

def extract_path_depth(url: str) -> int:
    try:
        parsed = urlparse(url if url.startswith("http") else "https://" + url)
        return len([s for s in parsed.path.split("/") if s])
    except Exception:
        return 0

def is_rejected_tld(domain: str) -> bool:
    return any(domain.endswith(tld) for tld in REJECTED_COUNTRY_TLDS)

# ── IMPROVED TOKENIZER ──────────────────────────────────────────────────────

def split_alphanumeric(text: str) -> str:
    """Insert spaces at digit↔letter boundaries.

    Examples:
        '121SECURITYUK' → '121 SECURITYUK' (then standard tokenization)
        'A1SECURITY'    → 'A1 SECURITY'
        'K2PROJECTS'    → 'K2 PROJECTS'
    """
    # letter followed by digit, or digit followed by letter
    text = re.sub(r'([A-Za-z])(\d)', r'\1 \2', text)
    text = re.sub(r'(\d)([A-Za-z])', r'\1 \2', text)
    return text

def extract_initials(business_name: str) -> str | None:
    """Detect 2+ consecutive single-letter tokens and return them concatenated.

    Catches initials patterns like:
        'A E L SYSTEMS LTD' → 'ael'
        'D S C ALARMS LTD'  → 'dsc'
        'T L S SECURITY'    → 'tls'
        'A.M.R SERVICES'    → 'amr'
        'J K E LIMITED'     → 'jke'

    Returns None if no consecutive initials found.
    """
    # First, normalise dot-separated initials: "A.M.R" → "A M R"
    normalised = re.sub(r'([A-Za-z])\.([A-Za-z])', r'\1 \2', business_name)
    # Strip remaining punctuation except spaces
    normalised = re.sub(r'[^A-Za-z0-9\s]', ' ', normalised)
    tokens = normalised.split()

    # Find longest run of consecutive single-letter tokens
    best_run = []
    current_run = []
    for t in tokens:
        if len(t) == 1 and t.isalpha():
            current_run.append(t.lower())
        else:
            if len(current_run) > len(best_run):
                best_run = current_run
            current_run = []
    if len(current_run) > len(best_run):
        best_run = current_run

    if len(best_run) >= 2:
        return "".join(best_run)
    return None

def name_tokens(business_name: str) -> list[str]:
    """Extract meaningful tokens with the IMPROVED tokenizer.

    Changes from original:
        1. Splits on alphanumeric boundaries before tokenizing
        2. Standard cleanup is unchanged
    """
    business_name = html.unescape(business_name)
    # NEW: split alphanumeric boundaries first
    business_name = split_alphanumeric(business_name)
    # Standard cleanup
    raw = re.sub(r"[^a-zA-Z0-9\s]", " ", business_name.lower())
    tokens = raw.split()
    return [t for t in tokens if t not in STRIP_WORDS and len(t) > 2]

def full_name_compact(business_name: str) -> str:
    """Return the company name with all spaces and punctuation removed, lowercased.

    Used to check if the entire name appears in the domain.
        'WELOHOME LTD'      → 'welohomeltd'
        'LSFIRE & SECURITY' → 'lsfiresecurity'
    """
    name = html.unescape(business_name).lower()
    name = re.sub(r'\b(ltd|limited|llp|plc|inc|corp)\b', '', name)
    return re.sub(r'[^a-z0-9]', '', name)

def token_match_score(tokens: list[str], domain: str) -> tuple[int, int]:
    domain_clean = re.sub(r"[^a-z0-9]", "", domain.lower())
    strong = sum(1 for t in tokens if t not in WEAK_WORDS and t in domain_clean)
    weak = sum(1 for t in tokens if t in WEAK_WORDS and t in domain_clean)
    return strong, weak

# ── IMPROVED VALIDATION ─────────────────────────────────────────────────────

def validate_url_v2(
    business_name: str,
    url: str,
    blacklist: set[str],
) -> tuple[str, str]:
    """Returns (confidence, reason) using the improved tokenizer."""
    if not url or not url.strip():
        return "DISCARD", "no url returned"

    domain = extract_domain(url)
    if not domain:
        return "DISCARD", "unparseable url"

    # 1. Blacklist
    for bl in blacklist:
        if domain == bl or domain.endswith("." + bl):
            return "DISCARD", f"blacklisted: {bl}"

    # 2. Rejected TLDs
    if is_rejected_tld(domain):
        return "DISCARD", f"non-UK country TLD: {domain}"

    # 3. Deep subpage
    depth = extract_path_depth(url)
    if depth >= 3:
        return "DISCARD", f"deep subpage (depth={depth}), likely directory"

    domain_compact = re.sub(r'[^a-z0-9]', '', domain.lower())

    # 4. NEW: full compact name match → HIGH (requires 6+ chars to avoid coincidences)
    name_compact = full_name_compact(business_name)
    if name_compact and len(name_compact) >= 6 and name_compact in domain_compact:
        return "HIGH", f"full company name '{name_compact}' present in domain"

    # 5. NEW: initials match
    initials = extract_initials(business_name)
    if initials and initials in domain_compact:
        if len(initials) >= 3:
            return "HIGH", f"initials '{initials}' present in domain"
        else:
            # 2-char initials are too short to be reliable on their own
            # but still better than no match — promote to MEDIUM
            return "MEDIUM", f"short initials '{initials}' present in domain (verify)"

    # 6. Standard token matching with IMPROVED tokenizer
    tokens = name_tokens(business_name)
    if len(tokens) == 0:
        return "LOW", "no meaningful name tokens to match"

    strong, weak = token_match_score(tokens, domain)

    if strong >= 1:
        return "HIGH", f"strong token match ({strong} distinctive word(s) in domain)"
    elif weak >= 2:
        return "MEDIUM", f"multiple weak token match ({weak} generic words in domain)"
    elif weak == 1:
        return "LOW", "single weak token match"
    else:
        return "LOW", f"no token match (0/{len(tokens)})"

# ── MAIN ─────────────────────────────────────────────────────────────────────

def run(input_path: str) -> None:
    input_path = Path(input_path)
    print(f" Loading {input_path}...")

    xl = pd.ExcelFile(input_path)
    print(f" Sheets: {xl.sheet_names}")

    # Auto-detect sheet names
    with_sheet = next((s for s in xl.sheet_names if "with_url" in s), None)
    no_sheet = next((s for s in xl.sheet_names if "no_url" in s), None)
    if not with_sheet or not no_sheet:
        print(" Could not find _with_url and _no_url sheets")
        sys.exit(1)

    df_with = pd.read_excel(xl, sheet_name=with_sheet, dtype=str).fillna("")
    df_no = pd.read_excel(xl, sheet_name=no_sheet, dtype=str).fillna("")
    print(f" {len(df_with):,} rows in {with_sheet}")
    print(f" {len(df_no):,} rows in {no_sheet}\n")

    blacklist = load_blacklist()
    print(f" {len(blacklist)} domains in blacklist\n")

    # Add the promoted column
    df_with["promoted"] = ""
    df_no["promoted"] = ""

    # Re-evaluate only LOW rows
    low_mask = df_no["url_confidence"] == "LOW"
    print(f" Re-evaluating {low_mask.sum():,} LOW rows...")

    promoted_count = 0
    promoted_breakdown = {"HIGH": 0, "MEDIUM": 0}

    for idx in df_no[low_mask].index:
        name = df_no.at[idx, "Business Name"]
        url = df_no.at[idx, "found_url"]
        if not name or not url:
            continue

        new_conf, new_reason = validate_url_v2(name, url, blacklist)

        if new_conf in ("HIGH", "MEDIUM"):
            df_no.at[idx, "url_confidence"] = new_conf
            df_no.at[idx, "url_reason"] = new_reason
            df_no.at[idx, "promoted"] = "yes"
            promoted_count += 1
            promoted_breakdown[new_conf] += 1

    print(f" Promoted: {promoted_count:,} ({promoted_breakdown['HIGH']:,} HIGH + {promoted_breakdown['MEDIUM']:,} MEDIUM)\n")

    # Split: move promoted rows from df_no to df_with
    promoted_mask = df_no["promoted"] == "yes"
    df_promoted = df_no[promoted_mask].copy()
    df_no_remaining = df_no[~promoted_mask].copy()
    df_with_combined = pd.concat([df_with, df_promoted], ignore_index=True)

    # Write output
    output_path = input_path.parent / f"{input_path.stem}_promoted.xlsx"
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df_with_combined.to_excel(writer, sheet_name=with_sheet, index=False)
        df_no_remaining.to_excel(writer, sheet_name=no_sheet, index=False)

        # Uniform row heights (same as the main script)
        from openpyxl.styles import Alignment
        for ws in writer.sheets.values():
            for row in ws.iter_rows():
                ws.row_dimensions[row[0].row].height = 15
                for cell in row:
                    if cell.alignment and cell.alignment.wrap_text:
                        cell.alignment = Alignment(wrap_text=False)

    print(f" Final counts:")
    print(f"   {with_sheet}: {len(df_with_combined):,} (+{promoted_count:,} from promotion)")
    print(f"   {no_sheet}: {len(df_no_remaining):,}")
    print(f"\n Output → {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-evaluate LOW confidence rows with improved tokenizer (no API calls)"
    )
    parser.add_argument("input", help="Path to the *80200_sgai_results.xlsx file")
    args = parser.parse_args()
    run(args.input)
