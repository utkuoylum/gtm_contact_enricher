from __future__ import annotations
"""
XING scraper — DACH'ın LinkedIn'i.

Almanya'da LinkedIn'den daha fazla kullanılır (22.5M DACH üyesi).
XING şirket sayfaları pre-rendered HTML olarak gelir, login gerektirmez.

İki kaynak:
  1. XING company pages (xing.com/pages/{slug}):
     - Şirket email, telefon, adres direkt HTML'de mevcut
     - Şirkette çalışan kişi listesi (XING'e kayıtlı olanlar)

  2. XING kişi profilleri via SERP (site:xing.com/profile):
     - Google/Bing üzerinden Geschäftsführer/HR unvan araması
     - Profil URL'sinden isim çıkarımı
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep, multi_engine_search
from utils.domain_finder import extract_email_from_text, extract_phone_from_text

logger = logging.getLogger(__name__)

XING_COMPANY_BASE = "https://www.xing.com/pages"


def find_xing_contacts(company_name: str, location: str = "") -> list[dict]:
    """Entry point: find contacts via XING company page + person search."""
    session = get_session()
    contacts: list[dict] = []
    seen_names: set[str] = set()

    # 1. Company page — direct contact data (email, phone)
    company_data = _scrape_company_page(company_name, location, session)
    if company_data:
        contacts.extend(company_data)
        for c in company_data:
            seen_names.add(c["full_name"].lower())

    # 2. Person search via SERP
    person_results = _search_xing_persons(company_name, location, session)
    for p in person_results:
        if p["full_name"].lower() not in seen_names:
            seen_names.add(p["full_name"].lower())
            contacts.append(p)

    return contacts[:15]


def _scrape_company_page(company_name: str, location: str, session) -> list[dict]:
    """
    Find and scrape the XING company page.
    XING company pages are fully public and pre-rendered.
    """
    slug = _company_slug_from_name(company_name)
    contacts = []

    # Try direct slug URL first
    for candidate_slug in _slug_candidates(company_name):
        url = f"{XING_COMPANY_BASE}/{candidate_slug}"
        html = fetch_url(url, session)
        if html and _page_matches_company(html, company_name):
            data = _extract_company_page_data(html, company_name, url)
            contacts.extend(data)
            if contacts:
                return contacts
        polite_sleep(0.5)

    # Fallback: search for company on XING via SERP
    query = f'site:xing.com/pages "{company_name}"'
    html = multi_engine_search(query, session)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"xing\.com/pages/([a-z0-9_\-]+)", href, re.IGNORECASE)
            if m:
                xing_url = f"{XING_COMPANY_BASE}/{m.group(1)}"
                page_html = fetch_url(xing_url, session)
                if page_html and _page_matches_company(page_html, company_name):
                    data = _extract_company_page_data(page_html, company_name, xing_url)
                    contacts.extend(data)
                    break
                polite_sleep(0.5)

    return contacts


def _slug_candidates(company_name: str) -> list[str]:
    """Generate possible XING page slugs from company name."""
    import re as _re
    # Remove legal suffixes
    clean = _re.sub(
        r"\b(gmbh|ag|kg|ug|srl|ltd|inc|co|gmbh-co-kg)\b", "", company_name, flags=_re.IGNORECASE
    ).strip()

    candidates = []
    # Hyphenated slug: "PPL Architektur" → "ppl-architektur"
    slug_hyph = _re.sub(r"[^a-z0-9]+", "-", clean.lower()).strip("-")
    # No-separator slug: "pplarchitektur"
    slug_noop = _re.sub(r"[^a-z0-9]", "", clean.lower())
    # Underscore slug (XING uses both)
    slug_under = _re.sub(r"[^a-z0-9]+", "_", clean.lower()).strip("_")

    if slug_hyph:
        candidates.append(slug_hyph)
    if slug_under and slug_under != slug_hyph:
        candidates.append(slug_under)
    if slug_noop and slug_noop not in candidates:
        candidates.append(slug_noop)

    # Also try original company name with legal form
    full_slug = _re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")
    if full_slug not in candidates:
        candidates.append(full_slug)

    return candidates[:6]


def _page_matches_company(html: str, company_name: str) -> bool:
    """Check if page content relates to the target company."""
    # Take first word of company name (most distinctive part)
    key = company_name.split()[0].lower()
    return key in html.lower()


def _extract_company_page_data(html: str, company_name: str, source_url: str) -> list[dict]:
    """
    Extract contact data from a XING company page.
    XING company pages contain: company email, phone, employee list.
    """
    soup = BeautifulSoup(html, "html.parser")
    contacts = []

    emails = extract_email_from_text(html)
    phones = extract_phone_from_text(html)

    # Filter out generic emails and keep company-specific ones
    company_emails = [e for e in emails if not e.startswith(("no-reply", "noreply", "donotreply"))]
    company_phone = phones[0] if phones else None

    # XING company pages list employees in a section
    # Look for person cards: name + role visible
    person_card_selectors = [
        "[data-testid*='employee']",
        "[class*='employee']",
        "[class*='member']",
        "[class*='person']",
        "article[class*='profile']",
    ]

    found_people = False
    for selector in person_card_selectors:
        cards = soup.select(selector)
        if len(cards) >= 1:
            for card in cards:
                text = card.get_text(separator=" ", strip=True)
                name_m = re.search(r"\b([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)\b", text)
                if name_m:
                    name = name_m.group(1)
                    role = _extract_role_from_text(text)
                    email = _match_email_to_name(name, company_emails)
                    contacts.append({
                        "full_name": name,
                        "title": role,
                        "email": email,
                        "phone": company_phone,
                        "source": "xing_company_page",
                    })
                    found_people = True
            if found_people:
                break

    # Do NOT create fake persons from email local parts — "hr.manager@" → "Hr Manager"
    # is not a real person. Return empty if no named profiles were found.

    return contacts


def _extract_role_from_text(text: str) -> str | None:
    german_roles = [
        "Geschäftsführer", "Geschäftsführerin", "Gesellschafter",
        "Inhaber", "Inhaberin", "Vorstand", "Gründer", "Gründerin",
        "Prokurist", "Prokuristin", "Personalleiter", "Personalleiterin",
        "HR-Manager", "HR Manager", "Personalreferent", "Recruiter",
        "Managing Director", "CEO", "CTO", "CFO", "COO",
        "Head of HR", "HR Director", "Director",
    ]
    text_lower = text.lower()
    for role in german_roles:
        if role.lower() in text_lower:
            return role
    return None


def _match_email_to_name(name: str, emails: list[str]) -> str | None:
    parts = name.lower().split()
    for email in emails:
        local = email.split("@")[0].lower()
        if any(p in local for p in parts if len(p) > 2):
            return email
    return None


def _company_slug_from_name(company_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")
    return slug


def _search_xing_persons(company_name: str, location: str, session) -> list[dict]:
    """
    Search XING for person profiles.
    Strategy 1: Xing company search page (direct, no SERP needed).
    Strategy 2: Google/Bing SERP with xing.com site: filter as fallback.
    """
    contacts = []
    seen: set[str] = set()

    # Strategy 1: Xing's own company search — lists employees on company profile pages
    contacts.extend(_xing_company_employee_search(company_name, location, session, seen))
    if len(contacts) >= 5:
        return contacts

    # Strategy 2: SERP with Claude extraction (more reliable than site: filter)
    try:
        from utils.claude_extractor import extract_contacts_from_serp, claude_available
        _claude_ok = claude_available()
    except Exception:
        _claude_ok = False

    queries = [
        f'xing.com "{company_name}" Geschäftsführer OR Inhaber OR CEO',
        f'xing.com "{company_name}" {location} Personalleiter OR HR',
    ]
    for query in queries:
        html = multi_engine_search(query, session)
        if not html:
            continue
        polite_sleep(0.8)
        soup = BeautifulSoup(html, "html.parser")

        # Extract Xing profile URLs from results
        for a in soup.find_all("a", href=True):
            href = a["href"]
            gm = re.search(r"/url\?q=(https?://[^&]+)", href)
            url = gm.group(1) if gm else href
            m = re.search(r"xing\.com/profile/([A-Za-z0-9_\-]+)", url)
            if not m:
                continue
            slug = m.group(1)
            if slug in seen:
                continue
            seen.add(slug)
            person = _parse_xing_profile_slug(slug, a.find_parent(["div", "li", "article"]))
            if person and len((person.get("full_name") or "").split()) >= 2:
                contacts.append(person)

        # Also try Claude on the SERP text
        if _claude_ok and len(contacts) < 3:
            try:
                serp_text = soup.get_text(separator=" ")
                claude_contacts = extract_contacts_from_serp(serp_text, company_name, location)
                for c in claude_contacts:
                    key = (c.get("full_name") or "").lower()
                    if key and key not in seen:
                        seen.add(key)
                        contacts.append({**c, "source": "xing_serp"})
            except Exception:
                pass

        if len(contacts) >= 10:
            break

    return contacts


def _xing_company_employee_search(company_name: str, location: str, session, seen: set) -> list[dict]:
    """
    Search Xing company pages for employees.
    Xing's /companies/search returns public company pages with employee lists.
    """
    contacts = []
    query = quote_plus(f"{company_name} {location}".strip())
    search_url = f"https://www.xing.com/search?q={query}&section=companies"
    html = fetch_url(search_url, session)
    if not html:
        return []

    polite_sleep(0.5)
    soup = BeautifulSoup(html, "html.parser")

    # Find company page links
    company_page_url = None
    company_kw = company_name.lower().split()[0]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/pages/" in href and company_kw in (a.get_text() or href).lower():
            company_page_url = href if href.startswith("http") else f"https://www.xing.com{href}"
            break

    if not company_page_url:
        return []

    polite_sleep(0.5)
    page_html = fetch_url(company_page_url, session)
    if not page_html:
        return []

    page_soup = BeautifulSoup(page_html, "html.parser")
    # Employee cards often in sections with data-testid or class containing 'employee'/'member'
    for card in page_soup.select("[class*='employee'], [class*='member'], [data-testid*='employee']"):
        text = card.get_text(separator=" ", strip=True)
        m = re.search(r"\b([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)\b", text)
        if m:
            name = m.group(1)
            key = name.lower()
            if key not in seen:
                seen.add(key)
                contacts.append({
                    "full_name": name,
                    "title": _extract_role_from_text(text),
                    "email": None, "phone": None,
                    "source": "xing_company_page",
                })

    return contacts


def _parse_xing_profile_slug(slug: str, parent_el) -> dict | None:
    """Extract name + title from XING profile URL slug and surrounding snippet."""
    # XING slugs: "Hans_Mueller" or "Hans-Mueller" or "Hans_Mueller2"
    clean = re.sub(r"\d+$", "", slug)
    parts = re.split(r"[_\-]", clean)
    parts = [p for p in parts if p.isalpha() and len(p) > 1]

    if len(parts) < 2:
        return None

    name = " ".join(p.capitalize() for p in parts[:3])
    snippet = parent_el.get_text(separator=" ", strip=True) if parent_el else ""
    title = _extract_role_from_text(snippet)

    return {
        "full_name": name,
        "title": title,
        "email": None,
        "phone": None,
        "linkedin_url": None,
        "source": "xing_serp",
    }
