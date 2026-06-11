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

    # Lazy import to avoid circular imports
    try:
        from utils.claude_extractor import extract_contacts_from_serp, claude_available
        _claude_ok = claude_available()
    except Exception:
        extract_contacts_from_serp = None  # type: ignore[assignment]
        _claude_ok = False

    queries = _build_queries(company_name, location, domain)

    for query in queries:
        html = multi_engine_search(query, session)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")
        serp_text = soup.get_text(separator=" ")

        # --- Claude primary extraction ---
        claude_contacts: list[dict] = []
        if _claude_ok and extract_contacts_from_serp is not None:
            try:
                claude_contacts = extract_contacts_from_serp(serp_text, company_name, location)
            except Exception:
                claude_contacts = []

        # --- Regex supplementary extraction ---
        regex_contacts = _extract_contacts_from_serp(html, company_name, domain)

        # Merge: Claude first, then regex results not already seen by name
        claude_names = {c.get("full_name", "").lower() for c in claude_contacts if c.get("full_name")}
        combined = claude_contacts + [c for c in regex_contacts if c.get("full_name", "").lower() not in claude_names]

        for c in combined:
            key = c.get("full_name", "").lower()
            if key and key not in seen:
                seen.add(key)
                contacts.append(c)

        polite_sleep(1.2)

        # If Claude already found enough contacts, skip remaining queries
        if _claude_ok and len([c for c in contacts if c.get("source") == "claude_serp"]) >= 3:
            break
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
        # Direct email address search — SERP snippets often contain actual email addresses
        queries.append(f'{name_q} email "@" Kontakt')
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
        # Direct email search
        queries.append(f'{name_q} email "@" contact{loc}')
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

    # Accept any business email found in SERP text.
    # DON'T restrict to the known domain — it might be the global brand site (parkplaza.com)
    # while the actual contact email is on the local site (parkplazagermany.com).
    # Filter: drop free email providers and obvious spam traps only.
    filtered_emails = []
    throwaway = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                 "icloud.com", "protonmail.com", "aol.com", "gmx.de", "web.de", "t-online.de"}
    company_keywords = {w.lower() for w in re.sub(r"[^a-z0-9 ]", "", company_name.lower()).split() if len(w) >= 4}
    for email in emails:
        email_domain = email.split("@")[-1].lower()
        if email_domain in throwaway:
            continue
        # Accept if: matches known domain, OR email domain contains a company keyword
        if (domain and email_domain == domain) or \
           any(kw in email_domain for kw in company_keywords) or \
           not domain:
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
