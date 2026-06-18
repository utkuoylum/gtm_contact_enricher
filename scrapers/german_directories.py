from __future__ import annotations
"""
German-specific business directories.

Sources (all free, no login required):
  - northdata.com  : Handelsregister + Bundesanzeiger aggregator; Geschäftsführer names
  - moneyhouse.de  : Swiss-German register aggregator; officer names (similar to Northdata)
  - wlw.de         : 600K+ B2B suppliers; email and phone publicly visible
  - gelbeseiten.de : German Yellow Pages; strong for SMBs
  - 11880.com      : German business directory; address + phone + email
  - dasoertliche.de: German local phone directory; SMB phone numbers
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep, multi_engine_search
from utils.domain_finder import extract_email_from_text, extract_phone_from_text

logger = logging.getLogger(__name__)


def find_german_directory_contacts(company_name: str, location: str = "") -> list[dict]:
    """Aggregate results from all German directories."""
    session = get_session()
    contacts: list[dict] = []
    phones_found: list[str] = []
    emails_found: list[str] = []

    # Officer name sources (Handelsregister aggregators)
    northdata = _scrape_northdata(company_name, location, session)
    contacts.extend(northdata)

    moneyhouse = _scrape_moneyhouse(company_name, location, session)
    contacts.extend(moneyhouse)

    # Phone/email directory sources
    wlw = _scrape_wlw(company_name, location, session)
    for item in wlw:
        if item.get("phone"):
            phones_found.append(item["phone"])
        if item.get("email"):
            emails_found.append(item["email"])
        contacts.extend(item.get("people", []))

    gelbe = _scrape_gelbeseiten(company_name, location, session)
    phones_found.extend(gelbe.get("phones", []))
    emails_found.extend(gelbe.get("emails", []))

    eleven = _scrape_11880(company_name, location, session)
    phones_found.extend(eleven.get("phones", []))
    emails_found.extend(eleven.get("emails", []))

    dasoertliche = _scrape_dasoertliche(company_name, location, session)
    phones_found.extend(dasoertliche.get("phones", []))
    emails_found.extend(dasoertliche.get("emails", []))

    # Do NOT create a fake contact with company_name as person — it's not a real person.
    # Phone/email data flows into result.company_phone separately via phone_hunter.

    # Attach phone/email to contactless entries
    fallback_phone = phones_found[0] if phones_found else None
    for c in contacts:
        if not c.get("phone") and fallback_phone:
            c["phone"] = fallback_phone

    return contacts


def _scrape_northdata(company_name: str, location: str, session) -> list[dict]:
    """
    Northdata.de aggregates Handelsregister + Bundesanzeiger.
    Two-step: suggest API to find company URL → scrape detail page for Geschäftsführer.
    """
    contacts = []
    city = location.split(",")[0].strip() if location else ""

    # Step 1: Suggest API to find the company
    query = company_name
    suggest_url = f"https://www.northdata.de/_api/v1/suggest?query={quote_plus(query)}&language=de"
    html = fetch_url(suggest_url, session)
    if not html:
        return []

    polite_sleep(0.5)
    soup = BeautifulSoup(html, "html.parser")

    # Find the best matching company link
    company_keyword = company_name.split()[0].lower()
    detail_href = None

    for a in soup.find_all("a", href=True):
        href = a["href"]
        link_text = a.get_text(strip=True)
        # Must be a company detail link (contains court registry number) and match company name
        if (company_keyword in link_text.lower() and
                href.startswith("/") and
                re.search(r"HRB|HRA|Amtsgericht|AG |CHE-|CVR|KVK", href)):
            # Prefer matching city if provided
            if city and city.lower() in link_text.lower():
                detail_href = href
                break
            elif not detail_href:
                detail_href = href

    if not detail_href:
        # Fallback: first company-ish link
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/") and company_keyword in a.get_text().lower() and len(href) > 10:
                detail_href = href
                break

    if detail_href:
        detail_url = f"https://www.northdata.de{detail_href}"
        polite_sleep(0.5)
        detail_html = fetch_url(detail_url, session)
        if detail_html:
            contacts.extend(_parse_northdata_detail_de(detail_html, company_name))

    # Fallback: parse suggest page itself for inline role mentions
    if not contacts:
        text = soup.get_text(separator="\n")
        contacts.extend(_extract_roles_from_text(text))

    return _dedupe_contacts(contacts)


def _parse_northdata_detail_de(html: str, company_name: str) -> list[dict]:
    """
    Parse a Northdata.DE company detail page for officers.
    Northdata formats inline: "Geschäftsführer: Michael Wernicke"
    """
    if not _page_matches_company(html, company_name):
        return []

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    return _extract_roles_from_text(text)


_UI_WORDS = {"Jetzt", "Upgraden", "Premium", "Login", "Anmelden", "Weitere", "Suche", "Mehr"}

_NORTHDATA_ROLE_PATTERNS = [
    # No re.IGNORECASE so name character class [A-ZÜÖÄ] stays uppercase-only
    (re.compile(r"Geschäftsführer(?:in)?\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)", re.MULTILINE), "Geschäftsführer"),
    (re.compile(r"Prokurist(?:in)?\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)", re.IGNORECASE | re.MULTILINE), "Prokurist"),
    (re.compile(r"Vorstand(?:svorsitzender)?\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)", re.IGNORECASE | re.MULTILINE), "Vorstand"),
    (re.compile(r"Inhaber(?:in)?\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)", re.IGNORECASE | re.MULTILINE), "Inhaber"),
    (re.compile(r"Gesellschafter\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)", re.IGNORECASE | re.MULTILINE), "Gesellschafter"),
]


def _extract_roles_from_text(text: str) -> list[dict]:
    """Extract officer role+name pairs from text using Northdata's format."""
    contacts = []
    seen: set[str] = set()

    # Only parse the section before "Nicht mehr" (former officers marker)
    former_boundary = re.search(r"Nicht mehr\s+Gesch[äa]ftsf[üu]hrer", text, re.IGNORECASE)
    active_text = text[:former_boundary.start()] if former_boundary else text

    for pattern, role in _NORTHDATA_ROLE_PATTERNS:
        for match in pattern.finditer(active_text):
            name = match.group(1).strip()
            # Clean soft hyphens and non-breaking spaces that Northdata embeds
            name = name.replace("\xad", "").replace("\xa0", " ").strip()
            parts = name.split()
            if (len(parts) >= 2 and
                    all(p[0].isupper() for p in parts) and
                    not any(p in _UI_WORDS for p in parts) and
                    name not in seen):
                seen.add(name)
                contacts.append({
                    "full_name": name,
                    "title": role,
                    "email": None,
                    "phone": None,
                    "source": "northdata",
                })

    return contacts[:5]




def _scrape_moneyhouse(company_name: str, location: str, session) -> list[dict]:
    """
    Moneyhouse.de — Swiss-German company register aggregator.
    Shows officer names (Geschäftsführer, Vorstand) similar to Northdata.
    """
    query = quote_plus(company_name)
    url = f"https://www.moneyhouse.de/suche?q={query}"

    html = fetch_url(url, session)
    if not html:
        return []

    polite_sleep(0.5)
    soup = BeautifulSoup(html, "html.parser")
    name_lower = company_name.lower().split()[0]

    # Find matching company detail link
    detail_href = None
    for a in soup.find_all("a", href=re.compile(r"/unternehmen/")):
        if name_lower in a.get_text().lower():
            detail_href = a["href"]
            break

    if not detail_href:
        return []

    detail_url = f"https://www.moneyhouse.de{detail_href}" if detail_href.startswith("/") else detail_href
    detail_html = fetch_url(detail_url, session)
    if not detail_html:
        return []

    if not _page_matches_company(detail_html, company_name):
        return []

    polite_sleep(0.5)
    detail_soup = BeautifulSoup(detail_html, "html.parser")
    text = detail_soup.get_text(separator="\n")
    contacts = _extract_roles_from_text(text)
    for c in contacts:
        c["source"] = "moneyhouse"
    return _dedupe_contacts(contacts)


def _scrape_dasoertliche(company_name: str, location: str, session) -> dict:
    """
    DasOertliche.de — German local business phone directory.
    Best for SMB phone numbers not found via Google Maps or other sources.
    """
    phones: list[str] = []
    emails: list[str] = []
    city = location.split(",")[0].strip() if location else ""

    query = f"{company_name} {city}".strip()
    url = f"https://www.dasoertliche.de/suche?form_name=search_nat&search_nat={quote_plus(query)}&biz=1"

    html = fetch_url(url, session)
    if not html:
        return {"phones": [], "emails": []}

    polite_sleep(0.5)
    soup = BeautifulSoup(html, "html.parser")
    name_lower = company_name.lower().split()[0]

    for card in soup.find_all(["div", "article", "li"], class_=re.compile(r"(result|entry|hit|item|company|treffer)", re.I)):
        if name_lower not in card.get_text().lower():
            continue
        for a in card.find_all("a", href=re.compile(r"^tel:")):
            phones.append(a["href"][4:].strip())
        for a in card.find_all("a", href=re.compile(r"^mailto:")):
            emails.append(a["href"][7:].strip())
        text = card.get_text(separator=" ")
        phones.extend(extract_phone_from_text(text))
        break

    return {"phones": list(dict.fromkeys(phones)), "emails": list(dict.fromkeys(emails))}


def _scrape_wlw(company_name: str, location: str, session) -> list[dict]:
    """
    wlw.de (Wer liefert was) — Germany's largest B2B supplier directory.
    600K+ companies, contact emails publicly visible.
    """
    results = []
    city = location.split(",")[0].strip() if location else ""

    search_url = f"https://www.wlw.de/de/firmen?q={quote_plus(company_name)}"
    if city:
        search_url += f"&city={quote_plus(city)}"

    html = fetch_url(search_url, session)
    if not html:
        return []

    polite_sleep(0.8)
    soup = BeautifulSoup(html, "html.parser")

    # WLW company cards
    company_cards = soup.select("[class*='company-card'], [class*='CompanyCard'], article[class*='result']")
    if not company_cards:
        # Try finding any card with company name match
        company_cards = soup.find_all("article")

    for card in company_cards[:3]:
        card_text = card.get_text(separator=" ", strip=True)
        if company_name.lower().split()[0] not in card_text.lower():
            continue

        emails = extract_email_from_text(card_text)
        phones = extract_phone_from_text(card_text)

        # Check for a detail page link
        detail_link = card.find("a", href=re.compile(r"/de/firmen/"))
        if detail_link:
            detail_url = f"https://www.wlw.de{detail_link['href']}"
            detail_html = fetch_url(detail_url, session)
            if detail_html:
                more_emails = extract_email_from_text(detail_html)
                more_phones = extract_phone_from_text(detail_html)
                emails.extend(more_emails)
                phones.extend(more_phones)
                polite_sleep(0.5)

        result = {
            "people": [],
            "email": emails[0] if emails else None,
            "phone": phones[0] if phones else None,
        }
        results.append(result)
        if results:
            break  # First match is enough

    return results


def _scrape_gelbeseiten(company_name: str, location: str, session) -> dict:
    """
    gelbeseiten.de — German Yellow Pages. Great for SMB phone numbers.
    """
    phones, emails = [], []
    city = location.split(",")[0].strip() if location else ""

    query = quote_plus(company_name)
    city_q = quote_plus(city) if city else ""
    url = f"https://www.gelbeseiten.de/suche/{query}/{city_q}" if city_q else f"https://www.gelbeseiten.de/suche/{query}"

    html = fetch_url(url, session)
    if not html:
        # Fallback: multi-engine search
        query_str = f'site:gelbeseiten.de "{company_name}" {city}'
        html = multi_engine_search(query_str, session)

    if not html:
        return {"phones": [], "emails": []}

    polite_sleep(0.8)
    soup = BeautifulSoup(html, "html.parser")

    # Gelbe Seiten: company name in h2/h3, phone in spans
    name_lower = company_name.lower().split()[0]
    for card in soup.find_all(["article", "div"], class_=re.compile(r"(result|entry|company|card)", re.I)):
        if name_lower not in card.get_text().lower():
            continue

        # Tel links
        for a in card.find_all("a", href=re.compile(r"^tel:")):
            phones.append(a["href"][4:].strip())

        # Email links
        for a in card.find_all("a", href=re.compile(r"^mailto:")):
            emails.append(a["href"][7:].strip())

        text = card.get_text(separator=" ")
        phones.extend(extract_phone_from_text(text))
        emails.extend(extract_email_from_text(text))
        break  # First match only

    return {"phones": list(dict.fromkeys(phones)), "emails": list(dict.fromkeys(emails))}


def _scrape_11880(company_name: str, location: str, session) -> dict:
    """
    11880.com — major German business directory.
    """
    phones, emails = [], []
    city = location.split(",")[0].strip() if location else ""

    url = f"https://www.11880.com/suche/{quote_plus(company_name)}/{quote_plus(city)}" if city \
        else f"https://www.11880.com/suche/{quote_plus(company_name)}/deutschland"

    html = fetch_url(url, session)
    if not html:
        return {"phones": [], "emails": []}

    polite_sleep(0.8)
    soup = BeautifulSoup(html, "html.parser")
    name_lower = company_name.lower().split()[0]

    for card in soup.find_all(["article", "div"], class_=re.compile(r"(result|entry|hit|company)", re.I)):
        if name_lower not in card.get_text().lower():
            continue
        for a in card.find_all("a", href=re.compile(r"^tel:")):
            phones.append(a["href"][4:].strip())
        for a in card.find_all("a", href=re.compile(r"^mailto:")):
            emails.append(a["href"][7:].strip())
        text = card.get_text(separator=" ")
        phones.extend(extract_phone_from_text(text))
        emails.extend(extract_email_from_text(text))
        break

    return {"phones": list(dict.fromkeys(phones)), "emails": list(dict.fromkeys(emails))}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_german_names(text: str) -> list[str]:
    """Extract German-style names (including umlauts) from text."""
    # German names can contain ä, ö, ü, Ä, Ö, Ü, ß
    pattern = r"\b([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)\b"
    found = re.findall(pattern, text)

    # Filter common false positives
    false_pos = {
        "Alle Unternehmen", "Mehr Ergebnisse", "Keine Ergebnisse",
        "Deutsche Bank", "Hamburg Port", "Bayern München",
    }
    return [n for n in found if n not in false_pos and len(n.split()) >= 2]


def _page_matches_company(html: str, company_name: str) -> bool:
    key = company_name.lower().split()[0]
    return key in html.lower()


def _dedupe_contacts(contacts: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for c in contacts:
        key = c.get("full_name", "").lower()
        if key and key not in seen:
            seen.add(key)
            result.append(c)
    return result
