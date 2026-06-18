from __future__ import annotations
"""
People Data Labs (PDL) — person search API.

Free plan: 1,000 credits/month (1 credit per successful API call).
Person search: company name + title role (HR, recruiting, events, operations).

Docs: https://docs.peopledatalabs.com/docs/person-search-api
API key: https://dashboard.peopledatalabs.com/
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

PDL_API_KEY = os.getenv("PDL_API_KEY", "")
_PDL_BASE = "https://api.peopledatalabs.com/v5"
_REQUEST_TIMEOUT = 20


def pdl_available() -> bool:
    return bool(PDL_API_KEY)


def search_pdl_contacts(
    company_name: str,
    location: str = "",
    job_category: str = "",
    max_results: int = 10,
) -> list[dict]:
    """
    Search PDL Person Search API with company + title filters.

    Returned list is compatible with enricher.py format:
      {full_name, title, email, linkedin_url, source}
    """
    if not pdl_available():
        return []

    must_clauses = [
        # Exact company name match (PDL normalizes it internally)
        {"term": {"job_company_name": company_name.lower()}},
    ]

    # Ülke filtresi (DACH)
    country = _infer_country(location)
    if country:
        must_clauses.append({"term": {"location_country": country}})

    # Role filter based on job category
    roles = _roles_for_category(job_category)
    should_clauses = [{"term": {"job_title_role": r}} for r in roles]

    query = {"bool": {"must": must_clauses}}
    if should_clauses:
        query["bool"]["should"] = should_clauses
        query["bool"]["minimum_should_match"] = 1

    payload = {
        "query": query,
        "size": min(max_results, 25),
        "pretty": False,
    }

    headers = {
        "X-Api-Key": PDL_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        resp = requests.post(
            f"{_PDL_BASE}/person/search",
            json=payload,
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code == 402:
            logger.warning("PDL: insufficient credits (402)")
            return []
        if resp.status_code == 404:
            logger.info(f"PDL: no results for '{company_name}'")
            return []
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning(f"PDL request error for '{company_name}': {e}")
        return []

    contacts = []
    for person in data.get("data") or []:
        name = _join_name(
            person.get("full_name"),
            person.get("first_name"),
            person.get("last_name"),
        )
        if not name:
            continue

        # Skip if PDL's confidence score is too low
        if (person.get("likelihood") or 0) < 3:
            continue

        email = person.get("work_email") or _best_email(person.get("emails"))

        contacts.append({
            "full_name": name,
            "title": person.get("job_title"),
            "email": email,
            "linkedin_url": person.get("linkedin_url"),
            "source": "pdl",
        })

    logger.info(f"PDL '{company_name}': {len(contacts)} contacts found")
    return contacts


def _join_name(full: str | None, first: str | None, last: str | None) -> str:
    if full:
        return full.strip()
    parts = [p.strip() for p in [first, last] if p and p.strip()]
    return " ".join(parts) if len(parts) >= 2 else ""


def _best_email(emails: list | None) -> str | None:
    if not emails:
        return None
    for e in emails:
        if isinstance(e, dict):
            addr = e.get("address") or e.get("email")
            if addr:
                return addr
        if isinstance(e, str):
            return e
    return None


def _infer_country(location: str) -> str:
    loc = location.lower()
    if any(k in loc for k in ("germany", "deutschland", "hamburg", "berlin", "münchen",
                               "frankfurt", "köln", "düsseldorf", "stuttgart")):
        return "germany"
    if any(k in loc for k in ("austria", "österreich", "wien", "graz")):
        return "austria"
    if any(k in loc for k in ("switzerland", "schweiz", "zürich", "basel", "genf")):
        return "switzerland"
    return ""


def _roles_for_category(job_category: str) -> list[str]:
    """Map job_category to PDL role taxonomy. Returns all staffing-related roles if empty."""
    cat = (job_category or "").lower()

    if "event" in cat or "mice" in cat or "conference" in cat:
        return ["event planner", "operations", "human resources"]
    if "hr" in cat or "personal" in cat or "recruit" in cat or "talent" in cat:
        return ["human resources", "recruiting", "staffing and outsourcing"]
    if "staffing" in cat or "zeitarbeit" in cat or "workforce" in cat:
        return ["staffing and outsourcing", "human resources", "operations"]

    # Default: all roles relevant to a staffing agency
    return [
        "human resources",
        "recruiting",
        "staffing and outsourcing",
        "operations",
        "event planner",
    ]
