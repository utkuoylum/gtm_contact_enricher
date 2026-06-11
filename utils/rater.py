from __future__ import annotations
import re
import unicodedata
from datetime import date
from config import DECISION_MAKER_TITLES

_CURRENT_YEAR: int = date.today().year

# Sources that are legally/structurally required to be kept current — no age penalty.
_ALWAYS_CURRENT_SOURCES = {
    "impressum",        # German law: must be up-to-date at all times
    "german_register",  # Handelsregister: official registered officer
    "website_schema",   # schema.org Person markup on company website
    "website_card",     # live team page card
}

# Sources that imply the person is actively there right now.
_ACTIVELY_HIRING_SOURCES = {
    "job_portal",       # person posted/manages a live job ad → definitely still there
    "linkedin_jobs",    # found via live LinkedIn job posting
}

# Per-year age penalty added to the base title rating (float, lower = better).
# Rating scale is 1–5; penalty pushes contact toward lower priority.
_AGE_PENALTIES = [
    (0, 0.0),    # current year   → no penalty
    (1, 0.3),    # 1 year old     → tiny penalty
    (2, 0.7),    # 2 years old    → moderate
    (3, 1.2),    # 3 years old    → significant (likely left)
    (4, 1.8),    # 4 years old    → high
    (5, 2.5),    # 5+ years old   → very high
]

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


def recency_adjustment(source: str, year_found: int | None) -> tuple[float, str]:
    """
    Return (penalty, note) based on how fresh the data is.

    penalty is added to the base title rating for sorting — higher = lower priority.
    Returns 0.0 for sources that are structurally always current (Impressum, Handelsregister, etc.)
    and a graduated penalty for dated sources (press releases, SERP snippets, etc.).
    """
    # Sources that are always current by definition
    if source in _ALWAYS_CURRENT_SOURCES:
        return 0.0, "Quelle immer aktuell (Impressum / Handelsregister / Website)"

    # Active job posting → person is definitely there now
    if source in _ACTIVELY_HIRING_SOURCES:
        return -0.3, "Aktive Stellenausschreibung gefunden — Person derzeit im Unternehmen"

    if year_found is None:
        # No date info → slight uncertainty for non-authoritative sources
        return 0.3, "Erscheinungsdatum unbekannt — geringe Unsicherheit"

    age = _CURRENT_YEAR - year_found
    if age < 0:
        age = 0  # future-dated (unlikely but safe)

    # Find the right penalty bracket
    penalty = _AGE_PENALTIES[-1][1]
    for max_age, p in _AGE_PENALTIES:
        if age <= max_age:
            penalty = p
            break

    if age == 0:
        note = f"Aktuelles Jahr ({year_found}) — sehr frisch"
    elif age == 1:
        note = f"1 Jahr alt ({year_found}) — wahrscheinlich noch aktuell"
    elif age == 2:
        note = f"2 Jahre alt ({year_found}) — möglicherweise noch aktuell"
    elif age == 3:
        note = f"3 Jahre alt ({year_found}) — Person könnte Unternehmen verlassen haben"
    elif age == 4:
        note = f"4 Jahre alt ({year_found}) — wahrscheinlich veraltet"
    else:
        note = f"{age} Jahre alt ({year_found}) — stark veraltet, bitte verifizieren"

    return penalty, note
