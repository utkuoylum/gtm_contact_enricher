from __future__ import annotations
"""
Press release scraper for German/DACH companies.

Sources (all free, no login):
  - presseportal.de  : Germany's #1 PR distribution platform (news aktuell/dpa)
                       Press releases always name executives; Pressekontakt block has email
  - firmenpresse.de  : German-language press portal
  - SERP mining      : site:presseportal.de + bundesanzeiger snippets for executive names
"""
import re
import logging
from datetime import date
from urllib.parse import quote_plus, unquote
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, fetch_with_jina, polite_sleep, multi_engine_search
from utils.domain_finder import extract_email_from_text, extract_phone_from_text

_CURRENT_YEAR = date.today().year

# German/English date patterns found in press releases
_DATE_YEAR_PATTERN = re.compile(
    r"\b(20[12]\d)\b"   # four-digit year 2010–2029
)
# More specific: "15. März 2024" or "März 2024" or "2024-03-15"
_FULL_DATE_PATTERN = re.compile(
    r"(?:\d{1,2}\.\s*(?:Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)"
    r"|\d{4}-\d{2}-\d{2}"
    r")\s*(20[12]\d)"
    r"|"
    r"(20[12]\d)-\d{2}-\d{2}",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)

# Match "Pressekontakt:" block followed by a German name on the next line(s)
_PRESS_CONTACT_HEADER = re.compile(
    r"(?:Pressekontakt|Medienkontakt|Ansprechpartner|Rückfragen an|PR-Kontakt)"
    r"[:\s]*[\n\r]+"
    r"(?:(?:Dr\.|Prof\.|Dipl\.|Mag\.) ?)?"
    r"([A-ZÜÖÄ][a-züöäß\-]+(?:\s[a-z][a-züöäß\-]+)?\s+[A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)",
    re.MULTILINE,
)

# Inline "Ansprechpartnerin: Sarah Müller" pattern
_INLINE_CONTACT = re.compile(
    r"(?:Ansprechpartner(?:in)?|Kontaktperson|Ihre(?:r)?\s+Ansprechpartner(?:in)?)[:\s]+"
    r"([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)",
    re.MULTILINE,
)

# Executive announcement patterns in German press releases
_EXEC_ANNOUNCE_PATTERNS = [
    # "[Name], Geschäftsführer, sagt:"
    re.compile(
        r"([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)"
        r",?\s+(?:Geschäftsführer(?:in)?|Inhaber(?:in)?|CEO|Vorstandsvorsitzender|Vorstandsvorsitzende)"
        r"[^,\n]{0,40}[,\s]+(?:sagt|erklärt|betont|kommentiert|meint|ergänzt)",
        re.MULTILINE,
    ),
    # "Geschäftsführer [Name]"
    re.compile(
        r"(?:Geschäftsführer(?:in)?|Inhaber(?:in)?|CEO|Managing Director|Vorstand)"
        r"\s+([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)",
        re.MULTILINE,
    ),
    # "[Name] ist neuer Geschäftsführer/CEO/Personalleiter"
    re.compile(
        r"([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)"
        r"\s+(?:ist|wird|übernimmt|übernimmt\s+die\s+Position)\s+(?:der |die |neuer? |neue? )?"
        r"(?:Geschäftsführ|CEO|Inhaber|Personalleiter|HR|Vorstand)",
        re.MULTILINE,
    ),
]

_NON_NAME_DE = {
    "die", "der", "das", "ein", "eine", "und", "oder", "für", "mit", "bei",
    "von", "nach", "über", "unter", "vor", "neben", "durch", "gegen", "ohne",
    "seit", "wie", "wenn", "weil", "dass", "aber", "auch", "noch", "schon",
    "mehr", "sehr", "gut", "neu", "alt", "groß", "klein", "viel", "alle",
    "press", "release", "news", "aktuell", "gmbh", "gruppe", "holding",
    "ihre", "uns", "wir", "sie", "kontakt", "service",
}

_TITLE_MAP = [
    ("Geschäftsführerin", "Geschäftsführerin"),
    ("Geschäftsführer", "Geschäftsführer"),
    ("Vorstandsvorsitzende", "Vorstandsvorsitzende"),
    ("Vorstandsvorsitzender", "Vorstandsvorsitzender"),
    ("Vorstand", "Vorstand"),
    ("Inhaber", "Inhaber"),
    ("Personalleiter", "Personalleiter"),
    ("Personalleiterin", "Personalleiterin"),
    ("HR Manager", "HR Manager"),
    ("HR Director", "HR Director"),
    ("Head of HR", "Head of HR"),
    ("Pressesprecher", "Pressesprecher"),
    ("Pressesprecherin", "Pressesprecherin"),
    ("Kommunikationsleiter", "Kommunikationsleiter"),
    ("CEO", "CEO"),
    ("CFO", "CFO"),
    ("CTO", "CTO"),
    ("COO", "COO"),
    ("Managing Director", "Managing Director"),
    ("Direktor", "Direktor"),
]


def find_press_contacts(company_name: str, location: str = "") -> list[dict]:
    """Find contacts from German press release portals (presseportal.de, firmenpresse.de)."""
    session = get_session()
    contacts: list[dict] = []

    # 1. Presseportal.de — Germany's #1 PR portal
    pp = _scrape_presseportal(company_name, session)
    contacts.extend(pp)

    # 2. Firmenpresse.de — fallback German press portal
    if len(contacts) < 3:
        fp = _scrape_firmenpresse(company_name, session)
        contacts.extend(fp)

    # 3. SERP-based press mining (includes Bundesanzeiger snippets)
    if len(contacts) < 3:
        sp = _serp_press_search(company_name, session)
        contacts.extend(sp)

    return _dedupe(contacts)[:8]


def _scrape_presseportal(company_name: str, session) -> list[dict]:
    contacts = []
    company_kw = company_name.lower().split()[0]

    # Step 1: Find press releases on presseportal.de
    search_url = (
        f"https://www.presseportal.de/suche.htm"
        f"?query={quote_plus(company_name)}&language=de"
    )
    html = fetch_url(search_url, session)
    polite_sleep(0.5)

    # Presseportal blocks many IPs — try Jina Reader (headless browser bypass)
    if not html:
        html = fetch_with_jina(search_url)

    if not html:
        query = f'site:presseportal.de "{company_name}" Pressekontakt'
        html = multi_engine_search(query, session)

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    # Collect press release URLs (format: /pm/XXXXX/XXXXXXXXXX/)
    pr_urls: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Google redirect
        m = re.search(r"/url\?q=(https?://[^&]+)", href)
        if m:
            href = unquote(m.group(1))
        # DuckDuckGo redirect
        m2 = re.search(r"uddg=([^&]+)", href)
        if m2:
            href = unquote(m2.group(1))
        if re.search(r"presseportal\.de/pm/\d+/\d+", href):
            pr_urls.append(re.sub(r"\?.*$", "", href))
        elif re.search(r"/pm/\d+/\d+", href):
            pr_urls.append(f"https://www.presseportal.de{href}")

    pr_urls = list(dict.fromkeys(pr_urls))

    # Fetch up to 5 press releases — try Jina if direct fetch fails
    for url in pr_urls[:5]:
        try:
            pr_html = fetch_url(url, session)
            if not pr_html:
                pr_html = fetch_with_jina(url)
            if not pr_html or company_kw not in pr_html.lower():
                continue
            polite_sleep(0.4)
            pr_contacts = _parse_press_release_page(pr_html, company_name)
            contacts.extend(pr_contacts)
        except Exception as e:
            logger.debug(f"Presseportal fetch error {url}: {e}")
        if len(contacts) >= 4:
            break

    # Also parse search result snippets directly
    if not contacts:
        text = soup.get_text(separator="\n")
        if company_kw in text.lower():
            contacts.extend(_extract_from_text(text, company_name))

    return contacts


def _extract_year_from_page(html: str, soup) -> int | None:
    """Extract publication year from a press release page (most reliable → least)."""
    # 1. HTML meta tags: <meta name="date" content="2024-03-15">
    for attr in ("date", "pubdate", "article:published_time", "og:article:published_time",
                 "DC.date", "published_time"):
        tag = soup.find("meta", attrs={"name": attr}) or soup.find("meta", attrs={"property": attr})
        if tag:
            content = tag.get("content", "")
            m = re.search(r"(20[12]\d)", content)
            if m:
                return int(m.group(1))

    # 2. <time> element
    time_tag = soup.find("time")
    if time_tag:
        dt = time_tag.get("datetime", "") or time_tag.get_text()
        m = re.search(r"(20[12]\d)", dt)
        if m:
            return int(m.group(1))

    # 3. Full date pattern in text (e.g. "15. März 2024" or "2024-03-15")
    text = soup.get_text(separator=" ")
    m = _FULL_DATE_PATTERN.search(text)
    if m:
        year_str = m.group(1) or m.group(2)
        if year_str:
            return int(year_str)

    # 4. Most common four-digit year in the first 500 chars (likely the article date)
    snippet = text[:500]
    years = _DATE_YEAR_PATTERN.findall(snippet)
    if years:
        from collections import Counter
        most_common = Counter(years).most_common(1)[0][0]
        return int(most_common)

    return None


def _parse_press_release_page(html: str, company_name: str) -> list[dict]:
    """Parse a single Presseportal.de (or similar) press release page."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    contacts = []
    seen: set[str] = set()

    year_found = _extract_year_from_page(html, soup)
    emails = extract_email_from_text(text)
    phones = extract_phone_from_text(text)

    def _add(name: str, title: str | None, email: str | None = None, src: str = "presseportal"):
        if not _is_valid_german_name(name) or name in seen:
            return
        seen.add(name)
        resolved_email = email
        if not resolved_email:
            for e in emails:
                local = e.split("@")[0].lower()
                if any(p.lower() in local for p in name.split() if len(p) > 2):
                    resolved_email = e
                    break
        contacts.append({
            "full_name": name,
            "title": title or "Pressekontakt",
            "email": resolved_email,
            "phone": phones[0] if phones else None,
            "source": src,
            "year_found": year_found,
        })

    # 1. Parse structured "Pressekontakt:" block
    pc_match = _PRESS_CONTACT_HEADER.search(text)
    if pc_match:
        name = pc_match.group(1).strip()
        block_text = text[pc_match.start():pc_match.start() + 300]
        title = _extract_title(block_text)
        email_m = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", block_text)
        _add(name, title, email=email_m.group(0) if email_m else None, src="presseportal_contact")

    # 2. Inline contact patterns
    for match in _INLINE_CONTACT.finditer(text):
        name = match.group(1).strip()
        ctx = text[max(0, match.start() - 10):match.end() + 120]
        _add(name, _extract_title(ctx))

    # 3. Executive announcement patterns
    for pattern in _EXEC_ANNOUNCE_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1).strip()
            ctx = text[max(0, match.start() - 30):match.end() + 100]
            _add(name, _extract_title(ctx), src="press_release")

    return contacts


def _scrape_firmenpresse(company_name: str, session) -> list[dict]:
    contacts = []
    company_kw = company_name.lower().split()[0]

    search_url = f"https://www.firmenpresse.de/suche/?q={quote_plus(company_name)}"
    html = fetch_url(search_url, session)
    if not html:
        return []

    polite_sleep(0.5)
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/pressemitteilung/" not in href and "/pm/" not in href:
            continue
        url = href if href.startswith("http") else f"https://www.firmenpresse.de{href}"
        pr_html = fetch_url(url, session)
        if not pr_html or company_kw not in pr_html.lower():
            continue
        polite_sleep(0.3)
        contacts.extend(_parse_press_release_page(pr_html, company_name))
        if len(contacts) >= 3:
            break

    return contacts


def _serp_press_search(company_name: str, session) -> list[dict]:
    """Mine SERP snippets for press/announcement executive mentions."""
    contacts = []

    queries = [
        f'"{company_name}" Geschäftsführer Pressemitteilung OR Pressekontakt',
        f'"{company_name}" site:presseportal.de OR site:firmenpresse.de',
    ]

    for query in queries[:2]:
        html = multi_engine_search(query, session)
        if not html:
            continue
        polite_sleep(0.5)
        contacts.extend(_extract_from_text(
            BeautifulSoup(html, "html.parser").get_text(separator="\n"),
            company_name,
        ))
        if len(contacts) >= 3:
            break

    return contacts


def _extract_from_text(text: str, company_name: str) -> list[dict]:
    contacts = []
    seen: set[str] = set()
    emails = extract_email_from_text(text)
    phones = extract_phone_from_text(text)
    company_kw = company_name.lower().split()[0]

    if company_kw not in text.lower():
        return []

    for pattern in _EXEC_ANNOUNCE_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1).strip()
            if not _is_valid_german_name(name) or name in seen:
                continue
            seen.add(name)
            ctx = text[max(0, match.start() - 30):match.end() + 100]
            email = next((e for e in emails if any(p.lower() in e.lower() for p in name.split() if len(p) > 2)), None)
            contacts.append({
                "full_name": name,
                "title": _extract_title(ctx),
                "email": email,
                "phone": phones[0] if phones else None,
                "source": "press_serp",
            })

    return contacts


def _extract_title(ctx: str) -> str | None:
    ctx_lower = ctx.lower()
    for keyword, role in _TITLE_MAP:
        if keyword.lower() in ctx_lower:
            return role
    return None


def _is_valid_german_name(name: str) -> bool:
    if not name:
        return False
    parts = name.split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    for p in parts:
        if not re.match(r"^[A-ZÜÖÄ][a-züöäß\-]+$", p):
            return False
        if p.lower() in _NON_NAME_DE:
            return False
        if len(p) < 2 or len(p) > 30:
            return False
    return True


def _dedupe(contacts: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result = []
    for c in contacts:
        key = c.get("full_name", "").lower()
        if key and key not in seen:
            seen.add(key)
            result.append(c)
    return result
