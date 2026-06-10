from __future__ import annotations
"""
Deep-crawl a company's website and extract every email address found.
BFS up to `max_pages` pages, prioritizing high-yield paths first.
"""
import re
import logging
from collections import deque
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep

logger = logging.getLogger(__name__)

HIGH_PRIORITY_PATHS = [
    "/team", "/our-team", "/about", "/about-us", "/people", "/leadership",
    "/management", "/executives", "/staff", "/contact", "/contact-us",
    "/blog", "/press", "/news", "/careers", "/jobs", "/hiring",
    "/company", "/who-we-are", "/meet-the-team", "/founders",
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s\-.]?)?\(?\d{2,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}")


def crawl_domain(domain: str, max_pages: int = 80) -> dict:
    """
    Returns {
        emails: set[str],
        phones: set[str],
        pages_crawled: int,
    }
    """
    base = f"https://{domain}"
    session = get_session()
    visited = set()
    emails: set[str] = set()
    phones: set[str] = set()

    # Seed queue: high-priority paths first, then homepage
    queue: deque[str] = deque()
    queue.append(base)
    for path in HIGH_PRIORITY_PATHS:
        queue.append(base + path)

    while queue and len(visited) < max_pages:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        html = fetch_url(url, session, use_scraper_api=True)
        if not html:
            continue

        # Extract emails & phones from this page
        page_emails = _extract_emails(html, domain)
        page_phones = _extract_phones(html)
        emails.update(page_emails)
        phones.update(page_phones)

        if page_emails:
            logger.debug(f"[site_crawler] {url} → {page_emails}")

        # Discover new internal links to follow
        new_links = _extract_internal_links(html, base, domain)
        for link in new_links:
            if link not in visited:
                queue.append(link)

        polite_sleep(0.6)

    logger.info(f"[site_crawler] crawled {len(visited)} pages on {domain}, found {len(emails)} emails")
    return {
        "emails": emails,
        "phones": phones,
        "pages_crawled": len(visited),
    }


def _extract_emails(html: str, domain: str) -> set[str]:
    found = set()
    soup = BeautifulSoup(html, "html.parser")

    # Hidden mailto: links first (most reliable)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("mailto:"):
            email = href[7:].split("?")[0].strip().lower()
            if _is_valid_email(email):
                found.add(email)

    # Raw text scan (obfuscated emails too: name [at] domain [dot] com)
    text = soup.get_text(separator=" ")
    for m in EMAIL_RE.finditer(text):
        e = m.group(0).lower().strip(".,;")
        if _is_valid_email(e):
            found.add(e)

    # Deobfuscate common patterns: "name AT domain DOT com"
    deob = re.sub(r"\s+\[?at\]?\s+", "@", text, flags=re.IGNORECASE)
    deob = re.sub(r"\s+\[?dot\]?\s+", ".", deob, flags=re.IGNORECASE)
    for m in EMAIL_RE.finditer(deob):
        e = m.group(0).lower().strip(".,;")
        if _is_valid_email(e):
            found.add(e)

    # Keep only emails matching this domain (or subdomains)
    return {e for e in found if _email_belongs_to_domain(e, domain)}


def _extract_phones(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    phones = set()

    # tel: links
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("tel:"):
            phones.add(href[4:].strip())

    text = soup.get_text(separator=" ")
    for m in PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 7 <= len(digits) <= 15:
            phones.add(m.group(0).strip())

    return phones


def _extract_internal_links(html: str, base: str, domain: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        absolute = urljoin(base, href)
        parsed = urlparse(absolute)
        # Must be same domain, no fragments, no external
        if parsed.netloc.endswith(domain) and not parsed.fragment:
            # Skip binary files
            if not re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|zip|doc|xls|css|js|ico)$", parsed.path, re.I):
                clean = absolute.split("#")[0]
                links.append(clean)
    return links


def _is_valid_email(email: str) -> bool:
    if not email or "@" not in email:
        return False
    local, domain = email.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        return False
    if len(local) > 64 or len(domain) > 255:
        return False
    # Filter out false positives (image filenames etc.)
    if re.search(r"\.(png|jpg|gif|svg|webp|ico)$", email, re.I):
        return False
    return True


def _email_belongs_to_domain(email: str, domain: str) -> bool:
    email_domain = email.split("@")[-1]
    return email_domain == domain or email_domain.endswith("." + domain)
