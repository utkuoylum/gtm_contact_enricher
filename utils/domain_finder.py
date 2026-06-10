from __future__ import annotations
import re
import logging
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep

logger = logging.getLogger(__name__)

EXCLUDED_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "wikipedia.org", "bloomberg.com", "crunchbase.com",
    "glassdoor.com", "indeed.com", "zoominfo.com", "google.com",
    "bing.com", "yahoo.com", "amazon.com", "apple.com", "microsoft.com",
}


def find_company_domain(company_name: str, location: str = "") -> str | None:
    """Find the primary website domain of a company via Google search."""
    query_parts = [f'"{company_name}"', "official website"]
    if location:
        query_parts.append(location)
    query = " ".join(query_parts)

    # Try Google first
    domain = _search_google_for_domain(query, company_name)
    if domain:
        return domain

    # Fallback: try direct guesses
    slug = re.sub(r"[^a-z0-9]", "", company_name.lower())
    for tld in [".com", ".co.uk", ".io", ".net", ".org"]:
        candidate = slug + tld
        if _domain_resolves(candidate):
            return candidate

    return None


def _search_google_for_domain(query: str, company_name: str) -> str | None:
    session = get_session()
    encoded = query.replace(" ", "+")
    url = f"https://www.google.com/search?q={encoded}&num=10"
    html = fetch_url(url, session, use_scraper_api=True)
    if not html:
        # Try DuckDuckGo as fallback
        url = f"https://html.duckduckgo.com/html/?q={encoded}"
        html = fetch_url(url, session)
    if not html:
        return None

    polite_sleep(0.5)
    soup = BeautifulSoup(html, "html.parser")

    # Extract all hrefs and find organic result URLs
    seen = set()
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
        if domain and domain not in EXCLUDED_DOMAINS and domain not in seen:
            seen.add(domain)
            # Prefer domain that contains a slug from company name
            slug = re.sub(r"[^a-z0-9]", "", company_name.lower())
            domain_slug = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
            if slug[:4] in domain_slug or domain_slug[:4] in slug:
                return domain

    # If no close match found, return first non-excluded result
    for d in seen:
        return d
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
