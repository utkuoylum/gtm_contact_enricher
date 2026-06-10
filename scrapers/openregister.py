from __future__ import annotations
"""
OpenRegister.de + OffeneRegister.de — German commercial register sources.

OpenRegister.de:
  - 4M+ Alman şirketi
  - Yönetim (Geschäftsführer, Vorstand, Prokurist) + ortaklık yapısı
  - İlk 50 sorgu ücretsiz (OPENREGISTER_API_KEY env var)
  - Kayıt gerektirmeden de bazı veriler döner

OffeneRegister SQL API:
  - 5M+ Alman şirketi (CC-BY lisanslı)
  - Ücretsiz SQL benzeri sorgu API'si (CORS açık)
  - Geschäftsführer adları ve pozisyonları
"""
import os
import re
import logging
import requests
from utils.http_client import REQUEST_TIMEOUT, polite_sleep, get_session, fetch_url
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

OPENREGISTER_API_KEY = os.getenv("OPENREGISTER_API_KEY", "")
OPENREGISTER_BASE = "https://api.openregister.de"
OFFENEREGISTER_API = "https://api.offeneregister.de"


def find_german_register_officers(company_name: str, location: str = "") -> list[dict]:
    """
    Query German commercial registers for company officers.
    Returns list of {full_name, title, source} dicts.
    """
    contacts = []

    # 1. OffeneRegister (free, no key, 5M+ companies)
    off_contacts = _query_offeneregister(company_name, location)
    contacts.extend(off_contacts)

    # 2. OpenRegister (50 free calls with key, broader data)
    if OPENREGISTER_API_KEY and len(contacts) < 3:
        or_contacts = _query_openregister(company_name, location)
        existing_names = {c["full_name"].lower() for c in contacts}
        for c in or_contacts:
            if c["full_name"].lower() not in existing_names:
                contacts.append(c)

    # 3. Northdata web fallback
    if not contacts:
        nd_contacts = _scrape_northdata_suggest(company_name)
        contacts.extend(nd_contacts)

    return contacts


def _query_offeneregister(company_name: str, location: str) -> list[dict]:
    """
    OffeneRegister SQL API (free, CORS-enabled).
    Endpoint: https://api.offeneregister.de/companies?name=...
    """
    contacts = []
    city = location.split(",")[0].strip() if location else ""

    try:
        params = {"name": company_name}
        if city:
            params["registered_address"] = city

        resp = requests.get(
            f"{OFFENEREGISTER_API}/companies",
            params=params,
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json", "User-Agent": "ContactEnrichmentBot/1.0"},
        )

        if resp.status_code == 200:
            data = resp.json()
            companies = data if isinstance(data, list) else data.get("results", [])

            for company in companies[:3]:
                officers = company.get("officers", [])
                for officer in officers:
                    name = officer.get("name", "")
                    if not name or len(name.split()) < 2:
                        continue
                    position = officer.get("position", "Geschäftsführer")
                    # Skip resigned/historical
                    if officer.get("end_date"):
                        continue
                    contacts.append({
                        "full_name": _fix_german_name(name),
                        "title": _translate_german_position(position),
                        "email": None,
                        "phone": None,
                        "source": "offeneregister",
                    })
                if contacts:
                    break

    except Exception as e:
        logger.debug(f"OffeneRegister error: {e}")

    return contacts


def _query_openregister(company_name: str, location: str) -> list[dict]:
    """
    OpenRegister.de API (requires OPENREGISTER_API_KEY).
    Returns management + ownership structure for German companies.
    """
    contacts = []
    headers = {
        "Authorization": f"Bearer {OPENREGISTER_API_KEY}",
        "Accept": "application/json",
    }

    try:
        # Search for company
        resp = requests.get(
            f"{OPENREGISTER_BASE}/company",
            params={"q": company_name, "country": "de"},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return []

        results = resp.json().get("results", [])
        if not results:
            return []

        company_id = results[0].get("id")
        if not company_id:
            return []

        polite_sleep(0.5)

        # Get company details with officers
        detail_resp = requests.get(
            f"{OPENREGISTER_BASE}/company/{company_id}",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if detail_resp.status_code != 200:
            return []

        detail = detail_resp.json()
        for officer in detail.get("management", []):
            name = officer.get("name", "")
            if len(name.split()) < 2:
                continue
            role = officer.get("role", "Geschäftsführer")
            contacts.append({
                "full_name": _fix_german_name(name),
                "title": _translate_german_position(role),
                "email": None,
                "phone": None,
                "source": "openregister",
            })

    except Exception as e:
        logger.debug(f"OpenRegister error: {e}")

    return contacts


def _scrape_northdata_suggest(company_name: str) -> list[dict]:
    """
    Northdata.com suggest endpoint — free autocomplete API.
    Returns company names + basic info including officer names in some cases.
    """
    contacts = []
    session = get_session()

    try:
        resp = requests.get(
            "https://www.northdata.com/_api/v1/suggest",
            params={"query": company_name, "language": "de"},
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; ContactEnrichment/1.0)",
                "Referer": "https://www.northdata.com/",
            },
            timeout=REQUEST_TIMEOUT,
        )

        if resp.status_code == 200:
            suggestions = resp.json()
            for item in suggestions[:3]:
                # Some suggestions include person names
                if item.get("type") == "person":
                    name = item.get("name", "")
                    company = item.get("company", "")
                    role = item.get("role", "")
                    if name and len(name.split()) >= 2:
                        contacts.append({
                            "full_name": _fix_german_name(name),
                            "title": _translate_german_position(role) if role else "Geschäftsführer",
                            "email": None,
                            "phone": None,
                            "source": "northdata_suggest",
                        })
    except Exception as e:
        logger.debug(f"Northdata suggest error: {e}")

    # Fallback: scrape Northdata search page
    if not contacts:
        contacts = _scrape_northdata_web(company_name, session)

    return contacts


def _scrape_northdata_web(company_name: str, session) -> list[dict]:
    """Scrape Northdata.com search results page (pre-rendered HTML)."""
    from urllib.parse import quote_plus
    contacts = []

    url = f"https://www.northdata.com/search?q={quote_plus(company_name)}&language=de"
    html = fetch_url(url, session)
    if not html:
        return []

    polite_sleep(0.8)
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")

    # Pattern: role label followed by name on same or next line
    role_patterns = [
        (r"Gesch[äa]ftsf[üu]hrer(?:in)?\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+)", "Geschäftsführer"),
        (r"Prokurist(?:in)?\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+)", "Prokurist"),
        (r"Vorstand\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+)", "Vorstand"),
        (r"Inhaber(?:in)?\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+)", "Inhaber"),
    ]

    for pattern, role in role_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            name = match.group(1).strip()
            if len(name.split()) >= 2:
                contacts.append({
                    "full_name": name,
                    "title": role,
                    "email": None,
                    "phone": None,
                    "source": "northdata_web",
                })

    return contacts[:5]


def _fix_german_name(name: str) -> str:
    """Convert 'MUELLER, Hans' → 'Hans Mueller' format."""
    if "," in name:
        parts = name.split(",", 1)
        last = parts[0].strip().title()
        first = parts[1].strip().title()
        return f"{first} {last}"
    return name.title()


def _translate_german_position(position: str) -> str:
    """Keep German titles as-is (they're useful for rating), but clean format."""
    mapping = {
        "geschaftsfuhrer": "Geschäftsführer",
        "geschaeftsfuehrer": "Geschäftsführer",
        "managing_director": "Geschäftsführer",
        "managing director": "Geschäftsführer",
        "prokurist": "Prokurist",
        "vorstand": "Vorstand",
        "inhaber": "Inhaber",
        "gesellschafter": "Gesellschafter",
        "director": "Director",
    }
    pos_lower = (position or "").lower().replace("-", "_").replace(" ", "_")
    return mapping.get(pos_lower, position.title() if position else "Geschäftsführer")
