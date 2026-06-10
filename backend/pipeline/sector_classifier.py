"""
sector_classifier.py
─────────────────────
Classify all rows from a promoted URL workbook into one of six sectors using
enriched signals: Business Name, URL, page title, and sg_content from the
Step 1 JSONL checkpoint. Rows with empty sg_content fall back to
name + url + title (and name + url only if no checkpoint is available).

This is a web-app friendly fork of the original `reclassify_sectors.py`. The old
SIC-only comparison (Old_Sector / Reclassified columns and the reclassification
report) has been removed — every row is classified fresh.

Modes:
    python sector_classifier.py --test           # first 20 rows, live API, instant
    python sector_classifier.py --batch          # submit full job to Batch API (50% off)
    python sector_classifier.py --batch-results  # poll / download results + write xlsx

The three entry points (cmd_test, cmd_batch_submit, cmd_batch_results) are also
importable and callable directly from the FastAPI backend (main.py), which sets
the INPUT_XLSX / INPUT_SHEET / CHECKPOINT_JSONL / OUTPUT_XLSX module variables
before invoking them.

Power cut / resume: --batch is safe. The job runs on Anthropic's servers.
If your machine dies, just run --batch-results when you're back — batch_id.txt
has everything needed to resume.
"""

import argparse
import asyncio
import json
from pathlib import Path

import aiohttp
import pandas as pd
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent / ".env")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Config ────────────────────────────────────────────────────────────────────
# INPUT_XLSX / INPUT_SHEET / CHECKPOINT_JSONL / OUTPUT_XLSX are overrideable:
# main.py sets them as module attributes before calling any function.

INPUT_XLSX = "input_sgai_urls_promoted.xlsx"   # overrideable
INPUT_SHEET = "Sheet1_with_url"                 # overrideable
CHECKPOINT_JSONL = "sgai_url_checkpoint.json"   # overrideable
OUTPUT_XLSX = "sector_classified.xlsx"          # overrideable
BATCH_REQUESTS_FILE = "batch_requests.jsonl"
BATCH_ID_FILE = "batch_id.txt"
BATCH_IDX_MAP_FILE = "batch_idx_mapping.json"

CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 4096
BATCH_SIZE = 150
CONCURRENCY = 5
MAX_RETRIES = 4
BASE_RETRY_DELAY = 5
SG_CONTENT_CAP = 3000

SECTOR_DEFINITIONS = """
You are a company sector classifier. Classify each company into exactly one sector
using all available signals: business name, website URL, page title, and website content.
Website content may be absent for some rows — use the other signals in that case.

SECTOR 1: "Security"
Core keywords: cybersecurity, SOC, MDR, endpoint protection, IDS/IPS,
identity & access management, IAM, SSO, MFA, encryption, key management,
VPN, secure access, compliance, PCI DSS, HIPAA, ISO 27001, SOC 2, GDPR,
vulnerability management, penetration testing, risk assessment,
logging & SIEM, forensic analysis, threat intelligence
Services & Capabilities: managed security, MSSP, security operations,
SOC-as-a-service, MDR, managed detection and response, endpoint protection,
EDR, XDR, managed firewall, threat hunting, SIEM, log management,
security monitoring, 24/7 monitoring, incident response, cyber incident,
breach response, vulnerability management, penetration testing, pen testing,
identity management, IAM, privileged access, MFA, SSO, zero trust
Certifications & Compliance: ISO 27001, SOC 2, Cyber Essentials, CREST,
PCI DSS, security accredited, certified security provider

SECTOR 2: "MSP V"
Vertical signals (weight these highly):
- Financial Services: fintech IT, financial services IT, FCA compliant,
  PCI DSS, banking IT, wealth management IT, insurance IT
- Healthcare: healthcare IT, NHS IT, clinical systems, HIPAA, health data,
  medical IT, GP surgery IT, pharmacy IT
- Legal: legal IT, law firm IT, SRA compliant, legal sector technology,
  chambers IT
- Defence / Government: MOD supplier, government IT, public sector IT,
  SC cleared, DV cleared, Cyber Essentials Plus, G-Cloud, Crown Commercial
- Professional Services: accountancy IT, property IT, real estate technology,
  recruitment IT
Recurring Revenue & Contract Signals: managed services contract, MRR,
monthly recurring, SLA-backed, per-seat pricing, per-user,
subscription-based, retained IT, outsourced IT department, IT partner
Toolstack signals: ConnectWise, Datto, Kaseya, Autotask, Halo PSA,
NinjaRMM, SolarWinds, N-able, RMM, PSA, remote monitoring and management,
professional services automation, automated remediation, self-healing,
AIOps, automation-first, custom platform, proprietary tooling
Maturity signals: infrastructure as code, Terraform, Ansible, DevOps,
CI/CD, cloud-native, containerised, Kubernetes, API-first,
integration platform, middleware

SECTOR 3: "Integration"
Keywords: systems integration, IT consulting, enterprise architecture,
application migration, cloud migration, replatforming, lift-and-shift,
API integration, middleware, ESB, microservices, DevOps, CI/CD, automation,
Infrastructure as Code, Terraform, Ansible, container orchestration,
Kubernetes, Docker, service mesh, platform engineering, cloud-native,
serverless, disaster recovery, business continuity, backup & replication,
RTO, RPO

SECTOR 4: "Support"
Keywords: hardware maintenance, onsite support, break/fix, depot repair,
lifecycle management, asset disposition, e-waste, spare parts,
warranty services, SLAs, ticketing, incident management, NOC,
remote monitoring, 24/7 monitoring, fault management, managed support,
helpdesk, tier 1/2/3 support, technical support, IT outsourcing,
staff augmentation, managed staff, vendor management, monitoring tools,
SNMP, Prometheus, Datadog, Nagios, break/fix, time and materials,
ad hoc support, hardware reseller, VAR, value-added reseller, box shifter,
printer support, CCTV, physical security, helpdesk only, first line support,
service desk outsourcing, hardware maintenance, depot repair, warranty services
Note: helpdesk-only or hardware-only without broader managed services
belongs here, NOT in MSP V.

SECTOR 5: "Infrastructure"
Keywords: data center, colocation, hyperscale, Tier III, facility management,
cloud infrastructure, IaaS, PaaS, private cloud, hybrid cloud, multi-cloud,
hosted servers, managed hosting, dedicated hosting, VPS, virtualization,
bare metal, metal-as-a-service, edge computing, CDN, content delivery,
edge nodes, power & cooling, UPS, redundancy, high-availability,
network backbone, fiber, peering, interconnect, cross-connect

SECTOR 6: "Other"
Anything that does not clearly fit the five sectors above.

Classification rules:
- Cluster signals: companies mentioning at least two of [SOC, MDR, SIEM,
  endpoint, zero trust] alongside [recurring, SLA, contract] → Security or MSP V
- Weight vertical signals highly: "IT partner to law firms" or
  "healthcare-focused MSP V" is a stronger indicator than a generic claim
- Certification mentions (ISO 27001, SOC 2, Cyber Essentials Plus, CREST,
  G-Cloud) are strong maturity proxies — factor into Security and MSP V
- Helpdesk-only or hardware-only without broader managed services → Support, not MSP V

Rules:
- Return ONLY a JSON array, no explanation, no markdown, no code fences.
- Each element: {"index": <original_index>, "sector": "<sector_name>"}
- Sector must be exactly one of: "Security", "MSP V", "Integration",
"Support", "Infrastructure", "Other"
- Do not add any text before or after the JSON array.
"""

SECTORS_ORDER = [
    "Security",
    "MSP V",
    "Integration",
    "Support",
    "Infrastructure",
    "Other",
]

# ── Load data ─────────────────────────────────────────────────────────────────


def load_data():
    df = pd.read_excel(INPUT_XLSX, sheet_name=INPUT_SHEET, dtype=str)
    df["found_url"] = df["found_url"].fillna("").str.strip()
    df["Company Number"] = df["Company Number"].str.strip()
    print(f"Loaded {len(df):,} rows from {INPUT_SHEET}")

    # The checkpoint enriches rows with page title + scraped homepage content.
    # It is optional: if it is missing, classification falls back to
    # name + url (+ title only when present), which is still valid.
    cp_by_url = {}
    if Path(CHECKPOINT_JSONL).exists():
        with open(CHECKPOINT_JSONL) as f:
            for line in f:
                line = line.strip()
                if line:
                    e = json.loads(line)
                    if e.get("url"):
                        cp_by_url[e["url"]] = e
        print(f"Loaded {len(cp_by_url):,} checkpoint entries")
    else:
        print(f"No checkpoint at {CHECKPOINT_JSONL} — using name+url+title signals only")

    titles, sg_contents = [], []
    capped, empty_sg = 0, 0
    for _, row in df.iterrows():
        cp = cp_by_url.get(row["found_url"], {})
        title = cp.get("title", "").strip()
        sg = cp.get("sg_content", "").strip()
        if not sg:
            empty_sg += 1
        elif len(sg) > SG_CONTENT_CAP:
            sg = sg[:SG_CONTENT_CAP]
            capped += 1
        titles.append(title)
        sg_contents.append(sg)

    df["_title"] = titles
    df["_sg_content"] = sg_contents
    print(f"sg_content: {capped:,} rows capped at {SG_CONTENT_CAP} chars | "
          f"{empty_sg:,} empty (name+url+title only)")
    return df

# ── Prompt ────────────────────────────────────────────────────────────────────


def build_prompt(batch_df: pd.DataFrame) -> str:
    lines = []
    for local_i, (_, row) in enumerate(batch_df.iterrows()):
        name = str(row.get("Business Name", "")).strip()
        url = str(row.get("found_url", "")).strip()
        title = str(row.get("_title", "")).strip()
        sg = str(row.get("_sg_content", "")).strip().replace("\n", " ")
        entry = f"index: {local_i}\nbusiness_name: {name}\nurl: {url}\nurl_title: {title}"
        if sg:
            entry += f"\nwebsite_content: {sg}"
        lines.append(entry)
    return "Classify the following companies.\n\n" + "\n\n---\n\n".join(lines)

# ── Live API ──────────────────────────────────────────────────────────────────


async def classify_batch_live(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    batch_df: pd.DataFrame,
) -> dict:
    local_to_orig = {i: idx for i, idx in enumerate(batch_df.index)}
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SECTOR_DEFINITIONS,
        "messages": [{"role": "user", "content": build_prompt(batch_df)}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw = data["content"][0]["text"].strip()
                        if raw.startswith("```"):
                            raw = raw.split("```")[1]
                        if raw.startswith("json"):
                            raw = raw[4:]
                        results = json.loads(raw.strip())
                        return {local_to_orig[item["index"]]: item["sector"] for item in results}
                    if resp.status == 429:
                        wait = int(resp.headers.get("retry-after", 30))
                        print(f"  Rate limited — sleeping {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    await asyncio.sleep(BASE_RETRY_DELAY * attempt)
            except (json.JSONDecodeError, KeyError) as e:
                print(f"  Parse error attempt {attempt}: {e}")
                await asyncio.sleep(BASE_RETRY_DELAY)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"  Network error attempt {attempt}: {e}")
                await asyncio.sleep(BASE_RETRY_DELAY * (2 ** (attempt - 1)))
        return {idx: "Other" for idx in batch_df.index}


async def run_live(df: pd.DataFrame) -> dict:
    n_batches = (len(df) + BATCH_SIZE - 1) // BATCH_SIZE
    batches = [df.iloc[i * BATCH_SIZE:(i + 1) * BATCH_SIZE] for i in range(n_batches)]
    results = {}
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession() as session:
        async def process(batch, num):
            res = await classify_batch_live(session, semaphore, batch)
            async with lock:
                results.update(res)
                print(f"  Batch {num}/{n_batches} done — {len(results):,}/{len(df):,} rows")
        await asyncio.gather(*[process(b, i + 1) for i, b in enumerate(batches)])
    return results

# ── Batch API ─────────────────────────────────────────────────────────────────


def submit_batch(df: pd.DataFrame):
    import urllib.request

    n_batches = (len(df) + BATCH_SIZE - 1) // BATCH_SIZE
    requests = []
    idx_mapping = {}

    for i in range(n_batches):
        batch_df = df.iloc[i * BATCH_SIZE:(i + 1) * BATCH_SIZE]
        custom_id = f"batch_{i}"
        idx_mapping[custom_id] = list(batch_df.index)
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": CLAUDE_MODEL,
                "max_tokens": MAX_TOKENS,
                "system": SECTOR_DEFINITIONS,
                "messages": [{"role": "user", "content": build_prompt(batch_df)}],
            },
        })

    with open(BATCH_IDX_MAP_FILE, "w") as f:
        json.dump(idx_mapping, f)
    print(f"Saved index mapping → {BATCH_IDX_MAP_FILE}")

    # Save JSONL locally for reference
    with open(BATCH_REQUESTS_FILE, "w") as f:
        for r in requests:
            f.write(json.dumps(r) + "\n")
    print(f"Wrote {len(requests):,} requests → {BATCH_REQUESTS_FILE} ({n_batches} batches × up to {BATCH_SIZE} rows)")

    # API expects JSON body with a "requests" array, not JSONL
    payload = json.dumps({"requests": requests}).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages/batches",
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    batch_id = data["id"]
    with open(BATCH_ID_FILE, "w") as f:
        f.write(batch_id)
    print(f"\nBatch submitted — ID: {batch_id}")
    print(f"Status: {data['processing_status']}")
    print(f"Run --batch-results to download when done (up to 24h).")


def poll_and_download_batch(df: pd.DataFrame):
    import urllib.request

    if not Path(BATCH_ID_FILE).exists():
        print(f"No {BATCH_ID_FILE} found. Submit first with --batch.")
        return

    batch_id = open(BATCH_ID_FILE).read().strip()
    print(f"Checking batch {batch_id}...")

    req = urllib.request.Request(
        f"https://api.anthropic.com/v1/messages/batches/{batch_id}",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    status = data["processing_status"]
    counts = data.get("request_counts", {})
    print(f"Status: {status} | {counts}")

    if status != "ended":
        print("Not ready yet — re-run --batch-results later.")
        return

    req2 = urllib.request.Request(
        data["results_url"],
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req2) as resp:
        results_raw = resp.read().decode()

    idx_mapping = json.load(open(BATCH_IDX_MAP_FILE))
    sector_map = {}

    for line in results_raw.strip().splitlines():
        result = json.loads(line)
        custom_id = result["custom_id"]
        idx_list = idx_mapping.get(custom_id, [])
        if result.get("result", {}).get("type") == "succeeded":
            raw = result["result"]["message"]["content"][0]["text"].strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            try:
                items = json.loads(raw.strip())
                for item in items:
                    local_i = item["index"]
                    if local_i < len(idx_list):
                        sector_map[idx_list[local_i]] = item["sector"]
            except (json.JSONDecodeError, KeyError):
                for idx in idx_list:
                    sector_map[idx] = "Other"
        else:
            for idx in idx_list:
                sector_map[idx] = "Other"

    print(f"Parsed {len(sector_map):,} classified rows")
    write_output(df, sector_map)

# ── Output ────────────────────────────────────────────────────────────────────

INTERNAL_COLS = ["_title", "_sg_content"]


def write_output(df: pd.DataFrame, sector_map: dict, output_path: str | None = None):
    output_path = output_path or OUTPUT_XLSX

    df = df.copy()
    df["Sector"] = df.index.map(lambda i: sector_map.get(i, "Other"))
    df = df.drop(columns=INTERNAL_COLS, errors="ignore")

    sheets = {
        "security": df[df["Sector"] == "Security"],
        "MSP V": df[df["Sector"] == "MSP V"],
        "integration": df[df["Sector"] == "Integration"],
        "support": df[df["Sector"] == "Support"],
        "infrastructure": df[df["Sector"] == "Infrastructure"],
        "other": df[df["Sector"] == "Other"],
    }

    # Make sure the destination directory exists before writing.
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    print("\nWriting output sheets:")
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for sheet_name, sheet_df in sheets.items():
            sheet_df.to_excel(writer, index=False, sheet_name=sheet_name)
            print(f"  {sheet_name:<10} {len(sheet_df):>5} rows")

    print(f"\nTotal: {sum(len(v) for v in sheets.values()):,} rows → {output_path}")

# ── Entry points ──────────────────────────────────────────────────────────────


def cmd_test():
    print("=== TEST MODE — first 20 rows, live API ===\n")
    df = load_data()
    sample = df.head(20)
    print(f"\nSending {len(sample)} rows to Claude...\n")
    results = asyncio.run(run_live(sample))
    print("\n--- Results ---")
    for orig_idx, new_sector in results.items():
        name = sample.loc[orig_idx, "Business Name"]
        has_sg = bool(sample.loc[orig_idx, "_sg_content"])
        signals = "name+url+title+sg" if has_sg else "name+url+title only"
        print(f"  {str(name)[:40]:<40} [{signals}] → {new_sector}")
    write_output(sample, results)


def cmd_batch_submit():
    print("=== BATCH MODE — submitting to Batch API (50% off) ===\n")
    df = load_data()
    submit_batch(df)


def cmd_batch_results():
    print("=== BATCH RESULTS — checking / downloading ===\n")
    df = load_data()
    poll_and_download_batch(df)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true", help="Run on first 20 rows, live API")
    group.add_argument("--batch", action="store_true", help="Submit full job to Batch API (50% off)")
    group.add_argument("--batch-results", action="store_true", help="Poll / download batch results")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set in .env or environment")
        exit(1)

    if args.test:
        cmd_test()
    elif args.batch:
        cmd_batch_submit()
    elif args.batch_results:
        cmd_batch_results()
