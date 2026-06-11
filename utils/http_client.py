from __future__ import annotations
import random
import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from config import USER_AGENTS, REQUEST_TIMEOUT, MAX_RETRIES, SCRAPER_API_KEY, JINA_API_KEY

logger = logging.getLogger(__name__)


_BROWSER_HEADER_SETS = [
    # Chrome 124 on macOS — bypasses most CloudFront WAFs
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "cache-control": "max-age=0",
    },
    # Firefox 125 on Windows
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "de,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "DNT": "1",
    },
    # Chrome 124 on Windows
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    },
]


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
    session.headers.update(random.choice(_BROWSER_HEADER_SETS))
    return session


def fetch_url(url: str, session: requests.Session = None, use_scraper_api: bool = False) -> str | None:
    """
    Fetch URL with automatic fallback chain:
    1. Current session headers
    2. Rotate to a different browser header set (bypasses basic WAF rules)
    3. ScraperAPI (if key configured)
    """
    _session = session or get_session()
    try:
        resp = _session.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (403, 429, 503):
            # Try a different browser header set before giving up
            retry_result = _fetch_with_rotated_headers(url)
            if retry_result:
                return retry_result
            if SCRAPER_API_KEY:
                return _fetch_via_scraper_api(url)
            logger.debug(f"Blocked ({resp.status_code}): {url}")
    except requests.RequestException as e:
        logger.debug(f"Request error for {url}: {e}")
        retry_result = _fetch_with_rotated_headers(url)
        if retry_result:
            return retry_result
        if SCRAPER_API_KEY and use_scraper_api:
            return _fetch_via_scraper_api(url)
    return None


def _fetch_with_rotated_headers(url: str) -> str | None:
    """Retry with a fresh session using a different browser header set."""
    for header_set in _BROWSER_HEADER_SETS:
        try:
            resp = requests.get(url, headers=header_set, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.text
        except requests.RequestException:
            continue
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


def fetch_with_jina(url: str) -> str | None:
    """
    Fetch any URL via Jina AI Reader (r.jina.ai).

    Returns clean markdown text — JS-rendered pages, CloudFront/Cloudflare sites,
    and other WAF-blocked URLs are all handled transparently by Jina's headless browser.

    Free tier available without API key (rate limited ~10 req/sec).
    Set JINA_API_KEY env var for higher limits.

    Returns markdown text (not HTML) — pass directly to text-based parsers.
    """
    jina_url = f"https://r.jina.ai/{url}"
    headers: dict[str, str] = {
        "Accept": "text/plain",
        "X-Return-Format": "markdown",
        # Reduce noise — remove nav/header/footer/cookie banners
        "X-Remove-Selector": "nav,header,footer,cookie-banner,[class*='cookie'],[id*='cookie'],[class*='banner']",
        # Don't wait forever for dynamic content
        "X-Timeout": "15",
    }
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"

    try:
        resp = requests.get(jina_url, headers=headers, timeout=REQUEST_TIMEOUT + 10)
        if resp.status_code == 200 and len(resp.text) > 200:
            logger.debug(f"Jina hit: {url} ({len(resp.text)} chars)")
            return resp.text
        logger.debug(f"Jina returned {resp.status_code} for {url}")
    except requests.RequestException as e:
        logger.debug(f"Jina error for {url}: {e}")

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
        # Unwrap Google redirect (/url?q=...)
        import re
        from urllib.parse import unquote
        m = re.search(r"/url\?q=(https?://[^&]+)", href)
        if m:
            href = unquote(m.group(1))
        # Unwrap DuckDuckGo redirect (?uddg=...)
        m2 = re.search(r"uddg=([^&]+)", href)
        if m2:
            href = unquote(m2.group(1))
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
