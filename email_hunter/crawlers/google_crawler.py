from __future__ import annotations
"""
Google/Bing email discovery — searches for indexed emails matching a domain.
Queries like: "@acme.com" site:acme.com, "@acme.com" -site:acme.com (off-site mentions)
Also tries: filetype:pdf "@acme.com", press releases, job postings with signatures.
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep

logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def find_emails_via_search(domain: str) -> set[str]:
    """Return emails found in search engine results for this domain."""
    emails: set[str] = set()
    session = get_session()

    queries = [
        f'"@{domain}"',
        f'"@{domain}" contact OR email',
        f'"@{domain}" filetype:pdf',
        f'site:{domain} email',
        f'"{domain}" "send email" OR "contact us"',
    ]

    for query in queries:
        found = _search_and_extract(query, domain, session)
        emails.update(found)
        polite_sleep(1.5)
        if len(emails) >= 20:  # enough for pattern detection
            break

    logger.info(f"[google_crawler] found {len(emails)} emails for {domain}")
    return emails


def _search_and_extract(query: str, domain: str, session) -> set[str]:
    emails: set[str] = set()
    encoded = quote_plus(query)

    # Try Google first, then Bing as fallback
    for url in [
        f"https://www.google.com/search?q={encoded}&num=20",
        f"https://www.bing.com/search?q={encoded}&count=20",
    ]:
        html = fetch_url(url, session, use_scraper_api=True)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator=" ")

        # Extract emails from SERP snippets
        for m in EMAIL_RE.finditer(text):
            e = m.group(0).lower().strip(".,;>\"'")
            if _is_company_email(e, domain):
                emails.add(e)

        # Also fetch the top result pages and scan them
        result_urls = _extract_result_urls(soup)
        for result_url in result_urls[:5]:
            page_html = fetch_url(result_url, session, use_scraper_api=True)
            if page_html:
                page_text = BeautifulSoup(page_html, "html.parser").get_text(separator=" ")
                for m in EMAIL_RE.finditer(page_text):
                    e = m.group(0).lower().strip(".,;>\"'")
                    if _is_company_email(e, domain):
                        emails.add(e)
                polite_sleep(0.6)

        if emails:
            break  # Got results, no need to try Bing

    return emails


def _extract_result_urls(soup) -> list[str]:
    urls = []
    # Google result links
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Google wraps: /url?q=https://...
        match = re.search(r"/url\?q=(https?://[^&]+)", href)
        if match:
            href = match.group(1)
        if href.startswith("http") and "google.com" not in href and "bing.com" not in href:
            urls.append(href)
    return urls[:10]


def _is_company_email(email: str, domain: str) -> bool:
    if "@" not in email:
        return False
    email_domain = email.split("@")[-1]
    return email_domain == domain or email_domain.endswith("." + domain)
