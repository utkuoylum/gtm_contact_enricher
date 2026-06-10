from __future__ import annotations
"""
Hunter.io API integration.
Free tier: 25 searches/month.
Paid: $49/mo (500 searches), $99/mo (2000 searches).

Endpoints used:
  - /domain-search: find all emails for a domain
  - /email-finder: find specific person's email
  - /email-verifier: verify a specific email (free: 50/mo)
"""
import logging
import requests
from config import HUNTER_API_KEY, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)
BASE_URL = "https://api.hunter.io/v2"


def _get(endpoint: str, params: dict) -> dict | None:
    if not HUNTER_API_KEY:
        logger.info("Hunter.io API key not set — skipping")
        return None
    params["api_key"] = HUNTER_API_KEY
    try:
        resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            logger.warning("Hunter.io rate limit hit")
        elif resp.status_code == 403:
            logger.warning("Hunter.io quota exhausted for this month")
        else:
            logger.warning(f"Hunter.io {endpoint} returned {resp.status_code}")
    except requests.RequestException as e:
        logger.error(f"Hunter.io request error: {e}")
    return None


def domain_search(domain: str, limit: int = 10) -> dict:
    """
    Returns { email_pattern, contacts: [{first, last, email, position, ...}] }
    """
    result = _get("domain-search", {"domain": domain, "limit": limit, "type": "personal"})
    if not result:
        return {"email_pattern": None, "contacts": []}

    data = result.get("data", {})
    pattern = data.get("pattern")  # e.g. "{first}.{last}"
    raw_emails = data.get("emails", [])

    contacts = []
    for item in raw_emails:
        email = item.get("value")
        if not email:
            continue
        first = item.get("first_name", "")
        last = item.get("last_name", "")
        full_name = f"{first} {last}".strip()
        contacts.append({
            "full_name": full_name or email.split("@")[0],
            "email": email,
            "title": item.get("position"),
            "confidence": item.get("confidence", 0),
            "source": "hunter_domain",
            "phone": None,
            "linkedin_url": item.get("linkedin"),
        })

    return {"email_pattern": pattern, "contacts": contacts}


def email_finder(domain: str, first_name: str, last_name: str) -> dict | None:
    """Find email for a specific person. Returns {email, score} or None."""
    result = _get("email-finder", {
        "domain": domain,
        "first_name": first_name,
        "last_name": last_name,
    })
    if not result:
        return None
    data = result.get("data", {})
    email = data.get("email")
    if not email:
        return None
    return {
        "email": email,
        "score": data.get("score", 0),
    }


def verify_email(email: str) -> dict | None:
    """Verify a single email. Free: 50/month."""
    result = _get("email-verifier", {"email": email})
    if not result:
        return None
    data = result.get("data", {})
    return {
        "status": data.get("status"),      # valid, invalid, accept_all, unknown
        "score": data.get("score", 0),
        "disposable": data.get("disposable", False),
        "webmail": data.get("webmail", False),
        "mx_records": data.get("mx_records", False),
        "smtp_server": data.get("smtp_server", False),
        "smtp_check": data.get("smtp_check", False),
    }
