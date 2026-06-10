from __future__ import annotations
import random
import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from config import USER_AGENTS, REQUEST_TIMEOUT, MAX_RETRIES, SCRAPER_API_KEY

logger = logging.getLogger(__name__)


def get_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "DNT": "1",
    })
    return session


def fetch_url(url: str, session: requests.Session = None, use_scraper_api: bool = False) -> str | None:
    """Fetch URL. Falls back to ScraperAPI on block (403/429/503)."""
    _session = session or get_session()
    try:
        resp = _session.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (403, 429, 503) and SCRAPER_API_KEY:
            return _fetch_via_scraper_api(url)
        if resp.status_code in (403, 429, 503):
            logger.debug(f"Blocked ({resp.status_code}): {url}")
    except requests.RequestException as e:
        logger.debug(f"Request error for {url}: {e}")
        if SCRAPER_API_KEY and use_scraper_api:
            return _fetch_via_scraper_api(url)
    return None


def _fetch_via_scraper_api(url: str) -> str | None:
    proxy_url = f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={url}"
    try:
        resp = requests.get(proxy_url, timeout=REQUEST_TIMEOUT + 20)
        if resp.status_code == 200:
            return resp.text
    except requests.RequestException:
        pass
    return None


def polite_sleep(base: float = 1.5):
    time.sleep(base + random.uniform(0, 0.8))


# ---------------------------------------------------------------------------
# Multi-engine search — tries engines in order, returns first successful HTML
# ---------------------------------------------------------------------------

def multi_engine_search(query: str, session: requests.Session = None, num: int = 10) -> str | None:
    """
    Try multiple search engines in order. Returns raw HTML of first successful result.
    Order: DuckDuckGo (least blocking) → Bing → Google (most blocking).
    """
    _session = session or get_session()
    encoded = quote_plus(query)

    engines = [
        # DuckDuckGo HTML (very permissive, no captcha on moderate use)
        f"https://html.duckduckgo.com/html/?q={encoded}",
        # Bing (moderate blocking)
        f"https://www.bing.com/search?q={encoded}&count={num}",
        # Google (most blocking, last resort)
        f"https://www.google.com/search?q={encoded}&num={num}",
    ]

    for url in engines:
        try:
            resp = _session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and len(resp.text) > 1000:
                return resp.text
        except requests.RequestException:
            continue
        polite_sleep(0.5)

    # Last resort: ScraperAPI with Google
    if SCRAPER_API_KEY:
        google_url = f"https://www.google.com/search?q={encoded}&num={num}"
        return _fetch_via_scraper_api(google_url)

    return None


def serp_links(query: str, session: requests.Session = None, num: int = 10) -> list[str]:
    """
    Run multi_engine_search and extract all non-excluded href links.
    Returns list of URLs.
    """
    _SKIP_DOMAINS = {
        "google.com", "bing.com", "duckduckgo.com", "yahoo.com",
        "facebook.com", "twitter.com", "instagram.com",
    }
    html = multi_engine_search(query, session, num)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Unwrap Google redirect
        import re
        m = re.search(r"/url\?q=(https?://[^&]+)", href)
        if m:
            href = m.group(1)
        if not href.startswith("http"):
            continue
        from urllib.parse import urlparse
        domain = urlparse(href).netloc.lstrip("www.")
        if any(skip in domain for skip in _SKIP_DOMAINS):
            continue
        if href not in seen:
            seen.add(href)
            urls.append(href)
    return urls
