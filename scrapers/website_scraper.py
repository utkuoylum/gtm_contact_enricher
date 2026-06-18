from __future__ import annotations
"""
Scrape company website for team members, contact info.
Looks at: /about, /team, /people, /contact, /leadership, /management pages.
Falls back to Wayback Machine cached versions when the live site blocks access.
Uses crt.sh to discover additional subdomains (team.company.com, karriere.company.com).
"""
import json
import logging
import re
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, fetch_with_jina, polite_sleep, REQUEST_TIMEOUT
from utils.domain_finder import extract_email_from_text, extract_phone_from_text
from utils.claude_extractor import parse_impressum_with_claude, extract_contacts_from_text, claude_available

logger = logging.getLogger(__name__)

TEAM_PAGE_PATHS = [
    # Universal
    "/contact", "/contact-us", "/about", "/about-us",
    "/team", "/our-team", "/about/team", "/people",
    "/leadership", "/management", "/executives", "/staff",
    # German / DACH (Impressum is legally required → always has name, phone, email)
    "/impressum", "/imprint", "/kontakt", "/ueber-uns", "/uber-uns",
    "/unternehmen", "/unternehmen/team", "/ueber-uns/team",
    "/team-de", "/ansprechpartner", "/fuehrungsteam", "/fuehrung",
    "/geschaeftsfuehrung", "/vorstand", "/leitung", "/management-team",
    "/unser-team", "/das-team", "/wir-ueber-uns", "/uber-uns/team",
    "/de/team", "/de/kontakt", "/de/impressum", "/de/unternehmen",
    # French
    "/contact-fr", "/equipe", "/a-propos",
    # Spanish/Italian
    "/contacto", "/chi-siamo", "/contatti",
    # Dutch
    "/contact-nl", "/over-ons",
    # Turkish
    "/iletisim", "/hakkimizda", "/ekibimiz",
]

# Subdomains that likely contain team/people pages
_TEAM_SUBDOMAINS = {
    "team", "about", "people", "karriere", "career", "careers", "jobs",
    "personal", "hr", "company", "corporate", "management", "leadership",
}

NAME_TITLE_PATTERNS = [
    # Tries to find name + title in cards/list items
    r"([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s*[–\-|,]\s*([A-Za-z &/]+)",
]


_THROWAWAY_EMAIL_PROVIDERS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "protonmail.com", "aol.com", "gmx.de", "web.de", "t-online.de",
}


def _is_company_email(email: str, domain: str, company_keywords: set[str]) -> bool:
    """
    Accept an email found on a company page if:
    1. Its domain matches the scraped domain (exact or subdomain), OR
    2. Its domain shares company keywords — handles cases like parkplazaberlin.com
       showing ppblinfo@parkplazagermany.com (same hotel, different domain).
    Reject free/throwaway providers.
    """
    host = email.split("@")[-1].lower()
    if host in _THROWAWAY_EMAIL_PROVIDERS:
        return False
    if host == domain or host.endswith("." + domain):
        return True
    # Accept cross-domain emails that share company keywords (≥1 keyword of ≥4 chars)
    if any(kw in host for kw in company_keywords):
        return True
    return False


def quick_impressum_check(domain: str) -> dict:
    """
    Fast single-page fetch targeting /impressum then /imprint.
    Called before the full parallel scrape so DACH company phone/email/officer
    data is available immediately without waiting for the 40-second pool.
    Returns {phone: str|None, email: str|None, contacts: list[dict]}.
    """
    session = get_session()
    for path in ("/impressum", "/imprint"):
        url = f"https://{domain}{path}"
        html = fetch_url(url, session, use_scraper_api=True)
        if not html:
            continue
        contacts = _parse_impressum(html)
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n")
        phones = extract_phone_from_text(text)
        emails = [
            e for e in extract_email_from_text(text)
            if not any(e.endswith(f"@{p}") for p in _THROWAWAY_EMAIL_PROVIDERS)
        ]
        if contacts or phones or emails:
            logger.info(f"Impressum pre-pass ({domain}{path}): {len(contacts)} contacts, phone={bool(phones)}, email={bool(emails)}")
            return {
                "phone": phones[0] if phones else None,
                "email": emails[0] if emails else None,
                "contacts": contacts,
            }
    return {}


def scrape_company_website(domain: str, company_name: str = "") -> list[dict]:
    base_url = f"https://{domain}"
    session = get_session()
    contacts = []
    emails_found = set()
    phones_found = set()

    # Build keyword set for cross-domain email matching
    _co_keywords = {w.lower() for w in re.sub(r"[^a-z0-9 ]", "", company_name.lower()).split() if len(w) >= 4}

    # Discover additional subdomains via crt.sh (finds team.company.com, karriere.company.com etc.)
    extra_bases = _discover_team_subdomains(domain)

    # First check homepage for emails/phones
    html = fetch_url(base_url, session, use_scraper_api=True)
    # If homepage itself is unreachable, treat the whole domain as WAF-blocked.
    # This lets the path loop skip Jina/Wayback immediately (each costs ~15s per path).
    _domain_hard_blocked = (html is None)
    if html:
        for e in extract_email_from_text(html):
            if _is_company_email(e, domain, _co_keywords):
                emails_found.add(e)
        for p in extract_phone_from_text(html):
            phones_found.add(p)
        polite_sleep(0.8)

    # Then hit team/about/impressum pages
    discovered_people = []
    # Limit Claude to a single call per scrape run — with 30+ paths, calling Claude
    # for every page that yields no results would produce 7+ API calls per run.
    _claude_called = False
    # On hard-blocked domains only try legally-required / highest-value paths.
    # Keep this list short (< 8) so the scraper stays fast even when fully blocked.
    _CRITICAL_PATHS = {
        "/impressum", "/imprint", "/kontakt", "/contact", "/contact-us",
        "/team", "/about",
    }
    _consecutive_fails = 0
    # Budget-limit expensive fallbacks: Jina takes ~25s/call, Wayback ~5s/call.
    # Without limits, 3 blocked paths × (Jina + Wayback) = 90s before hard_blocked fires.
    _jina_budget = 1    # call Jina at most once per scrape run
    _wayback_budget = 2  # call Wayback at most twice per scrape run
    for path in TEAM_PAGE_PATHS:
        # Once blocked, skip non-critical paths entirely (each fetch_url costs ~4s)
        if _domain_hard_blocked and path not in _CRITICAL_PATHS:
            continue
        url = base_url + path
        html = fetch_url(url, session, use_scraper_api=True)
        # Fallbacks: Jina handles JS/WAF, Wayback Machine serves cached pages.
        # Both are budget-limited to keep total scrape time predictable.
        if not html and _jina_budget > 0:
            html = fetch_with_jina(url)
            _jina_budget -= 1
        if not html and _wayback_budget > 0 and not _domain_hard_blocked:
            html = _fetch_wayback(domain, path)
            _wayback_budget -= 1
        if not html:
            _consecutive_fails += 1
            if _consecutive_fails >= 3:
                _domain_hard_blocked = True
            continue
        _consecutive_fails = 0  # reset on success
        polite_sleep(0.8)

        for e in extract_email_from_text(html):
            if _is_company_email(e, domain, _co_keywords):
                emails_found.add(e)
        for p in extract_phone_from_text(html):
            phones_found.add(p)

        # Impressum pages: use dedicated parser first, Claude fallback if empty
        if path in ("/impressum", "/imprint"):
            impressum_people = _parse_impressum(html)
            if impressum_people:
                discovered_people.extend(impressum_people)
            elif claude_available() and not _claude_called:
                soup_text = BeautifulSoup(html, "html.parser").get_text(separator="\n")
                claude_people = parse_impressum_with_claude(soup_text, domain)
                discovered_people.extend(claude_people)
                _claude_called = True
            continue  # Skip generic parser for impressum pages

        people = _parse_team_page(html, domain)
        # If team/about page yielded nothing — try Claude as last resort (once per run)
        if not people and claude_available() and not _claude_called and path in ("/team", "/about", "/about-us", "/leadership", "/management", "/unternehmen"):
            soup_text = BeautifulSoup(html, "html.parser").get_text(separator="\n")
            if len(soup_text) > 300:
                people = extract_contacts_from_text(soup_text, domain, source_hint="team_page")
                _claude_called = True
        discovered_people.extend(people)

    # Deduplicate and filter obvious false positives:
    # - card contacts without a title are usually navigation items or multi-name groups
    # - names with 3+ words where none look like a last name
    _NAV_WORDS = {"zum", "inhalt", "weiter", "mehr", "home", "back", "next", "skip",
                  "mach", "deine", "inbox", "tasty", "newsletter", "subscribe"}
    seen_names = set()
    unique_people = []
    for p in discovered_people:
        name = (p.get("full_name") or "").strip()
        key = name.lower()
        if not name or key in seen_names:
            continue
        # Drop card contacts that have no title — almost always navigational/structural text
        if p.get("source") == "website_card" and not p.get("title"):
            continue
        # Drop names that contain obvious navigation words
        name_words = {w.lower() for w in name.split()}
        if name_words & _NAV_WORDS:
            continue
        seen_names.add(key)
        unique_people.append(p)

    # Attach emails directly found on website to people if matching
    for person in unique_people:
        name_parts = person.get("full_name", "").lower().split()
        for email in emails_found:
            local = email.split("@")[0].lower()
            if any(part in local for part in name_parts if len(part) > 2):
                person["email"] = email
                break

    # Do NOT create fake person entries from email local parts.
    # "hr.manager@company.com" → "Hr Manager" looks like a name but isn't.
    # The email itself is captured by email_hunter separately.

    # Attach global phones to people that don't have one
    generic_phone = list(phones_found)[0] if phones_found else None
    for p in unique_people:
        if not p.get("phone") and generic_phone:
            p["phone"] = generic_phone

    # Check extra subdomains discovered via crt.sh
    for subdomain_base in extra_bases[:3]:
        for path in ["/", "/team", "/about", "/contact", "/people", "/leadership"]:
            url = subdomain_base + path
            sub_html = fetch_url(url, session)
            if not sub_html:
                continue
            polite_sleep(0.5)
            for e in extract_email_from_text(sub_html):
                emails_found.add(e)
            sub_people = _parse_team_page(sub_html, domain)
            for p in sub_people:
                key = p.get("full_name", "").lower().strip()
                if key and key not in seen_names:
                    seen_names.add(key)
                    if not p.get("phone") and generic_phone:
                        p["phone"] = generic_phone
                    unique_people.append(p)

    return unique_people[:20]


def _discover_team_subdomains(domain: str) -> list[str]:
    """
    Query crt.sh (Certificate Transparency logs) to find subdomains that
    may contain team/people/career pages (e.g. team.company.com).
    """
    try:
        resp = requests.get(
            f"https://crt.sh/?q=%.{domain}&output=json",
            timeout=8,
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            return []
        entries = resp.json()
        found: set[str] = set()
        for entry in entries:
            names = entry.get("name_value", "")
            for name in names.split("\n"):
                name = name.strip().lstrip("*.")
                if name and "." in name and name.endswith(domain):
                    # Extract subdomain part
                    sub = name[: -(len(domain) + 1)].lower()
                    if sub and sub in _TEAM_SUBDOMAINS:
                        found.add(f"https://{name}")
        return list(found)[:5]
    except Exception as e:
        logger.debug(f"crt.sh lookup failed for {domain}: {e}")
        return []


def _parse_impressum(html: str) -> list[dict]:
    """
    Parse German Impressum page. Legally required to contain responsible persons,
    phone, email. Patterns: 'Geschäftsführer: Max Muster', 'Inhaber: ...', etc.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    people = []

    role_patterns = [
        (r"Gesch[äa]ftsf[üu]hrer[in]?\s*[:\-–]\s*(.+)", "Geschäftsführer"),
        (r"Inhaber[in]?\s*[:\-–]\s*(.+)", "Inhaber"),
        (r"Vorstand\s*[:\-–]\s*(.+)", "Vorstand"),
        (r"Leitung\s*[:\-–]\s*(.+)", "Leitung"),
        (r"Managing Director\s*[:\-–]\s*(.+)", "Managing Director"),
        (r"Director\s*[:\-–]\s*(.+)", "Director"),
        (r"CEO\s*[:\-–]\s*(.+)", "CEO"),
        (r"Partner\s*[:\-–]\s*(.+)", "Partner"),
        (r"Verantwortlich[er]?\s*[:\-–]\s*(.+)", "Verantwortlicher"),
    ]

    emails = extract_email_from_text(text)
    phones = extract_phone_from_text(text)
    generic_phone = phones[0] if phones else None

    for pattern, role in role_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            raw = match.group(1).strip().split("\n")[0].strip()
            # Could be multiple names separated by comma/und
            raw = re.sub(r"\s+", " ", raw)
            names = re.split(r",|\bund\b|\band\b", raw)
            for name_raw in names:
                name = name_raw.strip()
                # Must look like a real name: 2 words, both start uppercase
                if re.match(r"^[A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+", name):
                    # Filter out obvious non-names
                    if len(name) > 50 or any(x in name.lower() for x in ["gmbh", "str.", "straße"]):
                        continue
                    person = {
                        "full_name": name,
                        "title": role,
                        "email": None,
                        "phone": generic_phone,
                        "source": "impressum",
                    }
                    # Try to match an email local part to this person
                    fn_parts = name.lower().split()
                    for email in emails:
                        local = email.split("@")[0].lower()
                        if any(p in local for p in fn_parts if len(p) > 2):
                            person["email"] = email
                            break
                    people.append(person)

    return people


def _parse_team_page(html: str, domain: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    people = []

    # Look for schema.org Person markup
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string or "")
            if isinstance(data, list):
                for item in data:
                    person = _extract_schema_person(item)
                    if person:
                        people.append(person)
            elif isinstance(data, dict):
                person = _extract_schema_person(data)
                if person:
                    people.append(person)
        except Exception:
            pass

    if people:
        return people

    # Heuristic: find elements that look like person cards
    # Common patterns: div.team-member, article.person, li.staff-item etc.
    card_selectors = [
        "[class*='team']", "[class*='member']", "[class*='person']",
        "[class*='staff']", "[class*='employee']", "[class*='leadership']",
        "[class*='executive']", "[class*='people']",
    ]

    for selector in card_selectors:
        cards = soup.select(selector)
        if len(cards) >= 2:
            for card in cards:
                person = _extract_person_from_card(card)
                if person:
                    people.append(person)
            if people:
                return people

    # Last resort: regex scan on plain text.
    # Strip <script> and <style> before get_text() — JSON-LD on hotel/booking sites
    # contains fields like `"name": "Access Restricted", "description": "..."` which
    # produce false matches like "Access Restricted – description".
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    _WEBSITE_NON_NAMES = {
        # Pronouns / possessives
        "my", "your", "our", "their", "his", "her", "its", "you",
        # German non-names
        "unsere", "unser", "ihr", "ihre", "wir", "sie", "das", "der", "die",
        "service", "kontakt", "support", "team", "kunden", "produkt",
        "über", "news", "blog", "home", "mehr", "weiter",
        # English non-names
        "the", "contact", "about", "this", "all", "new", "best", "top",
        "for", "to", "by", "at", "in", "on", "of",
        # Account / navigation UI
        "access", "restricted", "account", "profile", "settings", "login",
        "logout", "register", "password", "search", "menu", "navigation",
        # Loyalty / booking / travel
        "rewards", "points", "transfer", "redeem", "earn", "bonus",
        "corporate", "program", "programme", "offer", "offers", "deal",
        "agent", "arranger", "arrenger", "booking", "reservation", "reservations",
        # Hotel chain brand names that shouldn't be person first names
        "radisson", "marriott", "hilton", "hyatt", "sheraton", "westin", "ibis",
        # HTML / meta keywords that leak into get_text()
        "description", "title", "keywords", "key",
    }
    # Job title keywords — at least one must appear in the extracted title.
    # This blocks product names like "Mischung zum Selbstmixen" while keeping
    # "Geschäftsführer", "Front Office Manager", "Senior Designer", etc.
    _JOB_TITLE_WORDS = {
        "manager", "director", "ceo", "cto", "cfo", "coo", "vp", "president",
        "head", "lead", "senior", "junior", "associate", "specialist", "officer",
        "coordinator", "engineer", "developer", "designer", "analyst", "consultant",
        "leiter", "leiterin", "geschäftsführer", "geschäftsführerin",
        "inhaber", "inhaberin", "prokurist", "prokuristin", "vorstand",
        "referent", "referentin", "gründer", "gründerin", "eigentümer",
        "gesellschafter", "teamlead", "teamleiter", "chef", "chefin",
        "founder", "partner", "principal", "supervisor", "foreman",
    }

    text = soup.get_text(separator="\n")
    for pattern in NAME_TITLE_PATTERNS:
        for match in re.finditer(pattern, text):
            name = match.group(1).strip()
            title = match.group(2).strip()
            parts = name.split()
            if len(parts) < 2 or len(title) < 3:
                continue
            # Reject if any part is a known non-name word
            if any(p.lower() in _WEBSITE_NON_NAMES for p in parts):
                continue
            # Title must contain at least one job-related keyword
            title_lower = title.lower()
            if not any(kw in title_lower for kw in _JOB_TITLE_WORDS):
                continue
            # Title should not be extremely long (heading text, not a job title)
            if len(title) > 80:
                continue
            people.append({"full_name": name, "title": title, "email": None, "phone": None, "source": "website_text"})

    return people


def _extract_schema_person(data: dict) -> dict | None:
    if data.get("@type") not in ("Person", "Employee"):
        return None
    name = data.get("name")
    if not name:
        return None
    return {
        "full_name": name,
        "title": data.get("jobTitle"),
        "email": data.get("email"),
        "phone": data.get("telephone"),
        "source": "website_schema",
    }


def _fetch_wayback(domain: str, path: str) -> str | None:
    """
    Fetch a cached version of a URL from the Wayback Machine.
    Used when the live website blocks access (CloudFront 403, etc.)

    Returns the most recent archived HTML, or None if not available.
    """
    url = f"https://{domain}{path}"
    try:
        # Ask Wayback if they have this URL
        check_resp = requests.get(
            "https://archive.org/wayback/available",
            params={"url": url},
            timeout=8,
        )
        if check_resp.status_code != 200:
            return None

        data = check_resp.json()
        snapshot = data.get("archived_snapshots", {}).get("closest", {})
        if not snapshot.get("available"):
            return None

        snapshot_url = snapshot.get("url", "")
        if not snapshot_url:
            return None

        # Fetch the archived version
        resp = requests.get(snapshot_url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            logger.debug(f"Wayback Machine hit: {snapshot_url}")
            return resp.text

    except Exception as e:
        logger.debug(f"Wayback Machine error for {url}: {e}")

    return None


def _extract_person_from_card(card) -> dict | None:
    text = card.get_text(separator=" ", strip=True)
    # Look for a name: 2-3 words starting with capitals
    name_match = re.search(r"\b([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\b", text)
    if not name_match:
        return None
    name = name_match.group(1)

    # Look for title in nearby element with role/title class
    title = None
    for el in card.find_all(["p", "span", "h3", "h4", "small"]):
        el_class = " ".join(el.get("class", []))
        if any(kw in el_class.lower() for kw in ["title", "role", "position", "job"]):
            title = el.get_text(strip=True)
            break
    if not title:
        # Grab the line after the name
        lines = [l.strip() for l in text.split("  ") if l.strip()]
        for i, line in enumerate(lines):
            if name in line and i + 1 < len(lines):
                candidate = lines[i + 1]
                if 3 < len(candidate) < 60 and not re.search(r"\d{4}", candidate):
                    title = candidate
                    break

    emails = extract_email_from_text(text)
    phones = extract_phone_from_text(text)

    return {
        "full_name": name,
        "title": title,
        "email": emails[0] if emails else None,
        "phone": phones[0] if phones else None,
        "source": "website_card",
    }


# ── Company-level generic contact info ─────────────────────────────────────────

_GENERIC_EMAIL_PRIORITY = ["kontakt", "info", "contact", "office", "mail", "hallo", "hello"]


def get_company_generic_email(domain: str, company_name: str = "", location: str = "") -> str | None:
    """
    Find a generic company contact email.
    Strategy 1: scrape the contact/impressum page directly.
    Strategy 2 (fallback): SERP search for the company's email address.
    """
    base_url = f"https://{domain}"
    session = get_session()

    _co_kw = {w.lower() for w in re.sub(r"[^a-z0-9 ]", "", company_name.lower()).split() if len(w) >= 4}
    found_emails: list[str] = []
    for path in ["/impressum", "/kontakt", "/contact", "/contact-us", "/"]:
        html = fetch_url(base_url + path, session)
        if not html:
            continue
        for e in extract_email_from_text(html):
            if _is_company_email(e, domain, _co_kw):
                found_emails.append(e)
        if found_emails:
            break

    if not found_emails and company_name:
        # Fallback: SERP search — snippets often contain "kontakt@..." addresses directly
        from utils.http_client import multi_engine_search
        from bs4 import BeautifulSoup as _BS
        q = f'"{company_name}" {location} email "@" Kontakt' if location else f'"{company_name}" email "@" Kontakt'
        html = multi_engine_search(q, session)
        if html:
            text = _BS(html, "html.parser").get_text(separator=" ")
            company_kw = {w.lower() for w in company_name.split() if len(w) >= 4}
            throwaway = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "gmx.de", "web.de"}
            for e in extract_email_from_text(text):
                host = e.split("@")[-1].lower()
                if host in throwaway:
                    continue
                if host == domain or host.endswith("." + domain) or any(kw in host for kw in company_kw):
                    found_emails.append(e)

    if not found_emails:
        return None

    # Prefer well-known generic prefixes
    for prefix in _GENERIC_EMAIL_PRIORITY:
        for e in found_emails:
            if e.split("@")[0].lower() == prefix:
                return e

    return found_emails[0]
