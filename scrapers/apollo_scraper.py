from __future__ import annotations
"""
Apollo.io People Search API — finds contacts by company name + job title.

Flow:
  1. POST /api/v1/mixed_people/api_search  → get person IDs (data masked)
  2. POST /api/v1/people/bulk_match        → reveal full profiles (costs credits)

Extra endpoints:
  - POST /api/v1/people/match              → enrich a SINGLE person found by other
    sources (LinkedIn/Xing/register) with email + phone, by name+company or
    linkedin_url. This is the highest-precision way to get personal emails.
  - GET  /api/v1/organizations/enrich      → authoritative company domain + phone
    (fixes wrong-domain guesses like 'Octopus Energy' → octopus.com).
  - POST /api/v1/mixed_companies/search    → resolve organization_id by name, used
    to scope people-search to the right company.

Docs: https://docs.apollo.io/reference/people-api-search
"""
import os
import re as _re
import logging
import requests

logger = logging.getLogger(__name__)

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")
APOLLO_BASE = "https://api.apollo.io/api/v1"

_HEADERS = {"Content-Type": "application/json", "Cache-Control": "no-cache"}


def _headers() -> dict:
    return {**_HEADERS, "x-api-key": APOLLO_API_KEY}


def apollo_available() -> bool:
    # Apollo API calls are intentionally disabled.
    # Gemini initial search now handles company discovery.
    # To re-enable: remove the early return below and restore APOLLO_API_KEY usage.
    return False


# ─── Organization enrichment ──────────────────────────────────────────────────

def enrich_organization(company_name: str = "", domain: str = "") -> dict | None:
    """
    Resolve the company in Apollo's DB. Returns a dict:
      {id, name, primary_domain, website_url, phone, linkedin_url}
    Strategy: domain lookup (exact, free-ish) → name search (fuzzy, pick best match).
    """
    if not apollo_available():
        return None

    org = None
    if domain:
        org = _org_enrich_by_domain(domain)
    if not org and company_name:
        org = _org_search_by_name(company_name)
    if not org:
        return None

    return {
        "id": org.get("id"),
        "name": org.get("name"),
        "primary_domain": org.get("primary_domain"),
        "website_url": org.get("website_url"),
        "phone": (org.get("primary_phone") or {}).get("number") or org.get("phone") or org.get("sanitized_phone"),
        "linkedin_url": org.get("linkedin_url"),
    }


def _org_enrich_by_domain(domain: str) -> dict | None:
    try:
        resp = requests.get(
            f"{APOLLO_BASE}/organizations/enrich",
            params={"domain": domain},
            headers=_headers(),
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("organization")
        logger.warning(f"Apollo org enrich: {resp.status_code} — {resp.text[:150]}")
    except Exception as e:
        logger.error(f"Apollo org enrich error: {e}")
    return None


def _norm_org_name(name: str) -> str:
    name = _re.sub(
        r"\b(gmbh|ag|kg|ohg|gbr|ug|e\.k\.|ek|se|eg|ltd|llc|inc|corp|bv|srl|sa|sas|"
        r"gmbh\s*&\s*co\.?\s*kg)\b", "", name, flags=_re.IGNORECASE)
    return _re.sub(r"[^a-z0-9]", "", name.lower())


def _org_search_by_name(company_name: str) -> dict | None:
    try:
        resp = requests.post(
            f"{APOLLO_BASE}/mixed_companies/search",
            json={"q_organization_name": company_name, "page": 1, "per_page": 5},
            headers=_headers(),
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"Apollo org search: {resp.status_code} — {resp.text[:150]}")
            return None
        orgs = (resp.json().get("organizations") or []) + (resp.json().get("accounts") or [])
        if not orgs:
            return None
        # Pick the org whose normalized name best matches; reject totally unrelated hits
        target = _norm_org_name(company_name)
        best, best_score = None, 0.0
        for o in orgs:
            cand = _norm_org_name(o.get("name") or "")
            if not cand:
                continue
            if cand == target:
                return o
            shorter, longer = sorted((cand, target), key=len)
            score = len(shorter) / len(longer) if shorter and shorter in longer else 0.0
            if score > best_score:
                best, best_score = o, score
        # Require meaningful overlap — avoids matching 'Ruby Group' to a random 'Ruby'
        return best if best_score >= 0.45 else None
    except Exception as e:
        logger.error(f"Apollo org search error: {e}")
        return None


# ─── Single-person match (people/match) ──────────────────────────────────────

def match_person(
    full_name: str = "",
    first_name: str = "",
    last_name: str = "",
    company_name: str = "",
    domain: str = "",
    linkedin_url: str = "",
    email: str = "",
    reveal_personal_emails: bool = True,
) -> dict | None:
    """
    Enrich ONE person via Apollo people/match. Identification by any combination of
    name + organization_name/domain, or directly by linkedin_url / email.
    Returns normalized contact dict or None if no match. Costs 1 credit per match.
    """
    if not apollo_available():
        return None

    if full_name and not (first_name and last_name):
        parts = full_name.strip().split()
        if len(parts) >= 2:
            first_name, last_name = parts[0], parts[-1]
        else:
            first_name = full_name.strip()

    payload: dict = {"reveal_personal_emails": reveal_personal_emails}
    if first_name:
        payload["first_name"] = first_name
    if last_name:
        payload["last_name"] = last_name
    if full_name:
        payload["name"] = full_name
    if company_name:
        payload["organization_name"] = company_name
    if domain:
        payload["domain"] = domain
    if linkedin_url:
        payload["linkedin_url"] = linkedin_url
    if email:
        payload["email"] = email

    # Need at least a usable identifier
    if not (linkedin_url or email or (last_name and (company_name or domain))):
        return None

    try:
        resp = requests.post(
            f"{APOLLO_BASE}/people/match",
            json=payload,
            headers=_headers(),
            timeout=20,
        )
    except Exception as e:
        logger.error(f"Apollo people/match error: {e}")
        return None

    if resp.status_code != 200:
        logger.warning(f"Apollo people/match: {resp.status_code} — {resp.text[:150]}")
        return None

    person = resp.json().get("person")
    if not person:
        return None

    matched_email = person.get("email")
    email_status = person.get("email_status")
    if matched_email and email_status in ("invalid", "blocked"):
        matched_email = None

    phone = None
    for ph in (person.get("phone_numbers") or []):
        raw = ph.get("sanitized_number") or ph.get("raw_number")
        if raw:
            phone = raw
            break

    org = person.get("organization") or {}
    return {
        "full_name": person.get("name") or _join_name(person.get("first_name"), person.get("last_name")),
        "title": person.get("title") or person.get("headline"),
        "email": matched_email,
        "email_status": email_status,
        "phone": phone,
        "linkedin_url": person.get("linkedin_url"),
        "company": org.get("name") or person.get("organization_name"),
        "source": "apollo_match",
    }


def search_apollo_contacts(
    company_name: str,
    location: str = "",
    job_category: str = "",
    max_results: int = 10,
    organization_id: str = "",
) -> list[dict]:
    if not apollo_available():
        return []

    # Step 1: Search — returns masked profiles with IDs.
    # When an organization_id is known (via enrich_organization), scope the search
    # to that exact org — eliminates wrong-company matches entirely.
    ids = _search_person_ids(company_name, location, job_category, max_results,
                             organization_id=organization_id)
    if not ids and not organization_id:
        # Fall back to shorter name only for name-based search
        # (e.g. "Park Plaza Berlin" → "Park Plaza" if exact match returns 0)
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


def _search_person_ids(company_name: str, location: str, job_category: str, max_results: int,
                       organization_id: str = "") -> list[str]:
    payload: dict = {
        "page": 1,
        "per_page": min(max_results, 25),
    }
    if organization_id:
        # Exact org scoping — no name ambiguity, no need for location filter
        payload["organization_ids"] = [organization_id]
    else:
        payload["q_organization_name"] = company_name
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
            headers=_headers(),
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
            headers=_headers(),
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
