from __future__ import annotations
import re
from config import DECISION_MAKER_TITLES

# Short abbreviations (≤3 chars) must match as whole words to avoid false substrings
# e.g. "cto" must not match inside "director", "coo" must not match inside "coordinator"
_SHORT_ABBREVS = {"ceo", "coo", "cfo", "cto", "gm", "md", "vp"}


def _kw_matches(kw: str, title_lower: str) -> bool:
    if kw in _SHORT_ABBREVS:
        return bool(re.search(r"\b" + re.escape(kw) + r"\b", title_lower))
    return kw in title_lower


def rate_contact(title: str | None, job_category: str = "") -> tuple[int, str]:
    """
    Rate contact 1-5 based on hiring authority for a job agency.
    1 = highest authority (can sign agreements), 5 = lowest.
    Returns (rating, reason).
    """
    if not title:
        return 5, "No title available — cannot assess authority"

    title_lower = title.lower()

    for rating, keywords in DECISION_MAKER_TITLES.items():
        for kw in keywords:
            if _kw_matches(kw, title_lower):
                reasons = {
                    1: f"Executive / owner-level title ({title}) — highest hiring authority",
                    2: f"VP/Director HR/People ({title}) — can authorize agency agreements",
                    3: f"HR/TA Manager ({title}) — day-to-day recruitment decisions",
                    4: f"HR/TA Specialist ({title}) — involved in hiring, limited authority",
                    5: f"Support role ({title}) — likely no independent authority",
                }
                return rating, reasons[rating]

    # Fallback: check if any seniority signals exist
    senior_signals = ["head of", "chief", "director", r"\bvp\b", "vice president", "senior", "lead", "principal"]
    for sig in senior_signals:
        if re.search(sig, title_lower):
            return 3, f"Senior-sounding title ({title}) — moderate authority, unclassified"

    return 5, f"Title ({title}) not matched to known decision-maker patterns"
