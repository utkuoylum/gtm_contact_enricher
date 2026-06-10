from __future__ import annotations
"""
General Google/Bing scraping for executive contacts.
Searches for: "company name" CEO email, "company name" HR contact, etc.
Also checks press releases, news, and business directories.
"""
import re
import logging
from urllib.parse import quote_plus, urlparse
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep
from utils.domain_finder import extract_email_from_text, extract_phone_from_text

logger = logging.getLogger(__name__)

BUSINESS_DIRECTORIES = [
    "companieshouse.gov.uk",  # UK
    "opencorporates.com",
    "crunchbase.com",
    "bloomberg.com/profile",
    "pitchbook.com",
    "dnb.com",  # Dun & Bradstreet
]


def google_contact_search(company_name: str, location: str = "", domain: str = "") -> list[dict]:
    contacts = []
    session = get_session()

    queries = _build_queries(company_name, location, domain)
    seen = set()

    for query in queries:
        encoded = quote_plus(query)
        results = []
        for search_url in [
            f"https://www.google.com/search?q={encoded}&num=10",
            f"https://www.bing.com/search?q={encoded}&count=10",
        ]:
            html = fetch_url(search_url, session, use_scraper_api=True)
            if html:
                new_contacts = _extract_contacts_from_serp(html, company_name, search_url)
                results.extend(new_contacts)
                polite_sleep(1.5)
                break  # one search engine per query is enough

        for c in results:
            key = (c.get("full_name", ""), c.get("email", ""))
            if key not in seen:
                seen.add(key)
                contacts.append(c)

        if len(contacts) >= 10:
            break

    return contacts[:10]


def _build_queries(company_name: str, location: str, domain: str) -> list[str]:
    queries = []
    loc = f' "{location}"' if location else ""

    if domain:
        queries.append(f'site:{domain} "contact" OR "team" OR "about"')

    queries.append(f'"{company_name}" CEO OR "Managing Director" email{loc}')
    queries.append(f'"{company_name}" "HR Manager" OR "HR Director" contact{loc}')
    queries.append(f'"{company_name}" executive team contact{loc}')
    queries.append(f'"{company_name}" "@{domain}" contact' if domain else f'"{company_name}" email contact{loc}')

    return queries


def _extract_contacts_from_serp(html: str, company_name: str, source_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ")
    contacts = []

    # Extract any emails visible in snippets
    emails = extract_email_from_text(text)
    phones = extract_phone_from_text(text)

    # Try to pair emails with names from surrounding text
    for email in emails[:5]:
        local = email.split("@")[0]
        # Guess name from email local part
        name = local.replace(".", " ").replace("_", " ").replace("-", " ").title()
        # Look for title near email in snippet
        title = _find_title_near_email(text, email)
        contacts.append({
            "full_name": name,
            "email": email,
            "title": title,
            "phone": phones[0] if phones else None,
            "source": "google_serp",
        })

    return contacts


def _find_title_near_email(text: str, email: str) -> str | None:
    idx = text.find(email)
    if idx == -1:
        return None
    context = text[max(0, idx - 200): idx + 100]

    title_keywords = [
        "CEO", "Founder", "Director", "Manager", "Head of", "VP", "Chief",
        "HR", "Recruiter", "Talent", "Executive",
    ]
    for kw in title_keywords:
        if kw.lower() in context.lower():
            # Extract surrounding phrase
            kw_idx = context.lower().index(kw.lower())
            start = max(0, kw_idx - 10)
            end = min(len(context), kw_idx + 60)
            snippet = context[start:end].strip()
            snippet = re.sub(r"\s+", " ", snippet)
            if len(snippet) < 80:
                return snippet
    return None


def scrape_crunchbase_people(company_name: str) -> list[dict]:
    """Search Crunchbase (public pages) for company leadership."""
    session = get_session()
    slug = company_name.lower().replace(" ", "-").replace("&", "and")
    slug = re.sub(r"[^a-z0-9\-]", "", slug)
    url = f"https://www.crunchbase.com/organization/{slug}/people"

    html = fetch_url(url, session, use_scraper_api=True)
    if not html:
        return []

    polite_sleep(1.0)
    soup = BeautifulSoup(html, "html.parser")
    people = []

    # Crunchbase renders via JS, but some data is in <script> tags as JSON-LD
    for script in soup.find_all("script", type="application/json"):
        try:
            import json
            data = json.loads(script.string or "")
            extracted = _parse_crunchbase_json(data)
            people.extend(extracted)
        except Exception:
            pass

    return people[:10]


def _parse_crunchbase_json(data) -> list[dict]:
    people = []
    if isinstance(data, dict):
        # Look for person entities
        for key, val in data.items():
            if isinstance(val, dict) and "full_name" in val:
                people.append({
                    "full_name": val.get("full_name", ""),
                    "title": val.get("title") or val.get("primary_job_title"),
                    "email": None,
                    "phone": None,
                    "source": "crunchbase",
                })
            elif isinstance(val, (dict, list)):
                people.extend(_parse_crunchbase_json(val))
    elif isinstance(data, list):
        for item in data:
            people.extend(_parse_crunchbase_json(item))
    return people
