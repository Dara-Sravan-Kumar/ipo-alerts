"""
matching.py
===========
Company-name normalization so we can match an IPO coming from the exchange
(NSE/BSE) against a GMP row coming from a completely different source (a scraped
GMP site, or an aggregator API). Names never match exactly across sources —
"XYZ Technologies Limited" vs "XYZ Technologies IPO" vs "XYZ Tech (SME)" — so we
strip everything that isn't the core name and compare the residue.
"""

from __future__ import annotations

import re

# Tokens that appear in one source but not another and carry no identity.
_NOISE = {
    "ipo", "limited", "ltd", "the", "private", "pvt", "sme", "mainboard",
    "nse", "bse", "company", "co", "india", "indian", "industries", "inds",
    "enterprises", "corporation", "corp", "and", "&",
}


def normalize_company_name(name: str) -> str:
    """Reduce a company/IPO name to a comparable key (lowercase, no noise)."""
    if not name:
        return ""
    text = name.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = [t for t in text.split() if t and t not in _NOISE]
    return "".join(tokens)


def best_match(target: str, candidates: dict[str, str]) -> str | None:
    """
    Return the value from `candidates` (a {normalized_name: value} map) whose key
    best matches `target`. Tries exact, then containment either direction, with a
    minimum length guard to avoid spurious short-substring hits.
    """
    key = normalize_company_name(target)
    if not key:
        return None
    if key in candidates:
        return candidates[key]
    for cand_key, value in candidates.items():
        if len(key) < 4 or len(cand_key) < 4:
            continue
        if key.startswith(cand_key) or cand_key.startswith(key):
            return value
        if key in cand_key or cand_key in key:
            return value
    return None
