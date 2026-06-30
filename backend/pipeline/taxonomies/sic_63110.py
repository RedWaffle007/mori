"""
Taxonomy: SIC 63110 — data processing, hosting, cloud, network-edge & security.

Mirrors the SHAPE of sic_80200.py exactly (LABEL / SECTOR_DEFINITIONS /
SECTORS_ORDER / SHEET_MAP / FALLBACK_SECTOR) and the framing/voice of the 80200
prompt (same "You are a company sector classifier..." opening, same per-sector
keyword blocks, same final "Rules:" JSON-output contract).

FIVE sectors only. There is intentionally NO "Other" sector here — see SHEET_MAP.
On an API/parse failure the engine falls back to FALLBACK_SECTOR, which is a real
sector ("Cloud & IaaS Platforms") so failed rows still land somewhere instead of
crashing or being dropped.
"""

# Human-readable label for the UI dropdown.
LABEL = "SIC 63110 — Data processing & hosting"

# The classification prompt (system message). Same framing/voice as 80200.
SECTOR_DEFINITIONS = """
You are a company sector classifier. Classify each company into exactly one sector
using all available signals: business name, website URL, page title, and website content.
Website content may be absent for some rows — use the other signals in that case.

SECTOR 1: "Data Processing Services"
Core keywords: data processing bureau, batch processing, payroll bureau,
billing processor, data processing services
Analytics & Reporting: analytics platform, business intelligence,
reporting platform, data analytics, ETL pipeline, data transformation
Digitisation & Data Entry: data entry, document digitisation, data capture,
OCR processing, forms processing, scanning bureau
Contract signals: bureau services, processing contract, SLA-backed,
outsourced processing
Tech stack: SQL Server, Oracle, PostgreSQL, Hadoop, Spark, Snowflake, Azure, AWS
Maturity signals: multi-year enterprise / public-sector contracts,
proprietary processing platform, ISO 27001, Cyber Essentials,
GDPR data processor agreements, >£50k ARR per client

SECTOR 2: "Hosting"
Managed Hosting & Colocation: managed hosting, colocation, colo,
dedicated server hosting, datacentre, data centre, server hosting
Resilience & Facilities: Tier III, Tier IV, N+1, 2N redundancy,
Uptime Institute, PUE
Connectivity: carrier-neutral, cross-connect, dark fibre, MPLS, SD-WAN, private line
Managed Operations: managed server, managed network, NOC, 24x7 monitoring,
patch management, remote hands
Backup & Disaster Recovery: disaster recovery hosting, BaaS, DRaaS,
off-site backup, replication
Certifications: ISO 27001, SOC 2, PCI DSS compliant hosting, GDPR hosting,
UK data residency
Specialist & Regulated — Financial: FCA regulated hosting, financial services cloud,
trading infrastructure hosting, low latency hosting, capital markets hosting
Specialist & Regulated — Healthcare: NHS hosting, healthcare data platform,
DSP Toolkit, IG Toolkit, DSPT compliant, clinical data hosting
Specialist & Regulated — Government: secure hosting, government cloud,
IL2/IL3 hosting, OFFICIAL-SENSITIVE, Cyber Essentials Plus, MOD supplier, PSN compliance
Specialist & Regulated — Media & Gaming: game server hosting, gaming infrastructure,
live streaming hosting, media asset management, broadcast hosting
Specialist & Regulated — Legal: legal data hosting, law firm cloud, e-discovery hosting,
document review hosting, legal tech infrastructure

SECTOR 3: "Cloud & IaaS Platforms"
Core keywords: cloud hosting, IaaS, PaaS, private cloud, hybrid cloud,
cloud infrastructure, cloud platform
Partners & Managed Services: AWS/Azure/Google Cloud partner, cloud MSP,
cloud migration, cloud managed services
Sovereign & Virtualisation: sovereign cloud, UK cloud, on-premise cloud,
VMware cloud, OpenStack
Compute & AI: GPU cloud, GPU compute, HPC hosting, AI infrastructure,
ML training infrastructure, NVIDIA cloud
Commercial model: consumption-based, pay-as-you-go, reserved instances,
committed use, cloud cost optimisation, FinOps
Cloud security: cloud security, cloud SIEM, CSPM, zero trust cloud, secure cloud,
FedRAMP equivalent, IL3 cloud

SECTOR 4: "CDN & Network Services"
Core keywords: content delivery network, CDN, edge network, managed DNS,
DNS hosting, domain services
Edge security: DDoS mitigation, DDoS protection, WAF, web application firewall,
bot management, API security
Traffic & Performance: load balancing, traffic management, anycast,
edge caching, latency optimisation
Peering & Routing: internet exchange, IXP peering, BGP, anycast routing, PoP network, LINX
Media delivery: media delivery, video streaming CDN, gaming CDN, e-commerce CDN, OTT

SECTOR 5: "Security"
Compliance-as-product: GDPR-compliant storage, data residency management,
backup compliance
Where compliance IS the product: a business whose storage/hosting product *is* its
compliance posture fits here (e.g. "compliant cloud storage for healthcare")
Certifications as signals: ISO 27001, SOC 2, PCI DSS, Cyber Essentials Plus
as compliance-as-product signals

Classification rules:
- Weight vertical / regulated signals highly: an FCA-, NHS-, or government-targeted
  offering is a stronger indicator than a generic claim
- Certification mentions (ISO 27001, SOC 2, PCI DSS, Cyber Essentials Plus) are
  maturity proxies — factor them in
- A pure managed-hosting / colocation OR regulated / specialist hosting business → Hosting
- A consumption / ARR cloud-IaaS or GPU-compute business → Cloud & IaaS Platforms
- A CDN / DNS / DDoS / WAF network-edge business → CDN & Network Services
- A business whose PRODUCT is the compliance / data-residency posture itself → Security

Rules:
- Return ONLY a JSON array, no explanation, no markdown, no code fences.
- Each element: {"index": <original_index>, "sector": "<sector_name>"}
- Sector must be exactly one of: "Data Processing Services", "Hosting",
"Cloud & IaaS Platforms", "CDN & Network Services", "Security"
- Do not add any text before or after the JSON array.
"""

# Ordered sector names this scheme generates (exact order, exact names).
SECTORS_ORDER = [
    "Data Processing Services",
    "Hosting",
    "Cloud & IaaS Platforms",
    "CDN & Network Services",
    "Security",
]

# sheet_name -> Sector value. Sheet names mirror the sector names. Shape parallels
# sic_80200.py's SHEET_MAP; the "other" line is intentionally kept (commented) so the
# omission of an Other category is explicit rather than silent.
SHEET_MAP = {
    "Data Processing Services": "Data Processing Services",
    "Hosting": "Hosting",
    "Cloud & IaaS Platforms": "Cloud & IaaS Platforms",
    "CDN & Network Services": "CDN & Network Services",
    "Security": "Security",
    # "other": "Other",  # intentionally omitted for 63110 — no Other category
}

# Sector the engine assigns on an API/parse failure. Deliberately a real sector
# (there is no Other here) so failed rows land somewhere instead of crashing.
FALLBACK_SECTOR = "Cloud & IaaS Platforms"
