from __future__ import annotations
"""
Kununu.com — German/Austrian employee review platform.

Two valuable data points:
  1. HR person names from employer response signatures
     ("Mit freundlichen Grüßen, Maria Schmidt, HR-Leitung")
  2. Employee count from the company profile page (fallback when Gemini unavailable)

Scraping approach:
  - Search: site:kununu.com "{company}" → find company profile slug
  - Fetch profile + review response pages
  - Extract HR persons via signature regexes
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep, multi_engine_search

logger = logging.getLogger(__name__)

_KUNUNU_BASE = "https://www.kununu.com"

# Employer response signature patterns
# "Viele Grüße, Anna Müller, Head of HR" | "Dein HR-Team, Max Meier"
_SIGNATURE_PATTERNS = [
    re.compile(
        r"(?:Grüße?|Gruß|Gruss|Regards?|sincerely)[,\s]+"
        r"(?:(?:Dr\.|Prof\.)\s+)?"
        r"([A-ZÜÖÄ][a-züöäß\-]+\s+[A-ZÜÖÄ][a-züöäß\-]+(?:\s+[A-ZÜÖÄ][a-züöäß\-]+)?)"
        r"(?:[,\s]+([A-Za-züöäßÜÖÄ &\-]{5,50}))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"([A-ZÜÖÄ][a-züöäß\-]+\s+[A-ZÜÖÄ][a-züöäß\-]+(?:\s+[A-ZÜÖÄ][a-züöäß\-]+)?)"
        r"[,\s]+"
        r"((?:HR|Human Resources|People|Recruiting|Talent|Personal|Veranstaltung)[^\n]{0,50})",
        re.IGNORECASE,
    ),
]

# Employee count block: "51-200 Mitarbeiter"
_EMPLOYEE_RANGE_PATTERN = re.compile(
    r"(\d[\d.,]*)\s*[-–]\s*(\d[\d.,]*)\s*(?:Mitarbeiter|Beschäftigte|employees?)",
    re.IGNORECASE,
)
_EMPLOYEE_SINGLE_PATTERN = re.compile(
    r"ca\.?\s*(\d[\d.,]+)\s*(?:Mitarbeiter|Beschäftigte|employees?)",
    re.IGNORECASE,
)


def find_kununu_contacts(company_name: str, location: str = "") -> list[dict]:
    """
    Scrape employee/HR profiles from Kununu.
    Also returns employee count in a metadata field on each contact.
    """
    session = get_session()
    contacts: list[dict] = []
    seen: set[str] = set()

    slug_url = _find_company_profile_url(company_name, location, session)
    if not slug_url:
        return []
    polite_sleep(1.5)

    # Company main profile page (employee count + sometimes HR name)
    profile_text = _fetch_page_text(slug_url, session)
    if profile_text:
        employee_count = _parse_employee_count(profile_text)
        for person in _extract_contacts_from_text(profile_text, company_name):
            key = person["full_name"].lower()
            if key not in seen:
                seen.add(key)
                if employee_count:
                    person["_kununu_employee_count"] = employee_count
                contacts.append(person)

    # Review response page
    review_url = slug_url.rstrip("/") + "/kommentare"
    polite_sleep(1.5)
    review_text = _fetch_page_text(review_url, session)
    if review_text:
        for person in _extract_contacts_from_text(review_text, company_name):
            key = person["full_name"].lower()
            if key not in seen:
                seen.add(key)
                contacts.append(person)

    logger.info(f"Kununu '{company_name}': {len(contacts)} contacts found")
    return contacts


def _find_company_profile_url(company_name: str, location: str, session) -> str:
    """Find the kununu.com company profile URL via SERP."""
    loc_part = f" {location}" if location else ""
    query = f'site:kununu.com/de "{company_name}"{loc_part}'
    html = multi_engine_search(query, session)
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    company_lower = company_name.lower()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Profile URL: /de/something (skip search or job-listing pages)
        if ("kununu.com/de/" in href
                and "/suche" not in href
                and "/jobs" not in href
                and href.count("/") <= 5):
            link_text = a.get_text(separator=" ").lower()
            if any(w in link_text for w in company_lower.split() if len(w) > 3):
                # Build full URL if relative
                if href.startswith("http"):
                    return href
                return f"{_KUNUNU_BASE}{href}"

    return ""


def _fetch_page_text(url: str, session) -> str:
    html = fetch_url(url, session)
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n")


def _extract_contacts_from_text(text: str, company_name: str) -> list[dict]:
    contacts = []
    seen: set[str] = set()

    for pattern in _SIGNATURE_PATTERNS:
        for m in pattern.finditer(text):
            name = _clean_name(m.group(1))
            if not name or name.lower() in seen:
                continue
            title = _clean_title(m.group(2)) if m.lastindex and m.lastindex >= 2 else None
            seen.add(name.lower())
            contacts.append({
                "full_name": name,
                "title": title,
                "source": "kununu",
            })

    return contacts


def _clean_name(raw: str) -> str:
    if not raw:
        return ""
    name = raw.strip()
    name = re.sub(r"\s+", " ", name)
    parts = name.split()
    if len(parts) < 2 or len(parts) > 4:
        return ""
    particles = {"von", "van", "de", "der", "den"}
    if not all(p[0].isupper() for p in parts if p.lower() not in particles):
        return ""
    # Reject common generic words that appear in names
    _bad = {"team", "hr", "ihr", "ihr", "ihr", "dein", "ihr", "das", "die", "der"}
    if any(p.lower() in _bad for p in parts):
        return ""
    return name


def _clean_title(raw: str | None) -> str | None:
    if not raw:
        return None
    title = raw.strip().strip(",").strip()
    if len(title) < 3 or len(title) > 80:
        return None
    return title


def _parse_employee_count(text: str) -> int | None:
    m = _EMPLOYEE_SINGLE_PATTERN.search(text)
    if m:
        try:
            return int(m.group(1).replace(".", "").replace(",", ""))
        except ValueError:
            pass
    m = _EMPLOYEE_RANGE_PATTERN.search(text)
    if m:
        try:
            low = int(m.group(1).replace(".", "").replace(",", ""))
            high = int(m.group(2).replace(".", "").replace(",", ""))
            return (low + high) // 2
        except ValueError:
            pass
    return None
