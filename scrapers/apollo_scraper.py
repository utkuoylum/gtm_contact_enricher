from __future__ import annotations
"""
Apollo.io People Search API — finds contacts by company name + job title.

Flow:
  1. POST /api/v1/mixed_people/api_search  → get person IDs (data masked)
  2. POST /api/v1/people/bulk_match        → reveal full profiles (costs credits)

Docs: https://docs.apollo.io/reference/people-api-search
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")
APOLLO_BASE = "https://api.apollo.io/api/v1"


def apollo_available() -> bool:
    return bool(APOLLO_API_KEY)


def search_apollo_contacts(
    company_name: str,
    location: str = "",
    job_category: str = "",
    max_results: int = 10,
) -> list[dict]:
    if not apollo_available():
        return []

    # Step 1: Search — returns masked profiles with IDs
    # Try exact company name first; fall back to shorter version if no results
    # (e.g. "Park Plaza Berlin" → "Park Plaza" if exact match returns 0)
    ids = _search_person_ids(company_name, location, job_category, max_results)
    if not ids:
        short_name = _shorten_company_name(company_name)
        if short_name != company_name:
            ids = _search_person_ids(short_name, location, job_category, max_results)
    if not ids:
        return []

    # Step 2: Reveal full profiles via bulk_match (costs credits)
    people = _bulk_match(ids)

    contacts = []
    for person in people:
        name = person.get("name") or _join_name(person.get("first_name"), person.get("last_name"))
        if not name:
            continue

        email = person.get("email")
        if email and person.get("email_status") in ("invalid", "blocked"):
            email = None

        phone = None
        for ph in (person.get("phone_numbers") or []):
            raw = ph.get("sanitized_number") or ph.get("raw_number")
            if raw:
                phone = raw
                break

        org = person.get("organization") or {}
        contacts.append({
            "full_name": name,
            "title": person.get("title") or person.get("headline"),
            "email": email,
            "phone": phone,
            "linkedin_url": person.get("linkedin_url"),
            "company": org.get("name") or person.get("organization_name"),
            "source": "apollo",
        })

    logger.info(f"Apollo '{company_name}': {len(contacts)} contacts revealed")
    return contacts


def _search_person_ids(company_name: str, location: str, job_category: str, max_results: int) -> list[str]:
    payload: dict = {
        "q_organization_name": company_name,
        "page": 1,
        "per_page": min(max_results, 25),
    }
    # Use only city name for location (not "Berlin, Deutschland" — Apollo wants plain city)
    if location:
        city = location.split(",")[0].strip()
        payload["person_locations"] = [city]
    # Skip title filter — it's too narrow and eliminates valid contacts.
    # Let Claude/scoring filter by relevance after we retrieve the contacts.

    try:
        resp = requests.post(
            f"{APOLLO_BASE}/mixed_people/api_search",
            json=payload,
            headers={"Content-Type": "application/json", "x-api-key": APOLLO_API_KEY},
            timeout=15,
        )
    except Exception as e:
        logger.error(f"Apollo search error: {e}")
        return []

    if resp.status_code != 200:
        logger.warning(f"Apollo search: {resp.status_code} — {resp.text[:200]}")
        return []

    people = resp.json().get("people", [])

    # Prioritise contacts that actually have email data in Apollo's DB
    with_email = [p["id"] for p in people if p.get("has_email")]
    without_email = [p["id"] for p in people if not p.get("has_email")]
    return (with_email + without_email)[:max_results]


def _bulk_match(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    try:
        resp = requests.post(
            f"{APOLLO_BASE}/people/bulk_match",
            json={
                "details": [{"id": pid} for pid in ids],
                "reveal_personal_emails": True,
            },
            headers={"Content-Type": "application/json", "x-api-key": APOLLO_API_KEY},
            timeout=20,
        )
    except Exception as e:
        logger.error(f"Apollo bulk_match error: {e}")
        return []

    if resp.status_code != 200:
        logger.warning(f"Apollo bulk_match: {resp.status_code} — {resp.text[:200]}")
        return []

    return resp.json().get("matches", [])


def _join_name(first: str | None, last: str | None) -> str | None:
    parts = [p for p in [first, last] if p]
    return " ".join(parts) if parts else None


def _shorten_company_name(name: str) -> str:
    """'Park Plaza Berlin GmbH' → 'Park Plaza' (drop city suffix + legal form)."""
    import re
    name = re.sub(r"\b(gmbh|ag|kg|ohg|gbr|ug|ltd|llc|inc|bv|srl|sa|sas)\b", "", name, flags=re.IGNORECASE)
    words = name.strip().split()
    # Drop last word if it looks like a standalone city name (capitalized, alpha-only)
    if len(words) >= 3 and words[-1][0].isupper() and words[-1].isalpha():
        words = words[:-1]
    return " ".join(words).strip(" ,.")


_CATEGORY_TITLE_MAP: dict[str, list[str]] = {
    "hr": ["HR Manager", "Human Resources", "Personalleiter", "Recruiting", "Talent Acquisition", "CHRO"],
    "event": ["Event Manager", "Events Manager", "Veranstaltungsleiter", "MICE", "Conference Manager"],
    "sales": ["Sales Manager", "Vertriebsleiter", "Account Manager", "Business Development", "Director of Sales"],
    "marketing": ["Marketing Manager", "CMO", "Head of Marketing"],
    "management": ["CEO", "Managing Director", "Geschäftsführer", "General Manager", "Hoteldirektor"],
    "finance": ["CFO", "Finance Director", "Controller"],
    "it": ["CTO", "IT Manager", "Head of IT"],
    "pr": ["PR Manager", "Public Relations", "Pressesprecher"],
}

_DEFAULT_TITLES = [
    "CEO", "Managing Director", "Geschäftsführer", "General Manager",
    "HR Manager", "Personalleiter", "Sales Manager", "Director",
]


def _title_keywords(job_category: str) -> list[str]:
    if not job_category:
        return _DEFAULT_TITLES
    cat = job_category.lower()
    for key, titles in _CATEGORY_TITLE_MAP.items():
        if key in cat:
            return titles
    return [job_category] + _DEFAULT_TITLES[:4]
