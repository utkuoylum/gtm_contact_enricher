from __future__ import annotations
"""
Hunter.io domain-search — finds emails of people who work at a domain.

Two endpoints:
  1. /v2/domain-search  : all known emails + names for a domain (up to 100/call)
  2. /v2/email-finder   : find email for a specific person (name + domain)

Free plan: 25 searches/month. Paid: $49/mo for 500.
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")
HUNTER_BASE = "https://api.hunter.io/v2"


def hunter_available() -> bool:
    return bool(HUNTER_API_KEY)


def search_hunter_contacts(
    company_name: str,
    domain: str = "",
    location: str = "",
    job_category: str = "",
    max_results: int = 20,
) -> list[dict]:
    """
    Find contacts for a company via Hunter.io domain-search.

    Requires either domain or company_name (Hunter resolves name → domain internally).
    Returns list of contact dicts with full_name, title, email, linkedin_url, source.
    """
    if not hunter_available():
        return []

    if not domain and not company_name:
        return []

    params: dict = {
        "api_key": HUNTER_API_KEY,
        "limit": min(max_results, 100),
        "offset": 0,
    }

    if domain:
        params["domain"] = domain
    else:
        params["company"] = company_name

    try:
        resp = requests.get(
            f"{HUNTER_BASE}/domain-search",
            params=params,
            timeout=15,
        )
    except Exception as e:
        logger.error(f"Hunter.io domain-search error: {e}")
        return []

    if resp.status_code == 401:
        logger.warning("Hunter.io: invalid or missing API key")
        return []
    if resp.status_code == 429:
        logger.warning("Hunter.io: rate limit hit")
        return []
    if resp.status_code != 200:
        logger.warning(f"Hunter.io: {resp.status_code} — {resp.text[:200]}")
        return []

    data = resp.json().get("data", {})
    emails = data.get("emails", [])
    org_domain = data.get("domain", domain)

    contacts = []
    for entry in emails[:max_results]:
        email = entry.get("value")
        if not email:
            continue

        # Skip generic/role emails
        local = email.split("@")[0].lower()
        if local in _GENERIC_LOCALS:
            continue

        first = entry.get("first_name") or ""
        last = entry.get("last_name") or ""
        full_name = f"{first} {last}".strip() or None
        if not full_name:
            continue

        # Only include entries with at least a first+last name
        if not first or not last:
            continue

        confidence = entry.get("confidence", 0)
        # Hunter confidence < 50 → unreliable, skip
        if confidence < 50:
            continue

        contacts.append({
            "full_name": full_name,
            "title": entry.get("position"),
            "email": email,
            "email_confidence": confidence,
            "phone": None,
            "linkedin_url": entry.get("linkedin"),
            "source": "hunter",
        })

    logger.info(f"Hunter.io '{domain or company_name}': {len(contacts)} contacts")
    return contacts


def find_hunter_email(
    first_name: str,
    last_name: str,
    domain: str,
) -> str | None:
    """
    Find work email for a specific person at a domain (email-finder endpoint).
    Costs 1 credit regardless of hit/miss.
    Returns email string or None.
    """
    if not hunter_available() or not domain:
        return None

    try:
        resp = requests.get(
            f"{HUNTER_BASE}/email-finder",
            params={
                "first_name": first_name,
                "last_name": last_name,
                "domain": domain,
                "api_key": HUNTER_API_KEY,
            },
            timeout=10,
        )
    except Exception as e:
        logger.debug(f"Hunter email-finder error: {e}")
        return None

    if resp.status_code != 200:
        return None

    data = resp.json().get("data", {})
    email = data.get("email")
    confidence = data.get("score", 0)

    if email and confidence >= 50:
        return email
    return None


_GENERIC_LOCALS = {
    "info", "kontakt", "contact", "office", "mail", "hello", "hallo",
    "service", "support", "sales", "booking", "reservations", "reception",
    "team", "events", "jobs", "career", "careers", "recruiting", "hr",
    "press", "media", "legal", "privacy", "marketing", "billing",
    "admin", "administration", "management", "general", "no-reply", "noreply",
}
