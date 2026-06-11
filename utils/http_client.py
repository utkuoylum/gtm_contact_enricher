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
from utils.stealth_client import cffi_get, playwright_get, is_bot_blocked, _CURL_CFFI_AVAILABLE, _PLAYWRIGHT_AVAILABLE

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
        connect=0,       # don't retry connection timeouts — they signal a blocked host
        read=0,          # don't retry read timeouts — slow sites waste pipeline time
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(random.choice(_BROWSER_HEADER_SETS))
    return session


def fetch_url(url: str, session: requests.Session = None, use_scraper_api: bool = False,
              use_playwright: bool = False) -> str | None:
    """
    Fetch URL with tiered anti-detection fallback chain:

    Tier 1: curl_cffi Chrome impersonation (TLS/JA3/H2 fingerprint spoof)
    Tier 2: Rotated browser headers via requests (basic WAF bypass)
    Tier 3: ScraperAPI (if key configured)

    Playwright is NOT included in the default chain to avoid multi-second latency
    on every blocked URL. Pass use_playwright=True to enable it explicitly after
    all other tiers have failed.

    Callers can add Jina AI Reader as a further fallback after this function returns None.
    """
    # Tier 1: curl_cffi — bypasses TLS fingerprinting at handshake level
    if _CURL_CFFI_AVAILABLE:
        result = cffi_get(url, timeout=REQUEST_TIMEOUT + 5)
        if result:
            return result

    # Tier 2: regular requests with rotated browser headers
    _session = session or get_session()
    try:
        resp = _session.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200 and not is_bot_blocked(resp.text):
            return resp.text
        if resp.status_code in (403, 429, 503) or is_bot_blocked(getattr(resp, "text", "")):
            retry_result = _fetch_with_rotated_headers(url)
            if retry_result:
                return retry_result
    except requests.RequestException as e:
        logger.debug(f"Request error for {url}: {e}")
        retry_result = _fetch_with_rotated_headers(url)
        if retry_result:
            return retry_result

    # Tier 3: ScraperAPI
    if SCRAPER_API_KEY and use_scraper_api:
        return _fetch_via_scraper_api(url)

    # Optional Tier: Playwright (full JS rendering + stealth patches)
    # Only runs when explicitly requested — each call takes ~5s, which multiplies
    # badly when there are 30+ paths to probe.
    if use_playwright and _PLAYWRIGHT_AVAILABLE:
        result = playwright_get(url)
        if result:
            return result

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

_DDG_AVAILABLE = True   # flipped to False on first connection timeout — stays False for session
_SEARCH_CONNECT_TIMEOUT = 6   # connect timeout for search engines (fast-fail on network block)


def multi_engine_search(query: str, session: requests.Session = None, num: int = 10) -> str | None:
    """
    Try multiple search engines in order. Returns raw HTML of first successful result.
    Order: DuckDuckGo (least blocking) → Bing → Google (most blocking).
    Uses curl_cffi TLS impersonation as primary method for all engines.
    DuckDuckGo is skipped for the rest of the session after the first connect timeout.
    """
    global _DDG_AVAILABLE
    encoded = quote_plus(query)

    ddg_url = f"https://html.duckduckgo.com/html/?q={encoded}"
    bing_url = f"https://www.bing.com/search?q={encoded}&count={num}"
    google_url = f"https://www.google.com/search?q={encoded}&num={num}"

    engines = []
    if _DDG_AVAILABLE:
        engines.append(ddg_url)
    engines += [bing_url, google_url]

    for url in engines:
        is_ddg = "duckduckgo" in url
        conn_timeout = _SEARCH_CONNECT_TIMEOUT if is_ddg else REQUEST_TIMEOUT

        # Tier 1: curl_cffi (short timeout for DDG to fail fast)
        if _CURL_CFFI_AVAILABLE:
            html = cffi_get(url, timeout=conn_timeout)
            if html and len(html) > 1000 and not is_bot_blocked(html):
                return html
            if html is None and is_ddg:
                # cffi also failed on DDG — mark unavailable before trying requests
                _DDG_AVAILABLE = False
                polite_sleep(0.2)
                continue  # skip to next engine
            polite_sleep(0.3)

        # Tier 2: regular requests fallback
        _session = session or get_session()
        try:
            resp = _session.get(url, timeout=(conn_timeout, REQUEST_TIMEOUT))
            if resp.status_code == 200 and len(resp.text) > 1000 and not is_bot_blocked(resp.text):
                return resp.text
        except requests.exceptions.Timeout:
            if is_ddg:
                _DDG_AVAILABLE = False   # DDG unreachable — skip for rest of session
                logger.debug("DuckDuckGo timeout — disabling for this session")
        except requests.RequestException:
            pass
        polite_sleep(0.5)

    # Last resort: ScraperAPI with Google
    if SCRAPER_API_KEY:
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
