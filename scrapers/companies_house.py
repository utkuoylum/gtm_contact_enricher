from __future__ import annotations
"""
UK Companies House scraper — two data sources:

1. Companies House official REST API (free, registration at developer.company-information.service.gov.uk)
   - Key set via COMPANIES_HOUSE_API_KEY env var
   - Returns directors, secretaries with names and appointment dates

2. OpenCorporates (free, no key required for basic use)
   - Global coverage: UK, DE, FR, IT, NL, ES, US states, AU, etc.
   - Returns officers (directors, managing directors, etc.)

Both sources are authoritative — these are legal registration records.
"""
import os
import re
import logging
import requests
from utils.http_client import REQUEST_TIMEOUT, polite_sleep

logger = logging.getLogger(__name__)

COMPANIES_HOUSE_API_KEY = os.getenv("COMPANIES_HOUSE_API_KEY", "")
CH_BASE = "https://api.company-information.service.gov.uk"
OC_BASE = "https://api.opencorporates.com/v0.4"

# Map location keywords → OpenCorporates jurisdiction codes
_JURISDICTION_MAP = {
    "uk": "gb", "united kingdom": "gb", "england": "gb", "wales": "gb",
    "scotland": "gb", "london": "gb", "manchester": "gb", "birmingham": "gb",
    "germany": "de", "deutschland": "de", "hamburg": "de", "berlin": "de",
    "munich": "de", "münchen": "de", "frankfurt": "de", "cologne": "de",
    "france": "fr", "paris": "fr", "lyon": "fr",
    "italy": "it", "milan": "it", "rome": "it",
    "spain": "es", "madrid": "es", "barcelona": "es",
    "netherlands": "nl", "amsterdam": "nl",
    "austria": "at", "vienna": "at", "wien": "at",
    "switzerland": "ch", "zürich": "ch", "zurich": "ch",
    "australia": "au", "sydney": "au", "melbourne": "au",
    "usa": "us_de", "united states": "us_de",
    "turkey": "tr", "istanbul": "tr",
    "poland": "pl", "warsaw": "pl",
}


def _jurisdiction_from_location(location: str) -> str | None:
    loc_lower = location.lower()
    for key, code in _JURISDICTION_MAP.items():
        if key in loc_lower:
            return code
    return None


def find_company_officers(company_name: str, location: str = "") -> list[dict]:
    """
    Returns list of officers/directors from official registry sources.
    Tries Companies House first (UK), then OpenCorporates.
    """
    contacts = []

    # 1. Companies House (UK only, best quality)
    if COMPANIES_HOUSE_API_KEY or _is_uk(location):
        ch_contacts = _companies_house_search(company_name, location)
        contacts.extend(ch_contacts)

    # 2. OpenCorporates (global)
    if len(contacts) < 3:
        oc_contacts = _opencorporates_search(company_name, location)
        # Deduplicate by name
        existing_names = {c["full_name"].lower() for c in contacts}
        for c in oc_contacts:
            if c["full_name"].lower() not in existing_names:
                contacts.append(c)

    return contacts


def _is_uk(location: str) -> bool:
    uk_kws = ["uk", "united kingdom", "england", "wales", "scotland",
               "london", "manchester", "birmingham", "leeds", "glasgow"]
    loc = location.lower()
    return any(k in loc for k in uk_kws)


def _companies_house_search(company_name: str, location: str) -> list[dict]:
    """Search Companies House REST API."""
    if not COMPANIES_HOUSE_API_KEY and not _is_uk(location):
        return []

    auth = (COMPANIES_HOUSE_API_KEY, "") if COMPANIES_HOUSE_API_KEY else None
    contacts = []

    try:
        # Step 1: Find company number
        resp = requests.get(
            f"{CH_BASE}/search/companies",
            params={"q": company_name, "items_per_page": 5},
            auth=auth,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return []

        companies = resp.json().get("items", [])
        if not companies:
            return []

        # Take best match
        company_number = companies[0].get("company_number")
        company_title = companies[0].get("title", company_name)
        if not company_number:
            return []

        polite_sleep(0.5)

        # Step 2: Fetch officers
        resp2 = requests.get(
            f"{CH_BASE}/company/{company_number}/officers",
            params={"items_per_page": 30},
            auth=auth,
            timeout=REQUEST_TIMEOUT,
        )
        if resp2.status_code != 200:
            return []

        officers = resp2.json().get("items", [])
        for officer in officers:
            # Skip resigned officers
            if officer.get("resigned_on"):
                continue
            name = officer.get("name", "")
            if not name:
                continue
            # CH returns "LASTNAME, Firstname" — fix it
            name = _fix_ch_name(name)
            role = officer.get("officer_role", "director").replace("_", " ").title()
            contacts.append({
                "full_name": name,
                "title": role,
                "email": None,
                "phone": None,
                "source": "companies_house",
                "company": company_title,
            })

    except Exception as e:
        logger.debug(f"Companies House error: {e}")

    return contacts


def _fix_ch_name(name: str) -> str:
    """Convert 'SMITH, John Robert' → 'John Robert Smith'."""
    if "," in name:
        parts = name.split(",", 1)
        last = parts[0].strip().title()
        first = parts[1].strip().title()
        return f"{first} {last}"
    return name.title()


def _opencorporates_search(company_name: str, location: str) -> list[dict]:
    """Search OpenCorporates for company officers."""
    jurisdiction = _jurisdiction_from_location(location)
    contacts = []

    try:
        params = {"q": company_name, "per_page": 5}
        if jurisdiction:
            params["jurisdiction_code"] = jurisdiction

        resp = requests.get(
            f"{OC_BASE}/companies/search",
            params=params,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "ContactEnrichmentBot/1.0"},
        )
        if resp.status_code != 200:
            return []

        results = resp.json().get("results", {}).get("companies", [])
        if not results:
            return []

        # Take best match
        company_data = results[0].get("company", {})
        company_number = company_data.get("company_number")
        company_juris = company_data.get("jurisdiction_code", jurisdiction or "gb")
        company_title = company_data.get("name", company_name)

        if not company_number:
            return []

        polite_sleep(0.5)

        # Fetch officers
        resp2 = requests.get(
            f"{OC_BASE}/companies/{company_juris}/{company_number}/officers",
            params={"per_page": 20},
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "ContactEnrichmentBot/1.0"},
        )
        if resp2.status_code != 200:
            return []

        officers = resp2.json().get("results", {}).get("officers", [])
        for item in officers:
            officer = item.get("officer", {})
            # Skip inactive
            if officer.get("end_date"):
                continue
            name = officer.get("name", "")
            if not name or len(name.split()) < 2:
                continue
            position = officer.get("position", "Director")
            contacts.append({
                "full_name": name.title(),
                "title": position.title() if position else "Director",
                "email": None,
                "phone": None,
                "source": "opencorporates",
                "company": company_title,
            })

    except Exception as e:
        logger.debug(f"OpenCorporates error: {e}")

    return contacts
