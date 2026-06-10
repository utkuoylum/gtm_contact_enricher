from __future__ import annotations
"""
Hunt for DIRECT LINES (individual contact numbers) — the hardest part.

Sources tried (in order of likelihood):
  1. LinkedIn profile — some people list phone in Contact Info section
     (accessible only via Google cache / non-logged-in view)
  2. Press releases — PR contacts often list direct numbers
  3. Conference speaker bios — often have direct lines
  4. Company staff directory — some companies expose per-person info
  5. Email signature patterns in public content (PDFs, docs)
  6. Google search: "[name] [company] direct number OR mobile"
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep

logger = logging.getLogger(__name__)

PHONE_RE = re.compile(
    r'(?:\+\d{1,3}[\s\-.]?)?(?:\(0?\d{1,5}\)[\s\-.]?)?\d{2,5}[\s\-.]?\d{3,5}(?:[\s\-.]?\d{2,5})?'
)


def find_direct_lines(full_name: str, company_name: str, domain: str = "",
                      title: str = "", location: str = "") -> list[dict]:
    """
    Returns list of {number, source, confidence, context}
    """
    results = []
    session = get_session()
    seen: set[str] = set()

    # 1. LinkedIn profile phone (via Google cache)
    li_phones = _search_linkedin_phone(full_name, company_name, session)
    results.extend(li_phones)

    # 2. Press releases mentioning this person
    pr_phones = _search_press_release_phone(full_name, company_name, domain, session)
    results.extend(pr_phones)

    # 3. Google: direct name + phone search
    google_phones = _google_person_phone(full_name, company_name, location, session)
    results.extend(google_phones)

    # 4. Company staff/team directory page for this person
    if domain:
        staff_phones = _scrape_staff_directory(full_name, domain, session)
        results.extend(staff_phones)

    # Deduplicate
    unique = []
    for r in results:
        digits = re.sub(r'\D', '', r['number'])
        if digits not in seen and 7 <= len(digits) <= 15:
            seen.add(digits)
            unique.append(r)

    return unique


def _search_linkedin_phone(full_name: str, company_name: str, session) -> list[dict]:
    """
    Search Google for this person's LinkedIn profile (cached),
    then look for phone number in the snippet/page.
    Note: LinkedIn auth walls most profile data. Google snippet sometimes
    shows contact info if the person made it public.
    """
    results = []
    query = quote_plus(f'site:linkedin.com/in "{full_name}" "{company_name}"')
    html = fetch_url(f"https://www.google.com/search?q={query}", session, use_scraper_api=True)
    if not html:
        return results
    polite_sleep(0.8)

    soup = BeautifulSoup(html, 'html.parser')
    # Extract snippets from Google results
    for snippet in soup.select('.VwiC3b, .s3v9rd, .st'):
        text = snippet.get_text(separator=' ')
        m = PHONE_RE.search(text)
        if m:
            digits = re.sub(r'\D', '', m.group(0))
            if 7 <= len(digits) <= 15:
                results.append({
                    'number': m.group(0).strip(),
                    'source': 'linkedin_google_snippet',
                    'confidence': 55,
                    'context': f'LinkedIn profile snippet for {full_name}',
                })

    # Try to access Google's cached version of LinkedIn profile
    cache_query = quote_plus(f'cache:linkedin.com/in "{full_name}" "{company_name}"')
    cache_html = fetch_url(
        f"https://www.google.com/search?q={cache_query}",
        session, use_scraper_api=True
    )
    if cache_html:
        cache_soup = BeautifulSoup(cache_html, 'html.parser')
        for a in cache_soup.find_all('a', href=re.compile(r'^tel:')):
            phone = a['href'][4:].strip()
            if phone:
                results.append({
                    'number': phone,
                    'source': 'linkedin_cache',
                    'confidence': 70,
                    'context': f'LinkedIn cache for {full_name}',
                })

    return results


def _search_press_release_phone(full_name: str, company_name: str, domain: str, session) -> list[dict]:
    """
    Press releases often have PR contact info: "Contact: Jane Smith +44 20 7946 0958"
    Search for press releases mentioning this person.
    """
    results = []
    queries = [
        f'"{full_name}" "{company_name}" press release contact phone',
        f'"{full_name}" "{company_name}" "for more information" phone',
    ]
    if domain:
        queries.append(f'site:{domain} "{full_name}" phone OR tel OR contact')

    for query in queries[:2]:
        encoded = quote_plus(query)
        html = fetch_url(
            f"https://www.google.com/search?q={encoded}&num=5",
            session, use_scraper_api=True
        )
        if not html:
            continue
        polite_sleep(0.8)

        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text(separator=' ')

        # Look for phone near person's name
        name_parts = full_name.lower().split()
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if any(p in line.lower() for p in name_parts if len(p) > 2):
                window = ' '.join(lines[max(0, i-1):i+4])
                m = PHONE_RE.search(window)
                if m:
                    digits = re.sub(r'\D', '', m.group(0))
                    if 7 <= len(digits) <= 15:
                        results.append({
                            'number': m.group(0).strip(),
                            'source': 'press_release',
                            'confidence': 65,
                            'context': f'Press release mention of {full_name}',
                        })
                        break
        if results:
            break

    return results


def _google_person_phone(full_name: str, company_name: str, location: str, session) -> list[dict]:
    """
    Direct Google search for person + phone number.
    Low success rate but worth trying for executives with public profiles.
    """
    results = []
    query = quote_plus(f'"{full_name}" "{company_name}" phone number direct')
    html = fetch_url(
        f"https://www.google.com/search?q={query}&num=5",
        session, use_scraper_api=True
    )
    if not html:
        return results
    polite_sleep(0.8)

    soup = BeautifulSoup(html, 'html.parser')
    # tel: links are most reliable
    for a in soup.find_all('a', href=re.compile(r'^tel:')):
        phone = a['href'][4:].strip()
        if phone:
            results.append({
                'number': phone,
                'source': 'google_tel_link',
                'confidence': 60,
                'context': f'Google result for {full_name} at {company_name}',
            })

    return results


def _scrape_staff_directory(full_name: str, domain: str, session) -> list[dict]:
    """
    Some companies expose per-person staff directory pages.
    e.g. domain.com/team/john-smith or domain.com/about/john-smith
    """
    results = []
    # Build slug from name
    slug = full_name.lower().replace(' ', '-')
    name_parts = full_name.lower().split()

    candidate_urls = [
        f"https://{domain}/team/{slug}",
        f"https://{domain}/people/{slug}",
        f"https://{domain}/about/{slug}",
        f"https://{domain}/staff/{slug}",
    ]

    for url in candidate_urls:
        html = fetch_url(url, session)
        if not html:
            continue
        polite_sleep(0.4)

        # Check if this page is actually about this person
        text = BeautifulSoup(html, 'html.parser').get_text(separator=' ')
        if not all(p in text.lower() for p in name_parts if len(p) > 2):
            continue

        for a in BeautifulSoup(html, 'html.parser').find_all('a', href=re.compile(r'^tel:')):
            phone = a['href'][4:].strip()
            if phone:
                results.append({
                    'number': phone,
                    'source': 'company_staff_page',
                    'confidence': 82,
                    'context': f'Staff directory page at {url}',
                })
        if results:
            break

    return results
