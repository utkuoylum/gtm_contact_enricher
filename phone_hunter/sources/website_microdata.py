from __future__ import annotations
"""
Extract phone numbers from a company's own website using:
  1. JSON-LD schema.org LocalBusiness / Organization / ContactPoint
  2. HTML microdata (itemprop="telephone")
  3. Open Graph / meta tags
  4. vCard format (tel: links + surrounding context)
  5. hCard microformat (class="tel")
  6. Brute-force page scan (footer, header, contact pages)

This is the HIGHEST CONFIDENCE source — companies publish their own number here.
"""
import re
import json
import logging
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep

logger = logging.getLogger(__name__)

CONTACT_PATHS = [
    "/", "/contact", "/contact-us", "/about", "/about-us",
    "/company", "/reach-us", "/get-in-touch", "/support",
    "/locations", "/offices",
]

PHONE_RE = re.compile(
    r'(?:\+\d{1,3}[\s\-.]?)?'          # optional +country code
    r'(?:\(0?\d{1,5}\)[\s\-.]?)?'      # optional (area code)
    r'\d{2,5}[\s\-.]?\d{3,5}'          # main number
    r'(?:[\s\-.]?\d{2,5})?'            # optional extension
)


def find_phones_on_website(domain: str) -> list[dict]:
    """
    Returns list of {number, source, confidence, page_url}
    Ordered by confidence descending.
    """
    session = get_session()
    results = []
    seen_digits: set[str] = set()

    for path in CONTACT_PATHS:
        url = f"https://{domain}{path}"
        html = fetch_url(url, session, use_scraper_api=True)
        if not html:
            continue
        polite_sleep(0.5)

        page_results = _extract_from_page(html, url)
        for r in page_results:
            digits = re.sub(r'\D', '', r['number'])
            if digits not in seen_digits and 7 <= len(digits) <= 15:
                seen_digits.add(digits)
                results.append(r)

        if len(results) >= 5:
            break

    results.sort(key=lambda r: -r['confidence'])
    return results


def _extract_from_page(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    results = []

    # 1. JSON-LD — highest confidence
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or '')
            phones = _extract_jsonld_phones(data)
            for phone in phones:
                results.append({
                    'number': phone,
                    'source': 'schema_org_jsonld',
                    'confidence': 95,
                    'page_url': page_url,
                })
        except Exception:
            pass

    if results:
        return results  # JSON-LD is most reliable, don't need to go further

    # 2. HTML microdata: itemprop="telephone"
    for el in soup.find_all(attrs={'itemprop': 'telephone'}):
        phone = el.get('content') or el.get_text(strip=True)
        if phone:
            results.append({
                'number': phone,
                'source': 'microdata_itemprop',
                'confidence': 90,
                'page_url': page_url,
            })

    # 3. tel: links — very reliable
    tel_phones = set()
    for a in soup.find_all('a', href=re.compile(r'^tel:')):
        raw = a['href'][4:].strip()
        if raw and raw not in tel_phones:
            tel_phones.add(raw)
            results.append({
                'number': raw,
                'source': 'tel_link',
                'confidence': 88,
                'page_url': page_url,
            })

    # 4. hCard microformat: class="tel"
    for el in soup.find_all(class_=re.compile(r'\btel\b|\bphone\b|\btelephone\b')):
        # Avoid picking up generic containers
        text = el.get_text(strip=True)
        if text and _looks_like_phone(text):
            results.append({
                'number': text,
                'source': 'hcard_microformat',
                'confidence': 82,
                'page_url': page_url,
            })

    # 5. Meta tags (some sites put phone in Open Graph or custom meta)
    for meta in soup.find_all('meta'):
        name = (meta.get('name') or meta.get('property') or '').lower()
        if 'phone' in name or 'telephone' in name or 'contact' in name:
            content = meta.get('content', '')
            if content and _looks_like_phone(content):
                results.append({
                    'number': content,
                    'source': 'meta_tag',
                    'confidence': 85,
                    'page_url': page_url,
                })

    # 6. Footer/header scan — phones in footer are almost always the main number
    if not results:
        for container in soup.select('footer, header, [class*="footer"], [class*="header"], [class*="contact"]'):
            text = container.get_text(separator=' ')
            phones = _extract_phones_from_text(text)
            for phone in phones:
                results.append({
                    'number': phone,
                    'source': 'footer_header_scan',
                    'confidence': 75,
                    'page_url': page_url,
                })

    # 7. Brute-force full page scan as last resort
    if not results:
        text = soup.get_text(separator=' ')
        phones = _extract_phones_from_text(text)
        for phone in phones[:3]:
            results.append({
                'number': phone,
                'source': 'page_text_scan',
                'confidence': 55,
                'page_url': page_url,
            })

    return results


def _extract_jsonld_phones(data) -> list[str]:
    phones = []
    if isinstance(data, dict):
        # Direct telephone field
        for field in ['telephone', 'phone', 'faxNumber']:
            val = data.get(field)
            if val and isinstance(val, str):
                phones.append(val)

        # ContactPoint array
        for cp in data.get('contactPoint', []) if isinstance(data.get('contactPoint'), list) else [data.get('contactPoint')] if data.get('contactPoint') else []:
            if isinstance(cp, dict):
                phone = cp.get('telephone') or cp.get('phone')
                if phone:
                    phones.append(phone)

        # Recurse into nested objects
        for key in ['mainEntity', 'publisher', 'organization', 'location', 'address']:
            nested = data.get(key)
            if isinstance(nested, (dict, list)):
                phones.extend(_extract_jsonld_phones(nested))

    elif isinstance(data, list):
        for item in data:
            phones.extend(_extract_jsonld_phones(item))

    return phones


def _extract_phones_from_text(text: str) -> list[str]:
    found = []
    for m in PHONE_RE.finditer(text):
        candidate = m.group(0).strip()
        digits = re.sub(r'\D', '', candidate)
        if 7 <= len(digits) <= 15:
            found.append(candidate)
    return found


def _looks_like_phone(text: str) -> bool:
    """Quick check: does this string look like a phone number?"""
    digits = re.sub(r'\D', '', text)
    return 7 <= len(digits) <= 15 and bool(re.search(r'[\d\s\-\.\+\(\)]', text))
