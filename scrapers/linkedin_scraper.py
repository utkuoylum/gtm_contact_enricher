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
from datetime import date
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep, multi_engine_search
from utils.domain_finder import extract_email_from_text
from config import DECISION_MAKER_TITLES

_CURRENT_YEAR = date.today().year

# "vor 3 Jahren" → 3 years ago; "vor einem Jahr" → 1 year ago; "vor 6 Monaten" → 0
_RELATIVE_AGO_DE = re.compile(
    r"vor\s+(?:(einem|\d+)\s+Jahr(?:en)?|(einem|\d+)\s+Monat(?:en)?)"
    , re.IGNORECASE,
)
# English: "3 years ago", "1 year ago"
_RELATIVE_AGO_EN = re.compile(r"(\d+)\s+year(?:s)?\s+ago", re.IGNORECASE)
# Absolute year in snippet: "2023", "2022"
_ABS_YEAR = re.compile(r"\b(20[12]\d)\b")

logger = logging.getLogger(__name__)

TARGET_TITLES = [
    "CEO", "Founder", "Managing Director", "Owner", "General Manager",
    "HR Director", "Head of HR", "Chief People Officer", "VP HR",
    "Talent Acquisition", "HR Manager", "Recruiting Manager",
    "Head of Talent", "CHRO", "CPO", "Director",
    # DACH titles
    "Geschäftsführer", "Geschäftsführerin", "Inhaber", "Inhaberin",
    "Personalleiter", "Personalleiterin", "Personalreferent", "Personalreferentin",
    "HR Business Partner", "Recruiter", "Recruiterin",
    "Vorstand", "Prokurist", "Prokurentin",
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
    # German: "Geschäftsführer bei Company" in snippet
    r"(Geschäftsführer(?:in)?|Inhaber(?:in)?|Personalleiter(?:in)?|Personalreferent(?:in)?|HR\s+\w+|Vorstand|Prokurist)\s+(?:bei|von|der|des|at)\s+",
]


def search_linkedin_contacts(
    company_name: str,
    location: str = "",
    job_category: str = "",
    staffing_titles: list[str] | None = None,
) -> list[dict]:
    contacts: list[dict] = []
    seen_profiles: set[str] = set()
    session = get_session()

    # Build location keywords for filtering out wrong-country results
    _loc_keywords = _location_keywords(location)

    queries = _build_queries(company_name, location, job_category, staffing_titles)

    for query in queries:
        results = _search_for_profiles(query, session)
        polite_sleep(1.5)
        for r in results:
            url = r.get("url", "")
            snippet = r.get("snippet", "")

            # If we have location info, skip results that mention a different country/city
            # (avoids matching "Excel Building Management Australia" for Berlin company)
            if _loc_keywords and not _snippet_matches_location(snippet, _loc_keywords, company_name):
                continue

            if url and url not in seen_profiles:
                seen_profiles.add(url)
                person = _parse_profile_result(r, company_name)
                if person:
                    contacts.append(person)
        if len(contacts) >= 15:
            break

    # If still empty, try Claude SERP extraction on a broad query
    if not contacts:
        try:
            from utils.claude_extractor import extract_contacts_from_serp, claude_available
            if claude_available():
                broad_q = f'"{company_name}" {location} LinkedIn Mitarbeiter OR Geschäftsführer OR Manager'
                html = multi_engine_search(broad_q, session)
                if html:
                    from bs4 import BeautifulSoup as _BS
                    text = _BS(html, "html.parser").get_text(separator=" ")
                    claude_contacts = extract_contacts_from_serp(text, company_name, location)
                    for c in claude_contacts:
                        contacts.append({**c, "source": "linkedin_claude"})
        except Exception:
            pass

    return contacts[:15]


def _location_keywords(location: str) -> set[str]:
    """Extract meaningful location keywords for snippet filtering."""
    if not location:
        return set()
    # Map known DACH cities/countries to their keywords
    loc_lower = location.lower()
    keywords: set[str] = set()
    for word in re.split(r"[\s,]+", loc_lower):
        if len(word) > 3:
            keywords.add(word)
    return keywords


def _snippet_matches_location(snippet: str, loc_keywords: set[str], company_name: str) -> bool:
    """
    Return True if the snippet is plausibly for the right location.
    Strategy: if snippet contains a clearly different geography (AU, Australia, Sydney etc.)
    and none of our target keywords, reject it.
    """
    snippet_lower = snippet.lower()

    # If any of our location keywords appear → accept
    if any(kw in snippet_lower for kw in loc_keywords):
        return True

    # List of foreign geography markers that signal wrong country
    _foreign_markers = {
        "australia", "sydney", "melbourne", "brisbane", "perth", "adelaide",
        "new zealand", "auckland", "united states", "united kingdom", "london",
        "new york", "los angeles", "chicago", "toronto", "canada",
        "singapore", "hong kong", "dubai", "india", "mumbai",
    }
    if any(marker in snippet_lower for marker in _foreign_markers):
        return False

    # No clear location signal — accept (be permissive)
    return True


def _build_queries(
    company_name: str,
    location: str,
    job_category: str,
    staffing_titles: list[str] | None = None,
) -> list[str]:
    site = "site:linkedin.com/in"
    name_q = f'"{company_name}"'
    loc = f'"{location}"' if location else ""

    # Detect DACH company for German-specific queries
    _dach_locs = {
        "hamburg", "berlin", "münchen", "munich", "frankfurt", "köln", "cologne",
        "düsseldorf", "stuttgart", "hannover", "germany", "deutschland",
        "austria", "österreich", "switzerland", "schweiz", "wien", "zürich",
    }
    is_dach = any(d in location.lower() for d in _dach_locs) if location else False

    queries = []

    # Staffing-focused query (always first — these are the most valuable contacts)
    if staffing_titles:
        # Build two queries: first 6 titles, next 6 titles (OR clauses stay readable)
        for chunk in [staffing_titles[:6], staffing_titles[6:12]]:
            if chunk:
                title_expr = " OR ".join(f'"{t}"' for t in chunk)
                queries.append(f'{site} {name_q} ({title_expr}) {loc}')

    queries += [
        # HR/People ops roles
        f'{site} {name_q} ("HR Director" OR "Head of HR" OR "Chief People" OR "Talent Acquisition" OR "CHRO") {loc}',
        # C-suite
        f'{site} {name_q} (CEO OR Founder OR "Managing Director" OR Owner OR "General Manager") {loc}',
        # HR Managers
        f'{site} {name_q} ("HR Manager" OR "Recruiting Manager" OR "Head of Talent" OR "People Manager") {loc}',
    ]

    if is_dach:
        queries.append(
            f'{site} {name_q} (Geschäftsführer OR Inhaber OR Personalleiter OR Personalleiterin) {loc}'
        )
        queries.append(
            f'{site} {name_q} (Personalreferent OR "HR Business Partner" OR Recruiter OR Prokurist) {loc}'
        )
        queries.append(f'site:linkedin.com/company {name_q} Mitarbeiter OR Mitarbeiterinnen')

    if job_category:
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
        "year_found": _extract_year_from_snippet(snippet),
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


def _extract_year_from_snippet(snippet: str) -> int | None:
    """Estimate the year a LinkedIn profile was last active from SERP snippet text."""
    # "vor 3 Jahren" / "vor einem Jahr"
    m = _RELATIVE_AGO_DE.search(snippet)
    if m:
        years_str = m.group(1)
        if years_str:
            n = 1 if years_str.lower() == "einem" else int(years_str)
            return _CURRENT_YEAR - n
        # months → still current year
        return _CURRENT_YEAR

    # "3 years ago"
    m = _RELATIVE_AGO_EN.search(snippet)
    if m:
        return _CURRENT_YEAR - int(m.group(1))

    # Absolute year mention (e.g. "updated 2024", "· 2023 ·")
    years = _ABS_YEAR.findall(snippet)
    if years:
        return max(int(y) for y in years)

    return None
