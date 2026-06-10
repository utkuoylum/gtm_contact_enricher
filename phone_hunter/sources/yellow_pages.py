from __future__ import annotations
"""
Yellow Pages scraper for business phone numbers.
YP.com has the most comprehensive US + international business directory.
UK: yell.com, AU: yellowpages.com.au, etc.

Structure (US): <div class="v-card">
                  <div class="phones phone primary">555-555-5555</div>
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep

logger = logging.getLogger(__name__)

# Directory endpoints per region
DIRECTORIES = {
    "US": "https://www.yellowpages.com/search?search_terms={q}&geo_location_terms={loc}",
    "GB": "https://www.yell.com/ucs/UcsSearchAction.do?keywords={q}&location={loc}",
    "AU": "https://www.yellowpages.com.au/find/{q}/{loc}",
    "CA": "https://www.yellowpages.ca/search/si/1/{q}/{loc}",
}

PHONE_RE = re.compile(r'[\+\(]?[\d\s\-\.\(\)]{7,20}')


def find_phone_in_directories(company_name: str, location: str = "", region: str = "US") -> list[dict]:
    """
    Search business directories for phone numbers.
    Returns list of {number, business_name, address, source, confidence}
    """
    results = []
    session = get_session()

    # Choose directories based on region
    targets = _get_targets(region, company_name, location)

    for url, source_name in targets:
        html = fetch_url(url, session, use_scraper_api=True)
        if not html:
            polite_sleep(0.5)
            continue

        phones = _parse_directory_page(html, company_name, source_name)
        results.extend(phones)
        polite_sleep(1.0)

        if results:
            break  # Found results, no need to try more directories

    return results[:5]


def _get_targets(region: str, company: str, location: str) -> list[tuple[str, str]]:
    targets = []
    q = quote_plus(company)
    loc = quote_plus(location or region)

    if region in ("US", "CA"):
        targets.append((
            f"https://www.yellowpages.com/search?search_terms={q}&geo_location_terms={loc}",
            "yellowpages_us"
        ))
    if region == "GB":
        targets.append((
            f"https://www.yell.com/ucs/UcsSearchAction.do?keywords={q}&location={loc}",
            "yell_uk"
        ))
        targets.append((
            f"https://www.192.com/search/people/?SearchTargetType=PERSON&SearchText={q}+{loc}",
            "192_uk"
        ))
    if region == "AU":
        targets.append((
            f"https://www.yellowpages.com.au/search/listings?clue={q}&locationClue={loc}",
            "yellowpages_au"
        ))

    # Always try Google Maps web search as a fallback
    targets.append((
        f"https://www.google.com/search?q={q}+{loc}+phone+number",
        "google_maps_web"
    ))

    return targets


def _parse_directory_page(html: str, company_name: str, source: str) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    results = []
    company_lower = company_name.lower()

    # YellowPages US: v-card structure
    for card in soup.select('.v-card, .result, [class*="listing"], [class*="business"]'):
        name_el = card.select_one('[class*="business-name"], [class*="company-name"], h2, h3, .heading')
        name = name_el.get_text(strip=True) if name_el else ""

        # Must be same or similar company
        if not _names_overlap(company_lower, name.lower()):
            continue

        # Look for phone in card
        phone_el = card.select_one(
            '.phones, [class*="phone"], [itemprop="telephone"], '
            '[class*="tel"], a[href^="tel:"]'
        )
        phone = ""
        if phone_el:
            if phone_el.get("href", "").startswith("tel:"):
                phone = phone_el["href"][4:]
            else:
                phone = phone_el.get_text(strip=True)

        if not phone:
            # Try tel: links inside card
            tel_a = card.find("a", href=re.compile(r"^tel:"))
            if tel_a:
                phone = tel_a["href"][4:]

        if not phone:
            # Text scan of card
            text = card.get_text(separator=" ")
            m = PHONE_RE.search(text)
            if m:
                phone = m.group(0).strip()

        if phone:
            digits = re.sub(r'\D', '', phone)
            if 7 <= len(digits) <= 15:
                address_el = card.select_one('[class*="address"], [itemprop="address"], .adr')
                address = address_el.get_text(strip=True) if address_el else ""
                results.append({
                    "number": phone.strip(),
                    "business_name": name,
                    "address": address,
                    "source": source,
                    "confidence": 80,
                })

    # Yell UK: different structure
    if not results:
        for card in soup.select('.businessCapsule, [class*="business-card"]'):
            phone_el = card.select_one('[class*="phone"], .telephone, [itemprop="telephone"]')
            if phone_el:
                phone = phone_el.get_text(strip=True)
                digits = re.sub(r'\D', '', phone)
                if 7 <= len(digits) <= 15:
                    name_el = card.select_one('h2, h3, [class*="name"]')
                    name = name_el.get_text(strip=True) if name_el else ""
                    if _names_overlap(company_lower, name.lower()):
                        results.append({
                            "number": phone,
                            "business_name": name,
                            "address": "",
                            "source": source,
                            "confidence": 78,
                        })

    return results


def _names_overlap(query: str, result: str) -> bool:
    if not result:
        return False
    stop = {"the", "a", "an", "and", "&", "ltd", "llc", "inc", "co", "corp", "limited", "plc", "group"}
    q = set(query.lower().split()) - stop
    r = set(result.lower().split()) - stop
    if not q:
        return True
    return len(q & r) / len(q) >= 0.4
