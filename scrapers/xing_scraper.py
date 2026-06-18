from __future__ import annotations
"""
XING scraper — LinkedIn of the DACH region.

Used more than LinkedIn in Germany (22.5M DACH members).
XING company pages come as pre-rendered HTML — no login required.

Two sources:
  1. XING company pages (xing.com/pages/{slug}):
     - Company email, phone, address available directly in HTML
     - List of people working at the company (registered on XING)

  2. XING person profiles via SERP (site:xing.com/profile):
     - Title search via Google/Bing for Geschäftsführer/HR roles
     - Name extraction from profile URL slug
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, fetch_with_jina, polite_sleep, multi_engine_search
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
    Find and scrape the XING company page via Jina AI reader.
    Direct Xing access is blocked by WAF; Jina's headless browser bypasses it.
    """
    contacts = []
    found_slug = None

    # 1. Try slug candidates via Jina
    for candidate_slug in _slug_candidates(company_name):
        url = f"{XING_COMPANY_BASE}/{candidate_slug}"
        md = fetch_with_jina(url)
        if md and _page_matches_company(md, company_name):
            found_slug = candidate_slug
            # Extract company-level contact info (phone/email) from main page
            emails = extract_email_from_text(md)
            phones = extract_phone_from_text(md)
            company_phone = phones[0] if phones else None
            company_emails = [e for e in emails if e.split("@")[0].lower() not in _GENERIC_LOCALS]

            # /employees sub-page has the best employee listing (name + title + xing url)
            emp_md = fetch_with_jina(f"{url}/employees")
            if emp_md:
                emp_data = _extract_employees_from_md(emp_md, company_name)
                for emp in emp_data:
                    emp["phone"] = company_phone
                    if not emp.get("email"):
                        emp["email"] = _match_email_to_name(emp["full_name"], company_emails)
                contacts.extend(emp_data)

            # If /employees gave nothing, fall back to main-page extraction
            if not contacts:
                contacts.extend(_extract_company_page_data_md(md, company_name, url))
            break
        polite_sleep(0.4)

    if found_slug:
        return contacts

    # 2. Fallback: SERP to find the correct Xing slug
    query = f'site:xing.com/pages "{company_name}"'
    html = multi_engine_search(query, session)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            gm = re.search(r"/url\?q=(https?://[^&]+)", href)
            if gm:
                href = gm.group(1)
            m = re.search(r"xing\.com/pages/([a-z0-9_\-]+)", href, re.IGNORECASE)
            if not m:
                continue
            xing_url = f"{XING_COMPANY_BASE}/{m.group(1)}"
            md = fetch_with_jina(xing_url)
            if md and _page_matches_company(md, company_name):
                contacts.extend(_extract_company_page_data_md(md, company_name, xing_url))
                emp_md = fetch_with_jina(f"{xing_url}/employees")
                if emp_md:
                    contacts.extend(_extract_employees_from_md(emp_md, company_name))
                break
            polite_sleep(0.4)

    return contacts


def _slug_candidates(company_name: str) -> list[str]:
    """
    Generate possible XING page slugs from company name.

    Xing slugs are highly variable — common patterns observed:
      "KoRo Handels GmbH"      → korohandelsgmbh   (full name, no separators, keep legal suffix)
      "PPL Architektur GmbH"   → ppl-architektur   (no legal, hyphenated)
      "Excel Building Mgmt"    → excelbuilding     (no legal, no separators)
    Strategy: try the no-separator-with-legal-suffix form FIRST, then the cleaned variants.
    """
    def _normalize_umlauts(s: str) -> str:
        return (s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
                 .replace("Ä", "ae").replace("Ö", "oe").replace("Ü", "ue")
                 .replace("ß", "ss"))

    normed = _normalize_umlauts(company_name)
    normed_lower = normed.lower()

    # Full company name without any separators (includes legal suffix) — most common on Xing
    full_noop = re.sub(r"[^a-z0-9]", "", normed_lower)

    # Strip legal suffixes for clean variants
    clean = re.sub(
        r"\s*\b(gmbh\s*&?\s*co\.?\s*kg|gmbh|ag|kg|ug|ohg|gbr|srl|ltd|inc|plc|bv)\b",
        "", normed, flags=re.IGNORECASE,
    ).strip()
    clean_lower = clean.lower()

    # Hyphenated: "koro-handels"
    slug_hyph = re.sub(r"[^a-z0-9]+", "-", clean_lower).strip("-")
    # No-separator without legal: "korohandels"
    slug_noop = re.sub(r"[^a-z0-9]", "", clean_lower)
    # Underscore variant
    slug_under = re.sub(r"[^a-z0-9]+", "_", clean_lower).strip("_")
    # Full name hyphenated (with legal)
    full_hyph = re.sub(r"[^a-z0-9]+", "-", normed_lower).strip("-")

    seen: set[str] = set()
    candidates: list[str] = []

    for s in [full_noop, slug_noop, slug_hyph, slug_under, full_hyph]:
        if s and s not in seen:
            seen.add(s)
            candidates.append(s)

    return candidates[:8]


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


_GENERIC_LOCALS = {
    "info", "kontakt", "contact", "office", "mail", "hello", "hallo",
    "service", "support", "sales", "booking", "reception",
    "team", "events", "jobs", "career", "careers", "recruiting", "hr",
    "press", "media", "legal", "privacy", "marketing", "admin", "no-reply",
}

_NAME_RE = re.compile(
    r"\b([A-ZÜÖÄ][a-züöäß\-]{1,}(?:\s[a-züöäß\-]{1,2})?\s[A-ZÜÖÄ][a-züöäß\-]{1,}(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)\b"
)


def _extract_company_page_data_md(md: str, company_name: str, source_url: str) -> list[dict]:
    """
    Extract contact data from Jina markdown of XING company page.
    Jina returns markdown text; we use regex instead of CSS selectors.
    """
    emails = extract_email_from_text(md)
    phones = extract_phone_from_text(md)
    company_phone = phones[0] if phones else None
    company_emails = [e for e in emails if e.split("@")[0].lower() not in _GENERIC_LOCALS]

    # Use the profile-link parser first (most reliable on company main page too)
    contacts_from_links = _extract_employees_from_md(md, company_name)
    if contacts_from_links:
        for c in contacts_from_links:
            c["phone"] = company_phone
            if not c["email"]:
                c["email"] = _match_email_to_name(c["full_name"], company_emails)
        return contacts_from_links

    # Fallback: regex name extraction for pages without profile links
    contacts = []
    seen: set[str] = set()
    lines = md.split("\n")
    for i, line in enumerate(lines):
        m = _NAME_RE.search(line)
        if not m:
            continue
        name = m.group(1).strip()
        # Skip if first word is a title keyword (e.g., "Director Data", "Head Office")
        first_word = name.split()[0].lower()
        if first_word in _TITLE_FIRST_WORDS:
            continue
        if name.lower() in seen or len(name.split()) < 2:
            continue
        context = " ".join(lines[max(0, i - 2): i + 3])
        role = _extract_role_from_text(context)
        if not role:
            continue
        seen.add(name.lower())
        contacts.append({
            "full_name": name,
            "title": role,
            "email": _match_email_to_name(name, company_emails),
            "phone": company_phone,
            "source": "xing_company_page",
        })

    return contacts


def _normalize_for_compare(s: str) -> str:
    """Lowercase + strip umlauts for fuzzy word matching."""
    return (s.lower()
             .replace("ü", "ue").replace("ö", "oe").replace("ä", "ae").replace("ß", "ss"))


def _extract_title_from_link_text(link_text: str, name: str, first: str) -> str | None:
    """
    Strip the person's name prefix from link_text to get the title.
    Handles umlaut differences between slug-derived name (Fluegel) and display name (Flügel).

    Strategy: compare word-by-word with normalization; remaining words = title.
    """
    link_words = link_text.split()
    for prefix in [name, f"{first}"]:
        prefix_words = prefix.split()
        n = len(prefix_words)
        if len(link_words) <= n:
            continue
        # Check if first n words of link_text match prefix words (normalized)
        if all(
            _normalize_for_compare(link_words[i]) == _normalize_for_compare(prefix_words[i])
            for i in range(n)
        ):
            title_raw = " ".join(link_words[n:]).strip()
            if title_raw and len(title_raw) < 100:
                return title_raw
    return None


_XING_PROFILE_LINK_RE = re.compile(
    r'##\s*\[([^\]]+)\]\(https?://www\.xing\.com/profile/([A-Za-z0-9_\-]+)\)',
    re.IGNORECASE,
)

# Words that, when they start a name-candidate string, indicate it's a title not a person name
_TITLE_FIRST_WORDS = {
    "director", "manager", "head", "chief", "senior", "junior", "lead", "team",
    "vice", "assistant", "cover", "image", "markdown", "content", "employees",
}


def _extract_employees_from_md(md: str, company_name: str) -> list[dict]:
    """
    Parse Xing /employees Jina markdown. Format:
      ## [Piran Asci Co-CEO](https://www.xing.com/profile/Piran_Asci)

    Name comes from the profile URL slug (reliable), title is the remainder of the link text.
    """
    contacts = []
    seen: set[str] = set()

    for m in _XING_PROFILE_LINK_RE.finditer(md):
        link_text = m.group(1).strip()   # "Piran Asci Co-CEO"
        slug = m.group(2)                # "Piran_Asci" or "Julian_BeckmannGiesert"

        # Clean slug: strip trailing digits, split on underscore
        slug_clean = re.sub(r'\d+$', '', slug)
        parts = slug_clean.split('_')
        if len(parts) < 2:
            continue

        first = parts[0]
        # Last name part may be CamelCase: "BeckmannGiesert" → "Beckmann-Giesert"
        last_camel = parts[1]
        last = re.sub(r'([a-z])([A-Z])', r'\1-\2', last_camel)
        name = f"{first} {last}"

        if name.lower() in seen:
            continue
        seen.add(name.lower())

        # Title = link_text minus the name prefix (word-by-word, umlaut-aware)
        title = _extract_title_from_link_text(link_text, name, first)

        contacts.append({
            "full_name": name,
            "title": title,
            "email": None,
            "phone": None,
            "linkedin_url": f"https://www.xing.com/profile/{slug}",
            "source": "xing_employees_page",
        })

    return contacts[:15]


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

    polite_sleep(0.4)
    # Use Jina — direct Xing access is blocked
    md = fetch_with_jina(company_page_url)
    if not md:
        return []

    for emp in _extract_employees_from_md(md, company_name):
        key = emp["full_name"].lower()
        if key not in seen:
            seen.add(key)
            contacts.append(emp)

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
