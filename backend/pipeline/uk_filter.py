"""
Phase 3 — UK address filtering.

Applies two sequential layers to each extracted address:
    Layer A: explicit non-UK country string → skip "non-UK country"
    Layer B: postcode regex for UK format → skip "non-UK postcode"

A row proceeds only if:
    • Layer A passes (country present but not in the non-UK list), OR
    • Layer A is inconclusive (country null/empty)
  AND Layer B passes (postcode matches UK pattern) or is inconclusive (null).
"""

import json
import re
from pathlib import Path

EXTRACTED_JSON = Path("extracted_addresses.jsonl")
UK_ADDRESSES_JSON = Path("uk_addresses.json")
SKIPPED_JSON = Path("skipped_addresses.json")

# Substrings that identify a clearly non-UK country (case-insensitive match)
_NON_UK = frozenset(
    [
        "australia",
        "united states",
        "usa",
        "canada",
        "india",
        "new zealand",
    ]
)

# Official UK postcode pattern — rejects bare numeric codes like 2000 / 90210
_UK_POSTCODE_RE = re.compile(
    r"^[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}$",
    re.IGNORECASE,
)

# ── jsonl checkpoint helpers ────────────────────────────────────────────────────


def _read_jsonl(path: Path) -> dict:
    result = {}
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                result[entry["key"]] = entry["value"]
            except (json.JSONDecodeError, KeyError):
                continue  # skip corrupted line — crash safety
    return result


# ── layer helpers ─────────────────────────────────────────────────────────────


def _layer_a(country: str | None) -> str | None:
    """Return skip reason if country is clearly non-UK, else None (pass/inconclusive)."""
    if not country or not country.strip():
        return None  # inconclusive
    low = country.strip().lower()
    if any(nuk in low for nuk in _NON_UK):
        return "non-UK country"
    return None


def _layer_b(postcode: str | None) -> str | None:
    """Return skip reason if postcode fails UK format, else None (pass/inconclusive)."""
    if not postcode or not postcode.strip():
        return None  # inconclusive — benefit of the doubt
    if not _UK_POSTCODE_RE.match(postcode.strip()):
        return "non-UK postcode"
    return None


def classify(address: dict) -> str | None:
    """
    Return a skip reason string, or None if the address passes both filter layers.
    Layer A is evaluated first; Layer B is only evaluated when Layer A doesn't skip.
    """
    reason = _layer_a(address.get("country"))
    if reason:
        return reason
    return _layer_b(address.get("postcode"))


# ── main ──────────────────────────────────────────────────────────────────────


def run(
    extracted: dict[str, dict] | None = None,
) -> tuple[dict[str, dict], dict[str, str]]:
    """
    Filter *extracted* to UK-only addresses.

    Returns:
        uk_addresses — {row_idx: address_dict} for rows that passed both layers
        skipped — {row_idx: reason_string} for rows that were filtered out
    """
    if extracted is None:
        extracted = _read_jsonl(EXTRACTED_JSON)

    uk_addresses: dict[str, dict] = {}
    skipped: dict[str, str] = {}

    for row_idx, address in extracted.items():
        reason = classify(address)
        if reason:
            skipped[row_idx] = reason
        else:
            uk_addresses[row_idx] = address

    UK_ADDRESSES_JSON.write_text(
        json.dumps(uk_addresses, indent=2, ensure_ascii=False)
    )
    SKIPPED_JSON.write_text(json.dumps(skipped, indent=2, ensure_ascii=False))

    by_country = sum(1 for r in skipped.values() if "country" in r)
    by_postcode = sum(1 for r in skipped.values() if "postcode" in r)
    print(
        f"UK filter: {len(uk_addresses)} passed, "
        f"{len(skipped)} skipped "
        f"({by_country} by country, {by_postcode} by postcode)"
    )

    return uk_addresses, skipped


if __name__ == "__main__":
    run()
