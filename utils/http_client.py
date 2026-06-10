from __future__ import annotations
import random
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from config import USER_AGENTS, REQUEST_TIMEOUT, MAX_RETRIES, SCRAPER_API_KEY


def get_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    })
    return session


def fetch_url(url: str, session: requests.Session = None, use_scraper_api: bool = False) -> str | None:
    """Fetch URL content. Falls back to ScraperAPI if key is set and direct fails."""
    _session = session or get_session()
    try:
        resp = _session.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.text
        # If blocked (403/429) and ScraperAPI key is available, retry through it
        if resp.status_code in (403, 429, 503) and SCRAPER_API_KEY:
            return _fetch_via_scraper_api(url)
    except requests.RequestException:
        if SCRAPER_API_KEY and use_scraper_api:
            return _fetch_via_scraper_api(url)
    return None


def _fetch_via_scraper_api(url: str) -> str | None:
    proxy_url = f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={url}"
    try:
        resp = requests.get(proxy_url, timeout=REQUEST_TIMEOUT + 15)
        if resp.status_code == 200:
            return resp.text
    except requests.RequestException:
        pass
    return None


def polite_sleep(base: float = 1.5):
    time.sleep(base + random.uniform(0, 1.0))
