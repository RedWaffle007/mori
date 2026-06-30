"""
Taxonomy registry.

Each taxonomy is a plain module exposing the same surface:
    LABEL              — human-readable name for the UI
    SECTOR_DEFINITIONS — the classification system prompt
    SECTORS_ORDER      — ordered list of sector names the scheme generates
    SHEET_MAP          — {sheet_name: Sector value} for the output workbook
    FALLBACK_SECTOR    — sector assigned on an API/parse failure

The engine (sector_classifier.py) pulls all taxonomy-specific values from the
active taxonomy via get_taxonomy(TAXONOMY_ID); main.py sets TAXONOMY_ID per job.
To add a new taxonomy: drop in a module of the same shape and register it below —
no engine or frontend edits required (the UI is driven by list_taxonomies()).
"""

from . import sic_80200, sic_63110

REGISTRY = {
    "sic_80200": sic_80200,
    "sic_63110": sic_63110,
}

DEFAULT_TAXONOMY_ID = "sic_80200"


def get_taxonomy(taxonomy_id: str):
    """Return the taxonomy module for `taxonomy_id`.

    Unknown ids fall back to the default so a stale/empty value never crashes a
    job (existing jobs that carry no taxonomy id keep the 80200 behaviour)."""
    return REGISTRY.get(taxonomy_id or DEFAULT_TAXONOMY_ID, REGISTRY[DEFAULT_TAXONOMY_ID])


def list_taxonomies() -> list[dict]:
    """Return [{id, label, sectors}] for every registered taxonomy, for the UI."""
    return [
        {
            "id": tax_id,
            "label": getattr(mod, "LABEL", tax_id),
            "sectors": list(mod.SECTORS_ORDER),
        }
        for tax_id, mod in REGISTRY.items()
    ]
