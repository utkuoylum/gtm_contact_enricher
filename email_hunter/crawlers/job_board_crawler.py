from __future__ import annotations
"""
Job posting crawler — job ads often contain recruiter email addresses in
their signatures or contact sections. We scrape job boards for listings
by this company and extract emails.

Sources:
  - LinkedIn jobs (public, no auth needed for basic scrape)
  - Indeed
  - Glassdoor
  - Adzuna
  - The company's own /careers page
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep

logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def find_emails_in_job_postings(company_name: str, domain: str) -> set[str]:
    emails: set[str] = set()
    session = get_session()

    # 1. Company's own careers page
    career_emails = _scrape_careers_page(domain, session)
    emails.update(career_emails)

    # 2. LinkedIn job postings (public snippets via Google)
    li_emails = _search_linkedin_jobs(company_name, domain, session)
    emails.update(li_emails)

    # 3. Indeed
    indeed_emails = _search_indeed(company_name, domain, session)
    emails.update(indeed_emails)

    logger.info(f"[job_board] {domain}: found {len(emails)} emails in job postings")
    return emails


def _scrape_careers_page(domain: str, session) -> set[str]:
    emails: set[str] = set()
    career_paths = ["/careers", "/jobs", "/work-with-us", "/join-us", "/join", "/hiring",
                    "/vacancies", "/opportunities", "/open-positions", "/apply"]
    for path in career_paths:
        html = fetch_url(f"https://{domain}{path}", session, use_scraper_api=True)
        if not html:
            continue
        polite_sleep(0.5)
        text = BeautifulSoup(html, "html.parser").get_text(separator=" ")
        for m in EMAIL_RE.finditer(text):
            e = m.group(0).lower().strip(".,;")
            if domain in e:
                emails.add(e)
        if emails:
            break
    return emails


def _search_linkedin_jobs(company_name: str, domain: str, session) -> set[str]:
    """Search LinkedIn jobs via Google for this company, then scrape public job pages."""
    emails: set[str] = set()
    query = quote_plus(f'site:linkedin.com/jobs "{company_name}" apply email')
    html = fetch_url(
        f"https://www.google.com/search?q={query}&num=10",
        session, use_scraper_api=True
    )
    if not html:
        return emails
    polite_sleep(1.0)

    soup = BeautifulSoup(html, "html.parser")
    # Extract job URLs from Google results
    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = re.search(r"/url\?q=(https?://[a-z.]*linkedin\.com/jobs/[^&]+)", href)
        if match:
            job_url = match.group(1)
            job_html = fetch_url(job_url, session, use_scraper_api=True)
            if job_html:
                text = BeautifulSoup(job_html, "html.parser").get_text(separator=" ")
                for m in EMAIL_RE.finditer(text):
                    e = m.group(0).lower().strip(".,;")
                    if domain in e:
                        emails.add(e)
                polite_sleep(0.8)
    return emails


def _search_indeed(company_name: str, domain: str, session) -> set[str]:
    emails: set[str] = set()
    query = quote_plus(f'"{company_name}" email')
    # Indeed company page
    slug = company_name.lower().replace(" ", "-").replace("&", "and")
    slug = re.sub(r"[^a-z0-9\-]", "", slug)

    html = fetch_url(
        f"https://www.indeed.com/cmp/{slug}/jobs",
        session, use_scraper_api=True
    )
    if html:
        text = BeautifulSoup(html, "html.parser").get_text(separator=" ")
        for m in EMAIL_RE.finditer(text):
            e = m.group(0).lower().strip(".,;")
            if domain in e:
                emails.add(e)

    return emails
