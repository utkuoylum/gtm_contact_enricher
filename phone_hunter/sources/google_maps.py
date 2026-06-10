from __future__ import annotations
"""
Google Maps phone extraction — two modes:

Mode 1 (API key set): Google Places API
  - findplacefromtext → place_id → place_details (formatted_phone_number)
  - Free: $200/month credit ≈ 28,000 queries
  - Get key: https://console.cloud.google.com → Places API

Mode 2 (no key): Scrape Google local search (tbm=lcl)
  - `?q={company}+{location}&tbm=lcl` returns business cards in HTML
  - Less reliable but free
"""
import os
import re
import logging
import requests
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
PLACES_BASE = "https://maps.googleapis.com/maps/api/place"

PHONE_RE = re.compile(
    r'(?:\+\d{1,3}[\s\-.]?)?(?:\(0?\d{1,5}\)[\s\-.]?)?\d{2,5}[\s\-.]?\d{3,5}(?:[\s\-.]?\d{2,5})?'
)


def find_phone_google_maps(company_name: str, location: str = "") -> list[dict]:
    if GOOGLE_MAPS_API_KEY:
        return _places_api(company_name, location)
    return _local_search_scrape(company_name, location)


def _places_api(company_name: str, location: str) -> list[dict]:
    """Use Google Places API (official, requires key)."""
    query = f"{company_name} {location}".strip()
    results = []

    # Step 1: Find Place
    try:
        resp = requests.get(
            f"{PLACES_BASE}/findplacefromtext/json",
            params={
                "input": query,
                "inputtype": "textquery",
                "fields": "place_id,name,formatted_phone_number,international_phone_number",
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning(f"Google Places API status {resp.status_code}")
            return []
        data = resp.json()
        candidates = data.get("candidates", [])
    except Exception as e:
        logger.error(f"Google Places API error: {e}")
        return []

    for candidate in candidates[:2]:
        # Prefer international_phone_number (already in +XX format)
        phone = candidate.get("international_phone_number") or candidate.get("formatted_phone_number")
        if phone:
            results.append({
                "number": phone,
                "business_name": candidate.get("name", ""),
                "source": "google_places_api",
                "confidence": 95,
            })
            continue

        # Fallback: Place Details call for phone
        place_id = candidate.get("place_id")
        if place_id:
            detail = _places_detail(place_id)
            if detail:
                results.append(detail)

    return results


def _places_detail(place_id: str) -> dict | None:
    try:
        resp = requests.get(
            f"{PLACES_BASE}/details/json",
            params={
                "place_id": place_id,
                "fields": "name,international_phone_number,formatted_phone_number",
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            result = resp.json().get("result", {})
            phone = result.get("international_phone_number") or result.get("formatted_phone_number")
            if phone:
                return {
                    "number": phone,
                    "business_name": result.get("name", ""),
                    "source": "google_places_api_detail",
                    "confidence": 95,
                }
    except Exception:
        pass
    return None


def _local_search_scrape(company_name: str, location: str) -> list[dict]:
    """
    Scrape Google local search results (tbm=lcl).
    Business cards in these results sometimes include phone numbers.
    """
    results = []
    session = get_session()
    query = quote_plus(f"{company_name} {location}".strip())

    # tbm=lcl = local results; hl=en for consistent HTML
    url = f"https://www.google.com/search?q={query}&tbm=lcl&hl=en"
    html = fetch_url(url, session, use_scraper_api=True)
    if not html:
        # Fallback: regular search with maps emphasis
        url = f"https://www.google.com/search?q={query}+phone+number"
        html = fetch_url(url, session, use_scraper_api=True)
    if not html:
        return results

    polite_sleep(0.8)
    soup = BeautifulSoup(html, 'html.parser')

    # Look for tel: links first
    for a in soup.find_all('a', href=re.compile(r'^tel:')):
        phone = a['href'][4:].strip()
        if phone:
            results.append({
                "number": phone,
                "source": "google_local_tel_link",
                "confidence": 88,
            })

    # Google local result cards — phone appears in specific span patterns
    local_card_patterns = [
        # Google uses various internal class names
        '[data-attrid*="phone"]', '[data-dtype="d3ph"]',
        '.rllt__details span', '.VkpGBb', '.LrzXr',
        'span[jsaction*="mousedown:trigger.GHnT4e"]',
    ]
    for selector in local_card_patterns:
        try:
            for el in soup.select(selector):
                text = el.get_text(strip=True)
                m = PHONE_RE.search(text)
                if m:
                    digits = re.sub(r'\D', '', m.group(0))
                    if 7 <= len(digits) <= 15:
                        results.append({
                            "number": m.group(0).strip(),
                            "source": f"google_local:{selector[:30]}",
                            "confidence": 82,
                        })
        except Exception:
            pass

    # Broad text scan — Google sometimes encodes phone as data attribute
    # Look for phone patterns near company name in raw text
    text = soup.get_text(separator='\n')
    company_lower = company_name.lower()
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if company_lower in line.lower():
            window_lines = lines[max(0, i-1):i+8]
            for wl in window_lines:
                m = PHONE_RE.search(wl)
                if m:
                    digits = re.sub(r'\D', '', m.group(0))
                    if 7 <= len(digits) <= 15:
                        results.append({
                            "number": m.group(0).strip(),
                            "source": "google_local_text",
                            "confidence": 65,
                        })

    # Also check raw HTML for encoded phone data
    phone_in_html = re.findall(r'["\'](\+\d{1,3}[\s\d\-\.]{8,15})["\']', html)
    for p in phone_in_html[:5]:
        digits = re.sub(r'\D', '', p)
        if 8 <= len(digits) <= 15:
            results.append({"number": p.strip(), "source": "google_html_attribute", "confidence": 72})

    # Deduplicate
    seen = {}
    for r in results:
        key = re.sub(r'\D', '', r['number'])
        if key and (key not in seen or r['confidence'] > seen[key]['confidence']):
            seen[key] = r
    return list(seen.values())
