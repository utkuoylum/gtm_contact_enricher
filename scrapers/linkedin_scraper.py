from __future__ import annotations
"""
LinkedIn contact discovery via search engines — no LinkedIn auth needed.

Approach:
  1. site:linkedin.com/in searches for profiles (Google, Bing, DDG)
  2. Name extracted from URL slug (reliable)
  3. Title extracted from snippet (multiple format patterns)
  4. Email hints sometimes appear in bio text (some users publish them)
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep, multi_engine_search
from utils.domain_finder import extract_email_from_text
from config import DECISION_MAKER_TITLES

logger = logging.getLogger(__name__)

TARGET_TITLES = [
    "CEO", "Founder", "Managing Director", "Owner", "General Manager",
    "HR Director", "Head of HR", "Chief People Officer", "VP HR",
    "Talent Acquisition", "HR Manager", "Recruiting Manager",
    "Head of Talent", "CHRO", "CPO", "Director",
]

_SNIPPET_TITLE_PATTERNS = [
    # "Name · Title at Company"
    r"[·•]\s*(.+?)\s+(?:at|@|bei|chez|at)\s+.{2,40}(?:\||$)",
    # "Name · Title · Company"
    r"[·•]\s*(.+?)\s*[·•]",
    # "Name — Title"
    r"[-–—]\s*(.{5,60}?)\s*(?:\||$|\n)",
    # Title appears after comma: "Name, Title at Company"
    r",\s*(.{5,60}?)\s+(?:at|@|bei)\s+",
    # "Title · Company": name already in URL, title first in snippet
    r"^([A-Za-z /&\-]{5,60}?)\s*[·•|]",
]


def search_linkedin_contacts(company_name: str, location: str = "", job_category: str = "") -> list[dict]:
    contacts: list[dict] = []
    seen_profiles: set[str] = set()
    session = get_session()

    queries = _build_queries(company_name, location, job_category)

    for query in queries:
        results = _search_for_profiles(query, session)
        polite_sleep(1.5)
        for r in results:
            url = r.get("url", "")
            if url and url not in seen_profiles:
                seen_profiles.add(url)
                person = _parse_profile_result(r, company_name)
                if person:
                    contacts.append(person)
        if len(contacts) >= 15:
            break

    return contacts[:15]


def _build_queries(company_name: str, location: str, job_category: str) -> list[str]:
    site = "site:linkedin.com/in"
    name_q = f'"{company_name}"'
    loc = f'"{location}"' if location else ""

    queries = [
        # HR/People ops roles — highest priority for job agency
        f'{site} {name_q} ("HR Director" OR "Head of HR" OR "Chief People" OR "Talent Acquisition" OR "CHRO") {loc}',
        # C-suite
        f'{site} {name_q} (CEO OR Founder OR "Managing Director" OR Owner OR "General Manager") {loc}',
        # HR Managers
        f'{site} {name_q} ("HR Manager" OR "Recruiting Manager" OR "Head of Talent" OR "People Manager") {loc}',
    ]

    if job_category:
        # Relevant category manager
        queries.append(f'{site} {name_q} "{job_category}" manager OR director {loc}')

    # Broad sweep
    queries.append(f'{site} {name_q} {loc}')

    return queries


def _search_for_profiles(query: str, session) -> list[dict]:
    html = multi_engine_search(query, session)
    if not html:
        return []
    return _parse_search_html(html)


def _parse_search_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Generic link extraction — works across search engines
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Unwrap Google redirect
        m = re.search(r"/url\?q=(https?://[^&]+)", href)
        if m:
            href = m.group(1)

        if "linkedin.com/in/" not in href:
            continue

        url_clean = re.sub(r"\?.*$", "", href)
        if not re.search(r"linkedin\.com/in/[a-z0-9\-]+", url_clean):
            continue

        # Get surrounding snippet text
        parent = a.find_parent(["div", "li", "article", "section"])
        snippet = parent.get_text(separator=" ", strip=True) if parent else a.get_text()
        results.append({"url": url_clean, "snippet": snippet})

    return results


def _parse_profile_result(result: dict, company_name: str) -> dict | None:
    url = result.get("url", "")
    snippet = result.get("snippet", "")

    # Extract name from URL slug
    slug_match = re.search(r"/in/([a-z0-9][a-z0-9\-]+)", url)
    if not slug_match:
        return None

    slug = slug_match.group(1)
    # Remove trailing ID numbers: "john-smith-12345678" → "john-smith"
    slug = re.sub(r"-\d{5,}$", "", slug)
    slug = re.sub(r"-\d+$", "", slug)

    name_parts = [w for w in slug.split("-") if w.isalpha() and len(w) > 1]
    if len(name_parts) < 2:
        return None

    name = " ".join(w.capitalize() for w in name_parts[:3])

    # Extract title from snippet
    title = _extract_title(snippet, company_name)

    # Extract email if someone published it in their bio (uncommon but happens)
    emails = extract_email_from_text(snippet)
    email = emails[0] if emails else None

    return {
        "full_name": name,
        "title": title,
        "linkedin_url": url,
        "email": email,
        "phone": None,
        "source": "linkedin_google",
    }


def _extract_title(snippet: str, company_name: str) -> str | None:
    # Remove company name to reduce noise
    clean = re.sub(re.escape(company_name), "", snippet, flags=re.IGNORECASE)

    for pat in _SNIPPET_TITLE_PATTERNS:
        m = re.search(pat, clean, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip(" ·|-–—,")
            # Sanity check: not too long, not a URL, has real content
            if 4 < len(candidate) < 80 and "http" not in candidate:
                return candidate

    # Last resort: find known role keyword in snippet
    all_kws = [kw for kws in DECISION_MAKER_TITLES.values() for kw in kws]
    snippet_lower = snippet.lower()
    for kw in sorted(all_kws, key=len, reverse=True):
        if kw in snippet_lower:
            idx = snippet_lower.index(kw)
            return snippet[idx: idx + len(kw) + 30].strip()

    return None
