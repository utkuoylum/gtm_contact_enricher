from __future__ import annotations
import re
import logging
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep, multi_engine_search

logger = logging.getLogger(__name__)

EXCLUDED_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "wikipedia.org", "bloomberg.com", "crunchbase.com",
    "glassdoor.com", "indeed.com", "zoominfo.com", "google.com",
    "bing.com", "yahoo.com", "amazon.com", "apple.com", "microsoft.com",
    "xing.com", "kununu.com", "stepstone.de", "jobs.de", "monster.de",
}

# Legal entity suffixes to strip before slug generation
_LEGAL_SUFFIXES = re.compile(
    r"\b(gmbh|ag|kg|ohg|gbr|ug|srl|spa|sarl|bv|nv|ltd|llc|inc|corp|"
    r"co|plc|lp|llp|pte|pvt|pty|sa|as|ab|oy|sro|kft|zrt)\b",
    re.IGNORECASE,
)

# Country → preferred TLDs
_LOCATION_TLDS = {
    "germany": [".de"], "deutschland": [".de"], "hamburg": [".de"],
    "berlin": [".de"], "munich": [".de"], "münchen": [".de"],
    "frankfurt": [".de"], "cologne": [".de"], "köln": [".de"],
    "austria": [".at"], "österreich": [".at"], "vienna": [".at"], "wien": [".at"],
    "switzerland": [".ch"], "schweiz": [".ch"], "zürich": [".ch"],
    "france": [".fr"], "paris": [".fr"],
    "spain": [".es"], "madrid": [".es"], "barcelona": [".es"],
    "italy": [".it"], "milan": [".it"], "rome": [".it"],
    "netherlands": [".nl"], "amsterdam": [".nl"],
    "poland": [".pl"], "warsaw": [".pl"],
    "turkey": [".com.tr", ".tr"], "istanbul": [".com.tr"], "ankara": [".com.tr"],
    "uk": [".co.uk"], "london": [".co.uk"], "england": [".co.uk"],
    "australia": [".com.au"], "sydney": [".com.au"],
}


def _country_tlds(location: str) -> list[str]:
    loc_lower = location.lower()
    for key, tlds in _LOCATION_TLDS.items():
        if key in loc_lower:
            return tlds
    return []


def _clean_slug(company_name: str) -> str:
    """Strip legal suffixes and punctuation, return a clean slug."""
    cleaned = _LEGAL_SUFFIXES.sub("", company_name).strip(" ,.-&")
    return re.sub(r"[^a-z0-9]", "", cleaned.lower())


def find_company_domain(company_name: str, location: str = "") -> str | None:
    """Find the primary website domain of a company via search + direct resolution."""
    # Try search engines first
    for query in [
        f'"{company_name}" official website',
        f'"{company_name}" {location} Kontakt' if location else f'"{company_name}" Kontakt Impressum',
        f'"{company_name}" {location} website' if location else f'"{company_name}" website',
    ]:
        domain = _search_google_for_domain(query, company_name)
        if domain:
            return domain

    # Fallback: try direct TLD guesses
    # Build slug candidates — longest first, then first-word fallback
    slug_candidates = _build_slug_candidates(company_name)
    country_tlds = _country_tlds(location)
    # Always include .com early — many DACH companies use .com even if they're German
    base_tlds = [".com", ".de", ".at", ".ch", ".net", ".org", ".co.uk", ".io", ".eu"]
    all_tlds = list(dict.fromkeys(country_tlds + base_tlds))  # country TLD first, no duplicates

    seen = set()
    for slug in slug_candidates:
        for tld in all_tlds:
            key = slug + tld
            if key in seen:
                continue
            seen.add(key)
            if _domain_resolves(key):
                return key

    return None


def _build_slug_candidates(company_name: str) -> list[str]:
    """
    Generate ordered list of slug candidates to try.

    'Wenatex Das Schlafsystem GmbH' →
      ['wenatex', 'wenatexdasschlafsystem', 'wenatexschlafsystem']

    Rationale: the first meaningful word is almost always the brand name.
    The full slug (minus legal suffix) is a secondary candidate.
    """
    # Strip legal suffix
    cleaned = _LEGAL_SUFFIXES.sub("", company_name).strip(" ,.-&")

    candidates = []

    # 1. First word only (brand name — highest priority for complex names)
    words = cleaned.split()
    if words:
        first_word_slug = re.sub(r"[^a-z0-9]", "", words[0].lower())
        if len(first_word_slug) >= 3:
            candidates.append(first_word_slug)

    # 2. Full cleaned slug (without legal suffix)
    full_slug = re.sub(r"[^a-z0-9]", "", cleaned.lower())
    if full_slug and full_slug not in candidates and len(full_slug) <= 25:
        candidates.append(full_slug)

    # 3. First two words (catches "Weber Maschinenbau" → "webermaschinenbau")
    if len(words) >= 2:
        two_word_slug = re.sub(r"[^a-z0-9]", "", " ".join(words[:2]).lower())
        if two_word_slug not in candidates and len(two_word_slug) <= 20:
            candidates.append(two_word_slug)

    return candidates


def _search_google_for_domain(query: str, company_name: str) -> str | None:
    session = get_session()
    html = multi_engine_search(query, session)
    if not html:
        return None

    polite_sleep(0.5)
    soup = BeautifulSoup(html, "html.parser")

    # Extract all hrefs and find organic result URLs
    seen = set()
    # Use first-word slug for matching — more reliable for "Wenatex Das Schlafsystem GmbH"
    slug_candidates_list = _build_slug_candidates(company_name)
    primary_slug = slug_candidates_list[0] if slug_candidates_list else _clean_slug(company_name)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Google wraps links like /url?q=https://...
        match = re.search(r"/url\?q=(https?://[^&]+)", href)
        if match:
            href = match.group(1)
        if not href.startswith("http"):
            continue
        parsed = urlparse(href)
        domain = parsed.netloc.lstrip("www.")
        if not domain or domain in EXCLUDED_DOMAINS or domain in seen:
            continue
        seen.add(domain)

        # Match: any slug candidate appears in domain's first label
        domain_base = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
        matched = False
        for s in slug_candidates_list:
            if s and (s in domain_base or domain_base in s or
                      (len(s) >= 4 and s[:4] in domain_base)):
                matched = True
                break
        if matched:
            return domain

    # No slug match found — return None, let TLD guessing take over
    return None


def _domain_resolves(domain: str) -> bool:
    session = get_session()
    try:
        resp = session.get(f"https://{domain}", timeout=5, allow_redirects=True)
        return resp.status_code < 400
    except Exception:
        return False


def extract_email_from_text(text: str) -> list[str]:
    pattern = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
    emails = re.findall(pattern, text)
    # Filter out image/icon filenames that match the pattern
    return [e for e in emails if not any(e.endswith(ext) for ext in [".png", ".jpg", ".gif"])]


def extract_phone_from_text(text: str) -> list[str]:
    """Extract international and local phone numbers."""
    pattern = r"(?:\+?\d{1,3}[\s\-.]?)?\(?\d{2,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}"
    phones = re.findall(pattern, text)
    cleaned = []
    for p in phones:
        digits = re.sub(r"\D", "", p)
        if 7 <= len(digits) <= 15:
            cleaned.append(p.strip())
    return list(set(cleaned))
