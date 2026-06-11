from __future__ import annotations
"""
Multi-engine contact search via SERP.

Strategy:
  - Search Google/Bing/DDG for executives by name+title
  - Extract emails only when found verbatim in SERP snippets (no guessing)
  - Use OpenCorporates as a structured fallback for officer data
  - Bing/DDG searched first to reduce Google blocking
"""
import re
import logging
from urllib.parse import quote_plus, urlparse
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep, multi_engine_search
from utils.domain_finder import extract_email_from_text, extract_phone_from_text

logger = logging.getLogger(__name__)

_EXEC_ROLES = [
    "CEO", "Founder", "Co-Founder", "Managing Director", "MD",
    "Owner", "General Manager", "HR Director", "Chief People Officer",
    "CHRO", "CPO", "VP HR", "Head of HR", "HR Manager",
    "Talent Acquisition", "Recruiting Manager", "Head of Talent",
    "Chief Executive", "President", "Director",
]

_ROLE_PATTERN = re.compile(
    r"([A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)+)"  # Name (incl. German umlauts)
    r",?\s*[-–]?\s*"
    r"\b(CEO|CFO|CTO|COO|CHRO|CPO|Founder|Co-Founder|Managing Director|MD|"
    r"Chief Executive|President|Director|VP|Vice President|"
    r"HR Director|HR Manager|Head of HR|Chief People Officer|"
    r"Talent Acquisition|Owner|Partner|Head of \w+|"
    # German roles
    r"Gesch[äa]ftsf[üu]hrer(?:in)?|Inhaber(?:in)?|Prokurist(?:in)?|"
    r"Gesellschafter(?:in)?|Vorstand|Gr[üu]nder(?:in)?|Eigent[üu]mer(?:in)?|"
    r"Personalleiter(?:in)?|HR-Leiter(?:in)?|Personalreferent(?:in)?)",
    re.IGNORECASE,
)


def google_contact_search(company_name: str, location: str = "", domain: str = "") -> list[dict]:
    contacts = []
    session = get_session()
    seen: set[str] = set()

    queries = _build_queries(company_name, location, domain)

    for query in queries:
        html = multi_engine_search(query, session)
        if not html:
            continue
        new_contacts = _extract_contacts_from_serp(html, company_name, domain)
        for c in new_contacts:
            key = c.get("full_name", "").lower()
            if key and key not in seen:
                seen.add(key)
                contacts.append(c)
        polite_sleep(1.2)
        if len(contacts) >= 10:
            break

    return contacts[:10]


_DACH_INDICATORS = {
    "de", "at", "ch", "germany", "deutschland", "austria", "österreich",
    "switzerland", "schweiz", "hamburg", "berlin", "münchen", "munich",
    "frankfurt", "köln", "cologne", "düsseldorf", "stuttgart", "hannover",
    "wien", "vienna", "zürich", "zurich", "dach",
}


def _is_dach(location: str, domain: str) -> bool:
    loc_lower = location.lower()
    if any(indicator in loc_lower for indicator in _DACH_INDICATORS):
        return True
    if domain:
        tld = domain.lower().split(".")[-1]
        return tld in ("de", "at", "ch")
    return False


def _build_queries(company_name: str, location: str, domain: str) -> list[str]:
    queries = []
    name_q = f'"{company_name}"'
    dach = _is_dach(location, domain)

    # Site-specific search (most targeted)
    if domain:
        queries.append(f'site:{domain} contact OR team OR about')

    if dach:
        # German-specific: most likely to find Geschäftsführer, Inhaber, etc.
        queries.append(f'{name_q} Geschäftsführer OR Inhaber OR Prokurist Kontakt')
        queries.append(f'{name_q} Personalleiter OR "HR Manager" OR Personalreferent')
        # Email pattern on German domain
        if domain:
            queries.append(f'"@{domain}" {name_q}')
        # SERP for Impressum info
        queries.append(f'{name_q} Impressum Geschäftsführer')
    else:
        loc = f' "{location}"' if location else ""
        # Executive search (English)
        queries.append(f'{name_q} "Managing Director" OR "CEO" OR "Founder" email{loc}')
        queries.append(f'{name_q} "HR Director" OR "HR Manager" OR "Head of HR" contact{loc}')
        queries.append(f'{name_q} executive team contact{loc}')
        if domain:
            queries.append(f'"@{domain}" {name_q}')
        else:
            queries.append(f'{name_q} "@" email contact{loc}')

    return queries


def _extract_contacts_from_serp(html: str, company_name: str, domain: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ")
    contacts = []

    # Only extract emails that literally appear in the page text
    emails = extract_email_from_text(text)
    phones = extract_phone_from_text(text)

    # Filter emails: must match domain if known, or at least not be from huge providers
    filtered_emails = []
    throwaway = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                 "icloud.com", "protonmail.com", "aol.com"}
    for email in emails:
        email_domain = email.split("@")[-1].lower()
        if domain and email_domain == domain:
            filtered_emails.append(email)
        elif not domain and email_domain not in throwaway:
            filtered_emails.append(email)

    # Try to pair email with a name from surrounding context
    for email in filtered_emails[:5]:
        local = email.split("@")[0]
        name = _name_from_local(local)
        title = _find_title_near_email(text, email)
        contacts.append({
            "full_name": name,
            "email": email,
            "title": title,
            "phone": phones[0] if phones else None,
            "source": "google_serp",
        })

    # Also extract Name+Role pairs from text (even without email)
    _BAD_NAME_WORDS = {
        "our", "the", "new", "all", "your", "this", "or", "and", "not",
        "inhaber", "prokurist", "geschäftsführer", "vorstand",
        "service", "support", "kontakt", "unsere", "unser",
        # Title keywords that appear as name fragments when regex splits mid-word
        "managing", "chief", "head", "director", "vice", "senior",
        "executive", "officer", "president", "general", "regional",
    }
    for match in _ROLE_PATTERN.finditer(text):
        name = match.group(1).strip()
        role = match.group(2).strip()
        parts = name.split()
        if len(parts) < 2 or len(parts) > 4:
            continue
        # Reject if any part is ALL CAPS (boolean op like OR, AND) or a known bad word
        if any(w.upper() == w and len(w) <= 4 for w in parts):
            continue
        # All parts must start with actual uppercase (IGNORECASE regex bypass guard)
        if not all(p[0].isupper() for p in parts):
            continue
        if any(w.lower() in _BAD_NAME_WORDS for w in parts):
            continue
        contacts.append({
            "full_name": name,
            "title": role,
            "email": None,
            "phone": None,
            "source": "google_serp",
        })

    return contacts


def _name_from_local(local: str) -> str:
    """'john.smith' → 'John Smith'. Don't guess if pattern unclear."""
    parts = re.split(r"[._\-]", local)
    # Filter out single chars and numbers
    parts = [p for p in parts if len(p) > 1 and p.isalpha()]
    if len(parts) >= 2:
        return " ".join(p.capitalize() for p in parts[:3])
    return local.capitalize()


def _find_title_near_email(text: str, email: str) -> str | None:
    idx = text.find(email)
    if idx == -1:
        return None
    context = text[max(0, idx - 200): idx + 100]
    for role in _EXEC_ROLES:
        if role.lower() in context.lower():
            ki = context.lower().index(role.lower())
            snippet = context[max(0, ki - 5): ki + len(role) + 50].strip()
            return re.sub(r"\s+", " ", snippet)[:80]
    return None


def scrape_crunchbase_people(company_name: str) -> list[dict]:
    """
    Crunchbase is heavily JS-rendered; static HTML rarely contains useful data.
    Instead, search Bing/DDG for Crunchbase people page and extract from snippet.
    """
    session = get_session()
    contacts = []
    query = f'site:crunchbase.com "{company_name}" people OR team'
    html = multi_engine_search(query, session)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ")

    for match in _ROLE_PATTERN.finditer(text):
        name = match.group(1).strip()
        role = match.group(2).strip()
        if len(name.split()) >= 2:
            contacts.append({
                "full_name": name,
                "title": role,
                "email": None,
                "phone": None,
                "source": "crunchbase",
            })
        if len(contacts) >= 5:
            break

    return contacts
