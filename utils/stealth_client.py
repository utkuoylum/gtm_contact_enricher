from __future__ import annotations
"""
Stealth HTTP client — bypasses TLS fingerprinting, HTTP/2 fingerprinting, and WAF bot detection.

Detection layers defeated:

  Tier 1 — curl_cffi (Chrome TLS/H2 impersonation)
    Fixes the #1 detection vector: Python's requests library announces itself at the
    TLS handshake (JA3/JA4 fingerprint) before a single HTTP byte is sent. curl_cffi
    uses Chrome's exact cipher suites, extension order, and H2 SETTINGS frames.
    Defeats: Cloudflare, Akamai, F5, Imperva, most simple WAFs.

  Tier 2 — Playwright with stealth patches
    Full headless Chromium with navigator.webdriver patched, chrome object restored,
    plugins/languages spoofed, and --disable-blink-features=AutomationControlled.
    Defeats: JavaScript challenges, React/Vue SPAs, canvas/WebGL fingerprinting.

Both tiers are optional (graceful degradation if not installed).
"""

import random
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# ── curl_cffi availability ─────────────────────────────────────────────────────

try:
    from curl_cffi import requests as _cffi_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _cffi_requests = None
    _CURL_CFFI_AVAILABLE = False

# ── Playwright availability ────────────────────────────────────────────────────

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

# ── Chrome fingerprint profiles ───────────────────────────────────────────────
# Each profile has a consistent (UA, sec-ch-ua, platform) tuple.
# Mixing these across a request is a detection signal — always use one profile end-to-end.

_PROFILES: list[dict] = [
    {
        "impersonate": "chrome124",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    },
    {
        "impersonate": "chrome123",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-mobile": "?0",
    },
    {
        "impersonate": "chrome120",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    },
    {
        "impersonate": "chrome119",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="119", "Chromium";v="119", "Not?A_Brand";v="24"',
        "sec-ch-ua-platform": '"Linux"',
        "sec-ch-ua-mobile": "?0",
    },
]


def _build_headers(profile: dict, referer: str | None = None) -> dict:
    h = {
        "User-Agent": profile["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": profile["sec-ch-ua"],
        "sec-ch-ua-mobile": profile["sec-ch-ua-mobile"],
        "sec-ch-ua-platform": profile["sec-ch-ua-platform"],
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none" if not referer else "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "cache-control": "max-age=0",
    }
    if referer:
        h["referer"] = referer
        h["sec-fetch-site"] = "cross-site"
    return h


# ── Bot challenge detection ────────────────────────────────────────────────────

# Signals that ONLY appear in actual challenge/block pages, not in normal content.
# Keep these specific — broad strings like "cloudflare" appear in normal pages too.
_BOT_BLOCK_PATTERNS = [
    "cf-browser-verification",          # Cloudflare challenge form field
    "just a moment...",                 # Cloudflare interstitial title
    "checking your browser before",     # Cloudflare body text
    "enable javascript and cookies to continue",
    "_cf_chl_opt",                      # Cloudflare challenge JS var
    "datadome.co/captcha",              # DataDome captcha iframe
    "sind sie ein mensch",              # "are you human" German bot page
    "robot oder mensch",
    "incapsula incident id",            # Imperva/Incapsula block page
    "access denied",                    # Generic 403 block body
    # Only flag <title>403 as a block, not any mention of 403 in page body
]

# Title-based blocks — checked against <title> only
_BOT_TITLE_SIGNALS = [
    "403 forbidden",
    "access denied",
    "just a moment",
    "attention required",
    "security check",
]


def is_bot_blocked(html: str | None) -> bool:
    """Return True if the response looks like a bot challenge or block page."""
    if not html or len(html) < 300:
        return True
    lower = html.lower()

    # Check body signals
    if any(sig in lower for sig in _BOT_BLOCK_PATTERNS):
        return True

    # Check <title> specifically (avoids false positives from body content)
    import re
    title_m = re.search(r"<title[^>]*>([^<]{1,80})</title>", html, re.IGNORECASE)
    if title_m:
        title_lower = title_m.group(1).lower()
        if any(sig in title_lower for sig in _BOT_TITLE_SIGNALS):
            return True

    return False


# ── Domain warmup cache ────────────────────────────────────────────────────────
# Tracks domains we've already visited at root level to avoid repeat warmups.
_warmed_domains: set[str] = set()
_warmup_lock = threading.Lock()


def _warmup_domain(domain: str, timeout: int = 10) -> None:
    """Visit the root of a domain once per session to establish cookies and session state."""
    with _warmup_lock:
        if domain in _warmed_domains:
            return
        _warmed_domains.add(domain)

    if not _CURL_CFFI_AVAILABLE:
        return

    root = f"https://{domain}/"
    profile = random.choice(_PROFILES)
    try:
        _cffi_requests.get(
            root,
            headers=_build_headers(profile),
            impersonate=profile["impersonate"],
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception:
        pass


# ── Tier 1: curl_cffi ─────────────────────────────────────────────────────────

def cffi_get(
    url: str,
    timeout: int = 20,
    referer: str | None = None,
    warmup: bool = False,
) -> str | None:
    """
    Fetch URL using curl_cffi with Chrome TLS/H2 impersonation.

    Bypasses JA3/JA4 TLS fingerprinting and HTTP/2 SETTINGS fingerprinting —
    the detection that happens at handshake time before any HTTP content is sent.

    Args:
        warmup: If True, visit the domain root first (establishes cookies, looks natural).
    """
    if not _CURL_CFFI_AVAILABLE:
        return None

    if warmup:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        if domain:
            _warmup_domain(domain)

    profile = random.choice(_PROFILES)
    headers = _build_headers(profile, referer=referer)

    for attempt, p in enumerate([profile] + random.sample(_PROFILES, k=min(2, len(_PROFILES)))):
        if attempt > 0:
            headers = _build_headers(p, referer=referer)
        for ssl_verify in (True, False):  # retry with verify=False on SSL cert issues
            try:
                resp = _cffi_requests.get(
                    url,
                    headers=headers,
                    impersonate=p["impersonate"],
                    timeout=timeout,
                    allow_redirects=True,
                    verify=ssl_verify,
                )
                if resp.status_code == 200:
                    text = resp.text
                    if not is_bot_blocked(text):
                        logger.debug(f"curl_cffi [{p['impersonate']}] OK: {url}")
                        return text
                    logger.debug(f"curl_cffi [{p['impersonate']}] got bot page: {url}")
                elif resp.status_code not in (403, 429, 503):
                    break  # non-retryable (404, etc.)
                break  # don't retry ssl_verify=False on non-SSL errors
            except Exception as e:
                err_str = str(e).lower()
                if "ssl" in err_str or "certificate" in err_str or "cert" in err_str:
                    logger.debug(f"curl_cffi SSL error for {url}, retrying verify=False")
                    continue  # retry with verify=False
                logger.debug(f"curl_cffi error [{p['impersonate']}] for {url}: {e}")
                break  # non-SSL error, move to next profile

    return None


# ── Tier 2: Playwright ─────────────────────────────────────────────────────────

# JS injected before any page script runs — patches automation detection signals
_STEALTH_JS = """
// 1. Hide webdriver flag (the #1 automation signal)
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// 2. Restore window.chrome (absent in headless, present in real Chrome)
window.chrome = {
  app: {
    InstallState: {DISABLED:'disabled',INSTALLED:'installed',NOT_INSTALLED:'not_installed'},
    RunningState: {CANNOT_RUN:'cannot_run',READY_TO_RUN:'ready_to_run',RUNNING:'running'},
    getDetails: function(){}, getIsInstalled: function(){},
    installState: function(){}, isInstalled: false,
    runningState: function(){return 'cannot_run'}
  },
  runtime: {
    OnInstalledReason:{}, OnRestartRequiredReason:{},
    PlatformArch:{}, PlatformNaclArch:{}, PlatformOs:{},
    RequestUpdateCheckStatus:{}
  }
};

// 3. Restore plugins array (headless has 0, real browsers have 3+)
Object.defineProperty(navigator, 'plugins', {
  get: () => [
    {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer', description:'Portable Document Format'},
    {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:''},
    {name:'Native Client', filename:'internal-nacl-plugin', description:''},
  ]
});

// 4. Consistent German locale/language
Object.defineProperty(navigator, 'languages', {get: () => ['de-DE', 'de', 'en-US', 'en']});

// 5. Hardware concurrency (8 cores — common workstation)
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});

// 6. Device memory
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

// 7. Remove HeadlessChrome from user agent string if leaked
try {
  const origUA = navigator.userAgent;
  if (origUA.includes('HeadlessChrome')) {
    Object.defineProperty(navigator, 'userAgent', {
      get: () => origUA.replace('HeadlessChrome', 'Chrome')
    });
  }
} catch(e) {}

// 8. Realistic touch points for desktop (0 = not a touch device)
Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
"""

# Shared browser instance — created once, contexts are per-request (lightweight)
_pw_instance = None  # playwright context manager
_pw_browser = None
_pw_init_lock = threading.Lock()


def _ensure_playwright_browser():
    global _pw_instance, _pw_browser
    with _pw_init_lock:
        if _pw_browser is not None and _pw_browser.is_connected():
            return _pw_browser
        try:
            _pw_instance = _sync_playwright().__enter__()
            _pw_browser = _pw_instance.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--window-size=1920,1080",
                    "--disable-extensions",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-default-apps",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                ],
            )
        except Exception as e:
            logger.warning(f"Playwright browser launch failed: {e}")
            _pw_browser = None
    return _pw_browser


def playwright_get(
    url: str,
    wait_for: str = "networkidle",
    timeout_ms: int = 25000,
) -> str | None:
    """
    Fetch URL using Playwright with stealth JS patches.

    Use for: JS-rendered SPAs (StepStone, Xing), Cloudflare JS challenges,
    and any page where curl_cffi returned a bot challenge page.

    Creates a fresh BrowserContext per call (thread-safe, ~100ms overhead).
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return None

    browser = _ensure_playwright_browser()
    if not browser:
        return None

    profile = random.choice(_PROFILES)

    try:
        ctx = browser.new_context(
            user_agent=profile["User-Agent"],
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={
                "width": random.choice([1920, 1440, 1366, 1280]),
                "height": random.choice([1080, 900, 768]),
            },
            color_scheme="light",
            ignore_https_errors=True,  # handle sites with missing SAN in SSL cert
            extra_http_headers={
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
                "sec-ch-ua": profile["sec-ch-ua"],
                "sec-ch-ua-mobile": profile["sec-ch-ua-mobile"],
                "sec-ch-ua-platform": profile["sec-ch-ua-platform"],
            },
        )
        ctx.add_init_script(_STEALTH_JS)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until=wait_for, timeout=timeout_ms)
        except Exception:
            # "networkidle" can timeout on SPAs or streaming pages — try to get content anyway
            pass
        # Extra wait for React/Vue SPAs that keep re-rendering after initial load
        try:
            page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass
        import time as _time
        _time.sleep(1.5)  # let React finish hydration
        try:
            content = page.content()
        except Exception:
            # Page still navigating — force inner HTML
            content = page.evaluate("() => document.documentElement.outerHTML")
        ctx.close()
        if content and not is_bot_blocked(content) and len(content) > 500:
            logger.debug(f"Playwright OK: {url} ({len(content)} chars)")
            return content
    except Exception as e:
        logger.debug(f"Playwright error for {url}: {e}")

    return None
