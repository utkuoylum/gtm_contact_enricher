from __future__ import annotations
"""
Extract phone numbers from Google/Bing/DuckDuckGo search results.
The "knowledge panel" in Google SERPs shows the company phone number
sourced from Google Business Profile / schema.org.

Multiple extraction strategies — the HTML structure changes often so we
cast a wide net with regex + multiple CSS-style patterns.
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep

logger = logging.getLogger(__name__)

# Phone-like patterns — international and local forms
PHONE_PATTERNS = [
    # International formats: +44 20 7946 0958, +1-800-555-5555
    r'\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}(?:[\s\-.]?\d{1,4})?',
    # Local formats with parentheses: (020) 7946 0958, (800) 555-5555
    r'\(0?\d{2,5}\)[\s\-.]?\d{3,5}[\s\-.]?\d{3,5}',
    # UK local: 020 7946 0958, 0800 123 4567
    r'0\d{3,5}[\s\-.]?\d{3,5}[\s\-.]?\d{3,5}',
    # US/CA: 800-555-5555, 800.555.5555
    r'\b\d{3}[\-\.]\d{3}[\-\.]\d{4}\b',
]
COMPILED = [re.compile(p) for p in PHONE_PATTERNS]


def find_phones_via_serp(company_name: str, location: str = "", domain: str = "") -> list[dict]:
    """
    Returns list of {number: str, source: str, confidence: int, context: str}
    """
    results = []
    session = get_session()

    queries = _build_queries(company_name, location, domain)
    seen_numbers: set[str] = set()

    for query in queries:
        encoded = quote_plus(query)
        for search_url in [
            f"https://www.google.com/search?q={encoded}",
            f"https://www.bing.com/search?q={encoded}",
        ]:
            html = fetch_url(search_url, session, use_scraper_api=True)
            if not html:
                continue
            phones = _extract_phones_from_serp(html, company_name)
            for p in phones:
                digits = re.sub(r'\D', '', p['number'])
                if digits not in seen_numbers and 7 <= len(digits) <= 15:
                    seen_numbers.add(digits)
                    p['source'] = 'serp_' + ('google' if 'google' in search_url else 'bing')
                    results.append(p)
            polite_sleep(1.2)
            if results:
                break  # Found phones on this query, move on

        if len(results) >= 3:
            break

    return results


def _build_queries(company_name: str, location: str, domain: str) -> list[str]:
    queries = []
    loc = f" {location}" if location else ""
    queries.append(f"{company_name}{loc} phone number")
    queries.append(f"{company_name}{loc} contact number")
    if domain:
        queries.append(f"site:{domain} phone contact")
    queries.append(f'"{company_name}"{loc} tel OR telephone')
    return queries


def _extract_phones_from_serp(html: str, company_name: str) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    results = []

    # Strategy 1: Look for Google's knowledge panel phone container
    # Google uses various attributes — search for any element containing a
    # phone-like data attribute or class
    kp_selectors = [
        '[data-attrid*="phone"]',
        '[aria-label*="phone"]',
        '[aria-label*="Phone"]',
        '.LrzXr',        # Common Google KP value class
        '.rllt__details',
        '[data-dtype="d3ph"]',
        '.kc-pf',
        'span[jsaction*="phone"]',
    ]
    for sel in kp_selectors:
        try:
            for el in soup.select(sel):
                text = el.get_text(strip=True)
                phone = _first_phone_in_text(text)
                if phone:
                    results.append({
                        'number': phone,
                        'context': f'knowledge_panel:{sel}',
                        'confidence': 85,
                    })
        except Exception:
            pass

    # Strategy 2: Bing business answer box
    for el in soup.select('.b_ans .b_address, .b_listnav, .b_factrow'):
        text = el.get_text(separator=' ', strip=True)
        phone = _first_phone_in_text(text)
        if phone:
            results.append({'number': phone, 'context': 'bing_answer', 'confidence': 80})

    # Strategy 3: DuckDuckGo instant answer
    for el in soup.select('.zci__result, .result__extras'):
        text = el.get_text(separator=' ', strip=True)
        phone = _first_phone_in_text(text)
        if phone:
            results.append({'number': phone, 'context': 'ddg_instant', 'confidence': 75})

    # Strategy 4: Broad text scan — any phone near company name in snippet
    # This catches cases where the phone is in an organic result snippet
    full_text = soup.get_text(separator='\n')
    company_lower = company_name.lower()
    lines = full_text.split('\n')
    for i, line in enumerate(lines):
        if company_lower in line.lower():
            # Check 5 lines around the company mention
            window = '\n'.join(lines[max(0, i-2):i+5])
            phone = _first_phone_in_text(window)
            if phone:
                results.append({
                    'number': phone,
                    'context': 'serp_snippet_near_name',
                    'confidence': 60,
                })
            break

    # Strategy 5: tel: links — most reliable signal
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('tel:'):
            raw = href[4:].strip()
            if raw:
                results.append({'number': raw, 'context': 'tel_link', 'confidence': 90})

    # Deduplicate keeping highest confidence
    seen = {}
    for r in results:
        key = re.sub(r'\D', '', r['number'])
        if key and (key not in seen or r['confidence'] > seen[key]['confidence']):
            seen[key] = r
    return list(seen.values())


def _first_phone_in_text(text: str) -> str | None:
    for pattern in COMPILED:
        m = pattern.search(text)
        if m:
            candidate = m.group(0).strip()
            digits = re.sub(r'\D', '', candidate)
            if 7 <= len(digits) <= 15:
                return candidate
    return None
