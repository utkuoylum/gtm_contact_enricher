from __future__ import annotations
"""
LinkedIn contact discovery via Google search (no LinkedIn auth required).
Searches: site:linkedin.com/in "Company Name" "HR" OR "CEO" etc.
Parses name + title from Google snippet.
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep
from config import DECISION_MAKER_TITLES

logger = logging.getLogger(__name__)

# Titles to search for (flattened from DECISION_MAKER_TITLES, prioritizing 1-3)
TARGET_TITLES = [
    "CEO", "Founder", "Managing Director", "Owner", "HR Director",
    "Head of HR", "Chief People Officer", "VP HR", "Talent Acquisition",
    "HR Manager", "Recruiting Manager", "Head of Talent", "CHRO", "CPO",
]


def search_linkedin_contacts(company_name: str, location: str = "", job_category: str = "") -> list[dict]:
    contacts = []
    seen_profiles = set()
    session = get_session()

    queries = _build_queries(company_name, location, job_category)

    for query in queries:
        results = _google_search_linkedin(query, session)
        polite_sleep(2.0)
        for r in results:
            url = r.get("url", "")
            if url and url not in seen_profiles:
                seen_profiles.add(url)
                person = _parse_linkedin_snippet(r, company_name)
                if person:
                    contacts.append(person)
        if len(contacts) >= 15:
            break

    return contacts[:15]


def _build_queries(company_name: str, location: str, job_category: str) -> list[str]:
    site = "site:linkedin.com/in"
    company_q = f'"{company_name}"'
    loc = f'"{location}"' if location else ""

    queries = []
    # High-priority: HR/People roles
    hr_terms = '"HR Director" OR "Head of HR" OR "HR Manager" OR "Chief People" OR "Talent Acquisition" OR "Recruiting Manager"'
    queries.append(f'{site} {company_q} ({hr_terms}) {loc}'.strip())

    # C-suite
    exec_terms = '"CEO" OR "Founder" OR "Managing Director" OR "Owner" OR "General Manager"'
    queries.append(f'{site} {company_q} ({exec_terms}) {loc}'.strip())

    # If job_category provided, search for relevant hiring manager
    if job_category:
        queries.append(f'{site} {company_q} "{job_category}" manager OR director {loc}'.strip())

    # Broad sweep
    queries.append(f'{site} {company_q} {loc}'.strip())

    return queries


def _google_search_linkedin(query: str, session) -> list[dict]:
    encoded = quote_plus(query)
    # Try multiple search engines
    sources = [
        f"https://www.google.com/search?q={encoded}&num=10",
        f"https://www.bing.com/search?q={encoded}&count=10",
    ]
    for url in sources:
        html = fetch_url(url, session, use_scraper_api=True)
        if html:
            results = _parse_search_results(html, url)
            if results:
                return results
    return []


def _parse_search_results(html: str, source_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    is_bing = "bing.com" in source_url

    if is_bing:
        items = soup.select("li.b_algo")
    else:
        items = soup.select("div.g, div[data-hveid]")

    for item in items:
        a_tag = item.find("a", href=True)
        if not a_tag:
            continue
        href = a_tag["href"]
        if "linkedin.com/in/" not in href:
            continue

        # Extract clean URL
        url_match = re.search(r"(https?://[a-z.]*linkedin\.com/in/[^&\"' ]+)", href)
        if not url_match:
            continue
        profile_url = url_match.group(1).split("?")[0]

        # Extract snippet text for name + title
        snippet = item.get_text(separator=" ", strip=True)
        results.append({"url": profile_url, "snippet": snippet})

    return results


def _parse_linkedin_snippet(result: dict, company_name: str) -> dict | None:
    snippet = result.get("snippet", "")
    url = result.get("url", "")

    # Extract name from URL slug: linkedin.com/in/john-smith → John Smith
    slug_match = re.search(r"/in/([a-z0-9\-]+)", url)
    if not slug_match:
        return None

    slug = slug_match.group(1)
    # Remove trailing numbers (e.g. john-smith-12345 → john-smith)
    slug = re.sub(r"-\d+$", "", slug)
    name_from_slug = " ".join(w.capitalize() for w in slug.split("-") if w.isalpha())

    if len(name_from_slug.split()) < 2:
        return None

    # Extract title from snippet
    title = _extract_title_from_snippet(snippet, company_name)

    return {
        "full_name": name_from_slug,
        "title": title,
        "linkedin_url": url,
        "email": None,
        "phone": None,
        "source": "linkedin_google",
    }


def _extract_title_from_snippet(snippet: str, company_name: str) -> str | None:
    # Common LinkedIn snippet format: "Name · Title at Company"
    patterns = [
        r"·\s*(.+?)\s+(?:at|@)\s+" + re.escape(company_name),
        r"·\s*(.+?)\s*[-–]\s*" + re.escape(company_name),
        r"^[^·]+·\s*([^·]{5,60})",
    ]
    for pat in patterns:
        m = re.search(pat, snippet, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
            if 3 < len(title) < 80:
                return title

    # Fallback: look for known title keywords in snippet
    all_keywords = []
    for kws in DECISION_MAKER_TITLES.values():
        all_keywords.extend(kws)

    snippet_lower = snippet.lower()
    for kw in all_keywords:
        if kw in snippet_lower:
            # Extract surrounding context
            idx = snippet_lower.index(kw)
            start = max(0, idx - 5)
            end = min(len(snippet), idx + len(kw) + 30)
            return snippet[start:end].strip()

    return None
