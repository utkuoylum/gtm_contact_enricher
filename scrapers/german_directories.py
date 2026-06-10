from __future__ import annotations
"""
Almanya'ya özgü iş dizinleri.

Kaynaklar (hepsi ücretsiz, login gerektirmez):
  - northdata.com  : Handelsregister + Bundesanzeiger aggregatörü; Geschäftsführer isimleri
  - wlw.de         : 600K+ B2B tedarikçi; email ve telefon herkese açık
  - gelbeseiten.de : Almanya Sarı Sayfalar; özellikle SMB için güçlü
  - 11880.com      : Alman iş dizini; adres + telefon + email
  - cylex.de       : Avrupa iş dizini
  - dasoertliche.de: Alman yerel rehberi
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

    # Run all sources
    northdata = _scrape_northdata(company_name, location, session)
    contacts.extend(northdata)

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

    # If no people found but we have contact info, return as generic entry
    if not contacts and (phones_found or emails_found):
        contacts.append({
            "full_name": company_name,
            "title": "Kontakt",
            "email": emails_found[0] if emails_found else None,
            "phone": phones_found[0] if phones_found else None,
            "source": "german_directory",
        })

    # Attach phone/email to contactless entries
    fallback_phone = phones_found[0] if phones_found else None
    for c in contacts:
        if not c.get("phone") and fallback_phone:
            c["phone"] = fallback_phone

    return contacts


def _scrape_northdata(company_name: str, location: str, session) -> list[dict]:
    """
    Northdata.com aggregates Handelsregister + Bundesanzeiger.
    The web search page is pre-rendered HTML — shows Geschäftsführer names.
    """
    contacts = []
    city = location.split(",")[0].strip() if location else ""

    # North Data search
    query = f"{company_name}"
    if city:
        query += f" {city}"

    search_url = f"https://www.northdata.com/search?q={quote_plus(query)}&language=de"
    html = fetch_url(search_url, session)
    if not html:
        # Try English version
        search_url = f"https://www.northdata.com/_search?query={quote_plus(query)}"
        html = fetch_url(search_url, session)
    if not html:
        return []

    polite_sleep(0.8)
    soup = BeautifulSoup(html, "html.parser")

    # North Data search results show company cards with Geschäftsführer
    # Pattern: "Geschäftsführer: Max Muster" in company card text
    role_map = {
        "geschäftsführer": "Geschäftsführer",
        "prokurist": "Prokurist",
        "vorstand": "Vorstand",
        "inhaber": "Inhaber",
        "gesellschafter": "Gesellschafter",
        "gründer": "Gründer",
        "direktor": "Direktor",
    }

    text = soup.get_text(separator="\n")
    lines = text.split("\n")

    for i, line in enumerate(lines):
        line_lower = line.lower().strip()
        for role_key, role_label in role_map.items():
            if role_key in line_lower:
                # The name is often on the same or next line
                name_candidates = [line]
                if i + 1 < len(lines):
                    name_candidates.append(lines[i + 1])

                for nc in name_candidates:
                    names = _extract_german_names(nc)
                    for name in names:
                        contacts.append({
                            "full_name": name,
                            "title": role_label,
                            "email": None,
                            "phone": None,
                            "source": "northdata",
                        })

    # Also try to get the company detail page
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "northdata.com" in href and re.search(r"/[A-Za-z].*?/[A-Za-z]", href):
            detail_html = fetch_url(href if href.startswith("http") else f"https://www.northdata.com{href}", session)
            if detail_html and _page_matches_company(detail_html, company_name):
                detail_contacts = _parse_northdata_detail(detail_html)
                contacts.extend(detail_contacts)
                break
            polite_sleep(0.5)

    return _dedupe_contacts(contacts)


def _parse_northdata_detail(html: str) -> list[dict]:
    """Parse a NorthData company detail page for officers."""
    soup = BeautifulSoup(html, "html.parser")
    contacts = []

    german_roles = {
        "Geschäftsführer": 1, "Geschäftsführerin": 1,
        "Inhaber": 1, "Inhaberin": 1,
        "Vorstand": 1, "Vorstandsvorsitzender": 1,
        "Prokurist": 2, "Prokuristin": 2,
        "Gesellschafter": 1,
        "Liquidator": 3,
    }

    for role, _ in german_roles.items():
        # Find elements containing the role label
        for el in soup.find_all(string=re.compile(re.escape(role), re.IGNORECASE)):
            parent = el.find_parent(["div", "li", "tr", "section"])
            if parent:
                names = _extract_german_names(parent.get_text(separator=" "))
                for name in names:
                    contacts.append({
                        "full_name": name,
                        "title": role,
                        "email": None,
                        "phone": None,
                        "source": "northdata_detail",
                    })

    return contacts


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
    pattern = r"\b([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)\b"
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
