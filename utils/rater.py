from __future__ import annotations
import re
import unicodedata
from config import DECISION_MAKER_TITLES

# Short abbreviations must match as whole words (avoid "cto" in "director")
_SHORT_ABBREVS = {"ceo", "coo", "cfo", "cto", "gm", "md", "vp", "cpo", "chro"}


def _normalize_title(title: str) -> str:
    """Lowercase + strip German umlauts for fuzzy matching."""
    title_lower = title.lower()
    # Normalize umlauts for matching: ä→a, ö→o, ü→u, ß→ss
    nfkd = unicodedata.normalize("NFKD", title_lower)
    ascii_title = nfkd.encode("ascii", "ignore").decode("ascii")
    return ascii_title


def _kw_matches(kw: str, title_lower: str, title_normalized: str) -> bool:
    """Match keyword against both original (umlaut-aware) and normalized title."""
    if kw in _SHORT_ABBREVS:
        pattern = r"\b" + re.escape(kw) + r"\b"
        return bool(re.search(pattern, title_lower)) or bool(re.search(pattern, title_normalized))
    # Try exact match in original title first (handles umlauts)
    if kw in title_lower:
        return True
    # Also try normalized version (e.g. "geschaeftsfuehrer" matches "geschäftsführer")
    kw_normalized = _normalize_title(kw)
    return kw_normalized in title_normalized


def rate_contact(title: str | None, job_category: str = "") -> tuple[int, str]:
    """
    Rate contact 1-5 based on hiring authority for a job agency.
    1 = highest authority (can sign agreements), 5 = lowest.
    Supports English and German titles.
    Returns (rating, reason).
    """
    if not title:
        return 5, "No title available — cannot assess authority"

    title_lower = title.lower()
    title_normalized = _normalize_title(title)

    for rating, keywords in DECISION_MAKER_TITLES.items():
        for kw in keywords:
            if _kw_matches(kw, title_lower, title_normalized):
                reasons = {
                    1: f"Geschäftsführer/Executive ({title}) — highest hiring authority",
                    2: f"HR/People leadership ({title}) — can authorize agency agreements",
                    3: f"HR/TA Manager ({title}) — day-to-day recruitment decisions",
                    4: f"HR/TA Specialist ({title}) — involved in hiring, limited authority",
                    5: f"Support role ({title}) — likely no independent authority",
                }
                return rating, reasons[rating]

    # Fallback: seniority signals (German + English)
    senior_signals = [
        "head of", "chief", "director", r"\bvp\b", "vice president",
        "senior", "lead", "principal", "leiter", "leiterin",
        "verantwortlich", r"\bchef\b", "vorgesetzter",
    ]
    for sig in senior_signals:
        if re.search(sig, title_lower):
            return 3, f"Senior-sounding title ({title}) — moderate authority, unclassified"

    return 5, f"Title ({title}) not matched to known decision-maker patterns"
