"""
Phase 1 — Website scraping via ScrapeGraphAI SmartScraperGraph.

Reads an Excel file, scrapes each row that has a non-empty website column,
and writes the extracted text to scraped_content.jsonl keyed by row index.

Retry logic: headless=True first; if the result is null/empty, one retry
with headless=False to handle JS-heavy SPAs.
"""

import datetime
import json
import logging
import os
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from scrapegraphai.graphs import SmartScraperGraph

load_dotenv()

SCRAPED_JSON = Path("scraped_content.jsonl")
SCRAPED_SOURCES_JSON = Path("scraped_sources.json")
ERROR_LOG = Path("scrape_errors.log")
URL_CACHE_PATH = Path("outputs/pipeline/url_scrape_cache.json")

SCRAPE_PROMPT = (
    "Extract all text content from this page including any address, "
    "contact details, registered office, and company location information. "
    "Return the complete text."
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
    filename=str(ERROR_LOG),
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── helpers ───────────────────────────────────────────────────────────────────


def _graph_config(headless: bool) -> dict:
    return {
        "llm": {
            "api_key": os.getenv("ANTHROPIC_API_KEY"),
            "model": "anthropic/claude-haiku-4-5-20251001",
            "max_tokens": 8192,
        },
        "headless": headless,
        "verbose": False,
    }


def _normalise_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _result_to_text(result) -> str:
    """Convert SmartScraperGraph result (dict or str) to a plain string."""
    if not result:
        return ""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def _is_empty_result(raw) -> bool:
    """True when the scraper returned nothing useful — triggers the headless retry."""
    if raw is None:
        return True
    if isinstance(raw, str):
        return not raw.strip()
    if isinstance(raw, dict):
        # dict with all-null values means the page didn't render (JS SPA)
        return not raw or all(v is None for v in raw.values())
    return False


CONTACT_SUFFIXES = ["/contact", "/contact-us", "/about", "/about-us"]

import re
from urllib.parse import urlparse

UK_POSTCODE_RE = re.compile(r'[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}', re.IGNORECASE)


def _base_domain(url: str) -> str:
    """Return scheme://host (lowercase) for URL-based cache matching."""
    url = _normalise_url(url)
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}".lower().rstrip("/")


def _load_url_cache() -> dict:
    """Load the persistent cross-job URL scrape cache from disk."""
    if URL_CACHE_PATH.exists():
        try:
            return json.loads(URL_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_url_cache(cache: dict) -> None:
    """Persist the URL cache to disk atomically."""
    URL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    URL_CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


def _has_address_content(text: str) -> bool:
    if not text:
        return False
    # Strongest signal: actual UK postcode present
    if UK_POSTCODE_RE.search(text):
        return True
    # Fallback: address keywords
    keywords = ["street", "road", "avenue", "lane", "close", "house",
                "court", "place", "england", "scotland", "wales",
                "london", "manchester", "birmingham", "edinburgh"]
    return any(kw in text.lower() for kw in keywords)


def scrape_url(url: str, row_idx: int) -> tuple[str, str] | None:
    """Return (content, source_url) for the first page with address content, or None."""
    base = url.rstrip("/")
    urls_to_try = [url] + [base + suffix for suffix in CONTACT_SUFFIXES]

    for attempt_url in urls_to_try:
        result = None
        for headless in (True, False):
            try:
                graph = SmartScraperGraph(
                    prompt=SCRAPE_PROMPT,
                    source=attempt_url,
                    config=_graph_config(headless),
                )
                raw = graph.run()
                if _is_empty_result(raw):
                    continue  # try headless=False
                text = _result_to_text(raw)
                if _has_address_content(text):
                    result = text
                    break  # found address, exit headless loop
                break  # rendered fine but no address, skip to next URL
            except Exception as exc:
                log.error("row=%d url=%s headless=%s error=%s",
                          row_idx, attempt_url, headless, exc)
                break  # error on this URL, try next

        if result:
            return result, attempt_url

    log.error("row=%d url=%s FAILED — no address found on homepage or contact pages",
              row_idx, url)
    return None


# ── main ──────────────────────────────────────────────────────────────────────


def run(
    input_file: str | Path,
    skip_indices: set[str] | None = None,
    progress_cb=None,
    cache_info_cb=None,
    revalidate: bool = False,
) -> dict[str, str]:
    df = pd.read_excel(input_file)

    # accept "website" or "Website"
    website_col = next(
        (c for c in df.columns if c.strip().lower() == "website"),
        None,
    )
    if website_col is None:
        raise ValueError("No 'website' column found in the Excel file.")

    # Best-effort company name column for progress reporting
    name_col = None
    for candidate in ("business name", "company", "name"):
        name_col = next(
            (c for c in df.columns if c.strip().lower() == candidate),
            None,
        )
        if name_col:
            break

    # Load any previously scraped rows so an interrupted run can resume
    results: dict[str, str] = _read_jsonl(SCRAPED_JSON)
    if results:
        print(f"Loaded {len(results)} existing rows from {SCRAPED_JSON}")

    if SCRAPED_SOURCES_JSON.exists():
        sources: dict[str, str] = json.loads(SCRAPED_SOURCES_JSON.read_text())
    else:
        sources = {}

    # Persistent cross-job URL cache — never cleared between jobs
    url_cache = _load_url_cache() if not revalidate else {}

    # Build URL-based reverse lookup from this run's files: base_domain → (content, source_url)
    # Allows reusing content when the same URL appears at a different row index
    url_to_cached: dict[str, tuple[str, str]] = {}
    for cached_idx, source_url in sources.items():
        if cached_idx in results:
            domain = _base_domain(source_url)
            if domain not in url_to_cached:
                url_to_cached[domain] = (results[cached_idx], source_url)

    # Classify each row: already-cached (index, same-run URL, or cross-job URL), or needs scraping
    pre_cache_size = len(results)
    cache_hits = 0
    rows_to_process = []
    for idx, row in df.iterrows():
        row_key = str(idx)
        raw_url = row[website_col]
        if not raw_url or pd.isna(raw_url):
            continue

        # Index-based cache hit (interrupted run resuming)
        if row_key in results or (skip_indices and row_key in skip_indices):
            cache_hits += 1
            continue

        domain = _base_domain(str(raw_url))

        # Same-run URL cache hit (same website, different row index)
        if domain in url_to_cached:
            content, src_url = url_to_cached[domain]
            results[row_key] = content
            sources[row_key] = src_url
            _append_jsonl(SCRAPED_JSON, row_key, content)
            cache_hits += 1
            continue

        # Persistent cross-job URL cache hit
        if domain in url_cache:
            entry = url_cache[domain]
            results[row_key] = entry["content"]
            sources[row_key] = entry["source_url"]
            _append_jsonl(SCRAPED_JSON, row_key, entry["content"])
            cache_hits += 1
            continue

        rows_to_process.append((idx, row, str(raw_url)))

    # Persist any source entries that were filled from cache but not yet on disk
    # (scraped_content.jsonl rows were appended inline as they were filled)
    if len(results) > pre_cache_size:
        SCRAPED_SOURCES_JSON.write_text(json.dumps(sources, indent=2, ensure_ascii=False))

    total_rows = len(rows_to_process)

    if cache_info_cb:
        try:
            cache_info_cb(cache_hits, total_rows)
        except Exception:
            pass

    for current_num, (idx, row, raw_url) in enumerate(rows_to_process, 1):
        url = _normalise_url(raw_url)

        company_name = url
        if name_col:
            val = row[name_col]
            if val and not pd.isna(val):
                company_name = str(val)

        if progress_cb:
            try:
                progress_cb(current_num, total_rows, company_name)
            except Exception:
                pass

        print(f"[{idx}] Scraping {url} …", flush=True)

        scrape_result = scrape_url(url, int(idx))
        if scrape_result:
            content, source_url = scrape_result
            results[str(idx)] = content
            sources[str(idx)] = source_url
            # Write to persistent cross-job cache (never overwritten unless revalidate=True)
            domain = _base_domain(url)
            if revalidate or domain not in url_cache:
                url_cache[domain] = {
                    "scraped_at": datetime.datetime.utcnow().isoformat(),
                    "source_url": source_url,
                    "content": content,
                    "address_extracted": None,
                    "is_uk": None,
                }
                _save_url_cache(url_cache)
            _append_jsonl(SCRAPED_JSON, str(idx), content)
            SCRAPED_SOURCES_JSON.write_text(json.dumps(sources, indent=2, ensure_ascii=False))
        else:
            log.error("row=%d url=%s no content returned after retries", idx, url)
            print(f"  ✗ failed (see {ERROR_LOG})")

        time.sleep(1)

    print(f"\nSaved {len(results)} pages → {SCRAPED_JSON}")
    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(f"Usage: python {Path(__file__).name} <input.xlsx>")
        sys.exit(1)
    run(sys.argv[1])
