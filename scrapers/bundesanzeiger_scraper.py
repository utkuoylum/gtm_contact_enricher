from __future__ import annotations
"""
Bundesanzeiger scraper — official German federal gazette.

Extracts Geschäftsführer / Prokurist names from Handelsregister registration announcements.
Valuable for recent appointments not yet indexed by Northdata.

Approach:
  1. SERP search: site:bundesanzeiger.de "{company}" Geschäftsführer
  2. Matching announcement pages are read via Jina
  3. Registration texts are parsed with regex
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, multi_engine_search, polite_sleep

try:
    from utils.http_client import fetch_with_jina
    _JINA_OK = True
except ImportError:
    _JINA_OK = False

logger = logging.getLogger(__name__)

_BANZ_DOMAIN = "bundesanzeiger.de"

# Officer roles found in registration texts
_OFFICER_ROLES = re.compile(
    r"\b(Geschäftsführer(?:in)?|Prokurist(?:in)?|Vorstand(?:svorsitzender|svorsitzende)?|"
    r"Inhaber(?:in)?|Gesellschafter(?:in)?|Gründer(?:in)?|Liquidator(?:in)?)\b",
    re.IGNORECASE,
)

# "Geschäftsführer: First Last" or "First Last, City, *DD.MM.YYYY"
_NAME_AFTER_ROLE = re.compile(
    r"(?:Geschäftsführer(?:in)?|Prokurist(?:in)?|Vorstand(?:svorsitzender)?|"
    r"Inhaber(?:in)?|Liquidator(?:in)?)"
    r"[:\s]+(?:(?:Dr\.|Prof\.|Dipl\.)\s+)?"
    r"([A-ZÜÖÄ][a-züöäß\-]+(?:\s+[a-züöäß\-]+)?\s+[A-ZÜÖÄ][a-züöäß\-]+(?:\s+[A-ZÜÖÄ][a-züöäß\-]+)?)",
    re.IGNORECASE,
)

# Role following the name: "Max Mustermann, Geschäftsführer"
_ROLE_AFTER_NAME = re.compile(
    r"([A-ZÜÖÄ][a-züöäß\-]+(?:\s+[a-züöäß\-]+)?\s+[A-ZÜÖÄ][a-züöäß\-]+)"
    r",\s*(?:geb\.\s*[\d.]+,\s*)?(?:\w+,\s*)?"
    r"(Geschäftsführer(?:in)?|Prokurist(?:in)?|Vorstand|Inhaber(?:in)?)",
    re.IGNORECASE,
)


def find_bundesanzeiger_contacts(company_name: str, location: str = "") -> list[dict]:
    """Search Bundesanzeiger for company registration announcements and return officers."""
    session = get_session()
    contacts: list[dict] = []
    seen_names: set[str] = set()

    # 1. Find Bundesanzeiger announcement URLs via SERP
    query = f'site:{_BANZ_DOMAIN} "{company_name}" Geschäftsführer'
    html = multi_engine_search(query, session)
    if not html:
        return []

    banz_urls = _extract_banz_urls(html, company_name)
    polite_sleep(1.5)

    # 2. Fetch and parse announcement texts
    for url in banz_urls[:4]:
        try:
            page_text = _fetch_announcement_text(url, session)
            if not page_text:
                continue
            for person in _extract_officers(page_text, company_name):
                name_key = person["full_name"].lower()
                if name_key not in seen_names:
                    seen_names.add(name_key)
                    contacts.append(person)
        except Exception as e:
            logger.debug(f"Bundesanzeiger page fetch error ({url}): {e}")
        polite_sleep(1.0)

    # 3. If no announcement URLs found, extract from SERP snippets
    if not contacts and html:
        soup = BeautifulSoup(html, "html.parser")
        serp_text = soup.get_text(separator="\n")
        for person in _extract_officers(serp_text, company_name):
            name_key = person["full_name"].lower()
            if name_key not in seen_names:
                seen_names.add(name_key)
                contacts.append(person)

    logger.info(f"Bundesanzeiger '{company_name}': {len(contacts)} officer(s) found")
    return contacts


def _extract_banz_urls(html: str, company_name: str) -> list[str]:
    """Extract bundesanzeiger.de URLs from SERP HTML."""
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    company_lower = company_name.lower()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _BANZ_DOMAIN in href and "pub/de" in href:
            # Prioritize if URL or link text contains a company name word
            link_text = a.get_text(separator=" ").lower()
            if any(w in link_text or w in href.lower()
                   for w in company_lower.split() if len(w) > 3):
                urls.insert(0, href)
            else:
                urls.append(href)
    return list(dict.fromkeys(urls))  # deduplicate, preserve order


def _fetch_announcement_text(url: str, session) -> str:
    """Fetch announcement page as plain text (Jina first, HTTP fallback)."""
    if _JINA_OK:
        try:
            text = fetch_with_jina(url)
            if text and len(text) > 100:
                return text
        except Exception:
            pass
    html = fetch_url(url, session)
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n")


def _extract_officers(text: str, company_name: str) -> list[dict]:
    """Extract officer names from a registration announcement text."""
    contacts = []
    seen: set[str] = set()

    # Narrow to the section mentioning the company — avoid names from wrong companies
    company_words = [w.lower() for w in company_name.split() if len(w) > 3]
    relevant_text = text
    for i, line in enumerate(text.splitlines()):
        if any(w in line.lower() for w in company_words):
            # Take 30 lines after the company name mention
            relevant_text = "\n".join(text.splitlines()[max(0, i-2):i+30])
            break

    # Pattern: "Geschäftsführer: Vorname Nachname"
    for m in _NAME_AFTER_ROLE.finditer(relevant_text):
        name = _clean_name(m.group(1))
        if name and name.lower() not in seen:
            seen.add(name.lower())
            role = _extract_role_word(m.group(0))
            contacts.append({
                "full_name": name,
                "title": role,
                "source": "bundesanzeiger",
            })

    # Pattern: "Vorname Nachname, Geschäftsführer"
    for m in _ROLE_AFTER_NAME.finditer(relevant_text):
        name = _clean_name(m.group(1))
        role = m.group(2)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            contacts.append({
                "full_name": name,
                "title": role,
                "source": "bundesanzeiger",
            })

    return contacts


def _clean_name(raw: str) -> str:
    name = raw.strip()
    # Strip trailing birth date, city, "geb." artifacts
    name = re.sub(r",.*$", "", name).strip()
    name = re.sub(r"\s+", " ", name)
    # Must be at least two words, each starting with an uppercase letter
    parts = name.split()
    if len(parts) < 2 or len(parts) > 4:
        return ""
    if not all(p[0].isupper() for p in parts if p.lower() not in {"von", "van", "de", "der"}):
        return ""
    return name


def _extract_role_word(text: str) -> str:
    m = _OFFICER_ROLES.search(text)
    return m.group(1) if m else "Geschäftsführer"
