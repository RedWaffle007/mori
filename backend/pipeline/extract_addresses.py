"""
Phase 2 — Address extraction via synchronous Anthropic Messages API.

Loads scraped_content.jsonl, processes each row one at a time, and appends to
extracted_addresses.jsonl after every row (same pattern as scrape_websites.py).
"""

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import anthropic
from dotenv import load_dotenv

load_dotenv()

SCRAPED_JSON = Path("scraped_content.jsonl")
SCRAPED_SOURCES_JSON = Path("scraped_sources.json")
OUTPUT_JSON = Path("extracted_addresses.jsonl")
URL_CACHE_PATH = Path("outputs/pipeline/url_scrape_cache.json")

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 300

# Haiku 4.5 batch pricing (used for per-row cost tracking)
_INPUT_COST_PER_TOKEN = 0.50 / 1_000_000   # $0.50 per 1 M input tokens
_OUTPUT_COST_PER_TOKEN = 2.50 / 1_000_000  # $2.50 per 1 M output tokens

SYSTEM_PROMPT = (
    "You are an address extractor. Return ONLY valid JSON with keys: "
    "add1, add2, town, county, postcode, country. "
    "If a field is not present, return null. "
    "Extract only the primary/registered business address."
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


def _append_jsonl(path: Path, key: str, value) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, "value": value}, ensure_ascii=False) + "\n")


# ── logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── url cache helpers ─────────────────────────────────────────────────────────


def _base_domain(url: str) -> str:
    """Return scheme://host (lowercase) — matches the key used by scrape_websites."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}".lower().rstrip("/")


def _load_url_cache() -> dict:
    if URL_CACHE_PATH.exists():
        try:
            return json.loads(URL_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_url_cache(cache: dict) -> None:
    URL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    URL_CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


# ── main ──────────────────────────────────────────────────────────────────────


def run(
    scraped_file: str | Path = SCRAPED_JSON,
    skip_indices: set[str] | None = None,
    progress_cb=None,
    job_id=None,
) -> tuple[dict[str, dict], dict[str, int], dict[str, dict]]:
    """
    Extract addresses from scraped content via synchronous Anthropic API calls.

    Processes one row at a time and appends to OUTPUT_JSON after every row so
    an interrupted run can resume from the last unprocessed row.

    Returns
    -------
    (results, token_usage, per_row_costs)
        results — {row_idx: address_dict}
        token_usage — {"input_tokens": N, "output_tokens": N}
        per_row_costs — {row_idx: {"input_tokens": N, "output_tokens": N, "cost": float}}
                        one entry per row that triggered an API call this run
    """
    client = anthropic.Anthropic()

    scraped: dict[str, str] = _read_jsonl(Path(scraped_file))

    if skip_indices:
        scraped = {k: v for k, v in scraped.items() if k not in skip_indices}

    if not scraped:
        log.warning("No scraped content to process.")
        return {}, {"input_tokens": 0, "output_tokens": 0}, {}

    # Load existing results — rows already present in the per-run file are served from cache
    existing: dict[str, dict] = _read_jsonl(OUTPUT_JSON)

    # Load scraped source URLs so we can map row → domain for the persistent cache
    sources: dict[str, str] = {}
    if SCRAPED_SOURCES_JSON.exists():
        try:
            sources = json.loads(SCRAPED_SOURCES_JSON.read_text())
        except Exception:
            sources = {}

    url_cache = _load_url_cache()

    # Check the persistent url_cache for rows not yet in the per-run extracted file
    url_cache_hits = 0
    for row_idx in list(scraped.keys()):
        if row_idx in existing:
            continue
        src_url = sources.get(row_idx, "")
        if not src_url:
            continue
        domain = _base_domain(src_url)
        entry = url_cache.get(domain)
        if entry and entry.get("address_extracted") is not None:
            existing[row_idx] = entry["address_extracted"]
            _append_jsonl(OUTPUT_JSON, row_idx, entry["address_extracted"])
            url_cache_hits += 1

    if url_cache_hits:
        log.info("Served %d rows from persistent url_cache (no API call)", url_cache_hits)

    to_extract = {k: v for k, v in scraped.items() if k not in existing}

    if not to_extract:
        log.info("All %d rows already resolved — skipping API", len(scraped))
        final = {k: existing[k] for k in scraped if k in existing}
        return final, {"input_tokens": 0, "output_tokens": 0}, {}

    log.info(
        "Extracting %d new rows via Messages API (%d served from cache)",
        len(to_extract), len(scraped) - len(to_extract),
    )

    merged = dict(existing)
    token_usage = {"input_tokens": 0, "output_tokens": 0}
    per_row_costs: dict[str, dict] = {}
    total = len(to_extract)
    rows = list(to_extract.items())

    for current_num, (row_idx, content) in enumerate(rows, 1):
        if progress_cb:
            try:
                progress_cb(current_num, total, f"row {row_idx}")
            except Exception:
                pass

        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )
            in_tok = msg.usage.input_tokens
            out_tok = msg.usage.output_tokens
            token_usage["input_tokens"] += in_tok
            token_usage["output_tokens"] += out_tok

            # Record this row's individual token usage and cost
            per_row_costs[row_idx] = {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost": in_tok * _INPUT_COST_PER_TOKEN + out_tok * _OUTPUT_COST_PER_TOKEN,
            }

            text = next((b.text for b in msg.content if b.type == "text"), "")
            try:
                clean = re.sub(r'^```json\s*|\s*```$', '', text.strip())
                addr = json.loads(clean)
            except json.JSONDecodeError:
                log.error("row=%s JSON parse failed: %s", row_idx, text[:200])
                addr = {"raw": text}

            merged[row_idx] = addr
            # Write after every row so an interrupted run can resume
            _append_jsonl(OUTPUT_JSON, row_idx, addr)

            # Write extracted address back into the persistent url_cache entry
            src_url = sources.get(row_idx, "")
            if src_url:
                domain = _base_domain(src_url)
                if domain in url_cache:
                    url_cache[domain]["address_extracted"] = addr
                    _save_url_cache(url_cache)

        except Exception as exc:
            log.error("row=%s API error: %s", row_idx, exc)

    log.info("Saved %d address records → %s", len(merged), OUTPUT_JSON)

    final = {k: merged[k] for k in scraped if k in merged}
    return final, token_usage, per_row_costs


if __name__ == "__main__":
    results, usage, per_row = run()
    print(f"input_tokens={usage['input_tokens']} output_tokens={usage['output_tokens']}")
