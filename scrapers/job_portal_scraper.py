from __future__ import annotations
"""
Job portal scraper — extracts HR/recruiter contact names from job postings.

Sources (all free, no authentication):
  - stepstone.de        : Germany's leading job board (~25% of listings name a Kontaktperson)
  - linkedin.com/jobs   : LinkedIn jobs-guest API (public endpoint, no login needed)
  - indeed.de           : Indeed Germany job listings
  - xing.com/jobs       : XING Jobs (DACH-specific)

Key insight: German job postings often include a named contact person:
  "Ansprechpartner: [Name]", "Ihre Ansprechpartnerin: [Name]", "Kontaktperson: [Name]",
  "Wenden Sie sich an [Name]", "Rückfragen: [Name]"
These are direct HR/recruiting contacts with authority to screen candidates.
"""
import re
import logging
from urllib.parse import quote_plus, unquote
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep, multi_engine_search
from utils.domain_finder import extract_email_from_text, extract_phone_from_text

logger = logging.getLogger(__name__)

# LinkedIn-specific: "Den Jobinserenten von [Company] direkt kontaktieren\n  [Name]"
# Note: LinkedIn HTML has many whitespace/newline chars between the trigger line and name
_LINKEDIN_RECRUITER = re.compile(
    r"direkt kontaktieren\s+"
    r"([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)",
    re.MULTILINE,
)
# English: "Message the job poster\n  [Name]"
_LINKEDIN_RECRUITER_EN = re.compile(
    r"Message the job poster\s+"
    r"([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)",
    re.MULTILINE,
)

# German job posting contact patterns (ordered by reliability)
_CONTACT_PATTERNS = [
    # "Ansprechpartner(in): Max Mustermann"
    re.compile(
        r"(?:Ansprechpartner(?:in)?|Kontaktperson)[:\s]+"
        r"([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)",
        re.MULTILINE,
    ),
    # "Ihre(r) Ansprechpartnerin: [Name]"
    re.compile(
        r"Ihre(?:r)?\s+Ansprechpartner(?:in)?[:\s]+"
        r"([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)",
        re.MULTILINE,
    ),
    # "Wenden Sie sich an [Name]"
    re.compile(
        r"Wenden Sie sich an[:\s]+"
        r"([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)",
        re.MULTILINE,
    ),
    # "Rückfragen an: [Name]"
    re.compile(
        r"(?:Rückfragen(?:\s+an)?|Bewerbungen\s+an)[:\s]+"
        r"([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)",
        re.MULTILINE,
    ),
    # "HR Manager: [Name]" / "Personalreferent: [Name]"
    re.compile(
        r"(?:Personalleiter(?:in)?|Personalleiterin|HR Manager|HR Business Partner"
        r"|Personalreferent(?:in)?|Recruiting Manager|Head of HR|Recruiter(?:in)?)"
        r"[:\s]+([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?:\s[A-ZÜÖÄ][a-züöäß\-]+)?)",
        re.MULTILINE,
    ),
]

_NON_NAME_JOB = {
    "die", "der", "das", "und", "oder", "für", "mit", "bei", "ihre", "ihr",
    "wir", "sie", "uns", "alle", "mehr", "team", "resources", "personal",
    "management", "recruiting", "talent", "acquisition", "human", "kontakt",
    "mailto", "info", "jobs", "karriere", "bewerben",
    # English corporate words that look like name parts
    "service", "services", "desks", "desk", "support", "helpdesk",
    "center", "center", "department", "staff", "office",
    # German corporate words
    "unsere", "unser", "ihre", "unserer", "abteilung", "stelle", "stellen",
    "ansprechpartner", "ansprechpartnerin", "kontaktperson",
}

_HR_TITLE_MAP = [
    ("personalleiter", "Personalleiter"),
    ("personalleiterin", "Personalleiterin"),
    ("hr manager", "HR Manager"),
    ("hr director", "HR Director"),
    ("hr business partner", "HR Business Partner"),
    ("personalreferent", "Personalreferent"),
    ("personalreferentin", "Personalreferentin"),
    ("recruiting manager", "Recruiting Manager"),
    ("talent acquisition", "Talent Acquisition"),
    ("head of hr", "Head of HR"),
    ("recruiter", "Recruiter"),
    ("personalverantwortlich", "Personalverantwortlicher"),
    ("personalwesen", "Personalwesen"),
    ("chief people", "Chief People Officer"),
]


def find_job_portal_contacts(company_name: str, location: str = "") -> list[dict]:
    """Find recruiter/HR contacts from German job portals."""
    session = get_session()
    contacts: list[dict] = []

    # 1. LinkedIn jobs-guest API — most reliable, no auth needed
    lj = _scrape_linkedin_jobs(company_name, location, session)
    contacts.extend(lj)

    # 2. StepStone.de — SERP-based, finds jobs with named contacts
    if len(contacts) < 3:
        ss = _scrape_stepstone(company_name, location, session)
        contacts.extend(ss)

    # 3. Indeed.de — fallback
    if len(contacts) < 3:
        ind = _scrape_indeed_de(company_name, location, session)
        contacts.extend(ind)

    # 4. SERP-based job posting mining (catches Monster, Xing, company careers pages)
    if len(contacts) < 3:
        serp = _serp_job_search(company_name, location, session)
        contacts.extend(serp)

    return _dedupe(contacts)[:8]


def _scrape_stepstone(company_name: str, location: str, session) -> list[dict]:
    contacts = []
    city = location.split(",")[0].strip() if location else "Deutschland"
    company_kw = company_name.lower().split()[0]

    # SERP to find StepStone + Monster + XING listings with named contacts
    query = (
        f'"{company_name}" {city} '
        f'(Ansprechpartner OR Kontaktperson OR Personalreferent OR "HR Manager")'
        f' (stepstone OR monster OR xing OR stellenangebote)'
    )
    html = multi_engine_search(query, session)
    if not html:
        return []

    polite_sleep(0.8)
    soup = BeautifulSoup(html, "html.parser")

    # Collect job listing URLs — handle both Google (/url?q=) and DDG (?uddg=) formats
    job_urls: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Google redirect
        m = re.search(r"/url\?q=(https?://[^&]+)", href)
        if m:
            href = unquote(m.group(1))
        # DuckDuckGo redirect
        m2 = re.search(r"uddg=([^&]+)", href)
        if m2:
            href = unquote(m2.group(1))
        if re.search(r"(stepstone|monster|xing)\.de", href) and re.search(r"/(stellenangebote|job|jobs|stellen)/", href):
            job_urls.append(re.sub(r"\?.*$", "", href))

    # Fetch individual job listings
    for url in list(dict.fromkeys(job_urls))[:5]:
        jhtml = fetch_url(url, session)
        if not jhtml or company_kw not in jhtml.lower():
            continue
        polite_sleep(0.4)
        jc = _extract_from_job_html(jhtml, company_name)
        contacts.extend(jc)
        if len(contacts) >= 3:
            break

    # Mine SERP snippets directly for contact names
    text = soup.get_text(separator="\n")
    if company_kw in text.lower():
        contacts.extend(_extract_from_text(text, company_name))

    return contacts


def _scrape_linkedin_jobs(company_name: str, location: str, session) -> list[dict]:
    contacts = []
    location_str = location.split(",")[0].strip() if location else "Germany"

    # LinkedIn jobs-guest API — served to crawlers/search engines, no login needed
    api_url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={quote_plus(company_name)}&location={quote_plus(location_str)}&start=0"
    )
    html = fetch_url(api_url, session)
    if not html:
        # Fallback to main jobs search page
        html = fetch_url(
            f"https://www.linkedin.com/jobs/search"
            f"?keywords={quote_plus(company_name)}&location={quote_plus(location_str)}",
            session,
        )
    if not html:
        return []

    polite_sleep(0.8)
    soup = BeautifulSoup(html, "html.parser")
    company_kw = company_name.lower().split()[0]

    # Collect job detail URLs — handle Google + DDG link formats
    job_urls: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/url\?q=(https?://[^&]+)", href)
        if m:
            href = unquote(m.group(1))
        m2 = re.search(r"uddg=([^&]+)", href)
        if m2:
            href = unquote(m2.group(1))
        if "linkedin.com/jobs/view/" in href:
            job_urls.append(re.sub(r"\?.*$", "", href))

    for url in list(dict.fromkeys(job_urls))[:6]:
        jhtml = fetch_url(url, session)
        if not jhtml or company_kw not in jhtml.lower():
            continue
        polite_sleep(0.4)
        jc = _extract_from_job_html(jhtml, company_name)
        contacts.extend(jc)
        if len(contacts) >= 3:
            break

    # Parse search results page text
    text = soup.get_text(separator="\n")
    if company_kw in text.lower():
        contacts.extend(_extract_from_text(text, company_name))

    return contacts


def _scrape_indeed_de(company_name: str, location: str, session) -> list[dict]:
    city = location.split(",")[0].strip() if location else "Deutschland"
    url = f"https://de.indeed.com/jobs?q={quote_plus(company_name)}&l={quote_plus(city)}"
    html = fetch_url(url, session)
    if not html:
        return []

    polite_sleep(0.8)
    soup = BeautifulSoup(html, "html.parser")
    company_kw = company_name.lower().split()[0]
    contacts = []

    # Parse page text
    text = soup.get_text(separator="\n")
    if company_kw in text.lower():
        contacts.extend(_extract_from_text(text, company_name))

    # Fetch individual job pages
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/viewjob" in href or "/rc/clk" in href:
            full_url = href if href.startswith("http") else f"https://de.indeed.com{href}"
            jhtml = fetch_url(full_url, session)
            if jhtml and company_kw in jhtml.lower():
                polite_sleep(0.3)
                contacts.extend(_extract_from_job_html(jhtml, company_name))
        if len(contacts) >= 3:
            break

    return contacts


def _serp_job_search(company_name: str, location: str, session) -> list[dict]:
    """
    Broad SERP search for job postings with named contact persons.
    Catches company career pages, Arbeitsagentur listings, and smaller job portals.
    """
    city = location.split(",")[0].strip() if location else ""
    contacts = []
    company_kw = company_name.lower().split()[0]

    queries = [
        f'"{company_name}" {city} Ansprechpartner OR Kontaktperson HR Bewerbung',
        f'"{company_name}" {city} Personalreferent OR Recruiter Kontakt email',
    ]

    for query in queries[:2]:
        html = multi_engine_search(query, session)
        if not html:
            continue
        polite_sleep(0.5)

        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n")

        if company_kw not in text.lower():
            continue

        contacts.extend(_extract_from_text(text, company_name))

        # Fetch any linked pages that might have contact details
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/url\?q=(https?://[^&]+)", href)
            if m:
                href = unquote(m.group(1))
            m2 = re.search(r"uddg=([^&]+)", href)
            if m2:
                href = unquote(m2.group(1))
            if not href.startswith("http"):
                continue
            if any(x in href for x in ["bing.com", "google.com", "duckduckgo.com"]):
                continue
            if company_kw in a.get_text().lower() or "ansprechpartner" in a.get_text().lower():
                jhtml = fetch_url(href, session)
                if jhtml and company_kw in jhtml.lower():
                    polite_sleep(0.3)
                    contacts.extend(_extract_from_job_html(jhtml, company_name))
                if len(contacts) >= 3:
                    break

        if len(contacts) >= 3:
            break

    return contacts


def _extract_from_job_html(html: str, company_name: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    contacts = _extract_from_text(text, company_name)

    emails = extract_email_from_text(text)
    phones = extract_phone_from_text(text)

    for c in contacts:
        if not c.get("email") and emails:
            for e in emails:
                if any(p.lower() in e.lower() for p in c["full_name"].split() if len(p) > 2):
                    c["email"] = e
                    break
        if not c.get("phone") and phones:
            c["phone"] = phones[0]

    return contacts


def _extract_from_text(text: str, company_name: str) -> list[dict]:
    contacts = []
    seen: set[str] = set()
    company_kw = company_name.lower().split()[0]

    if company_kw not in text.lower():
        return []

    # LinkedIn-specific recruiter pattern (most reliable)
    for pattern in [_LINKEDIN_RECRUITER, _LINKEDIN_RECRUITER_EN]:
        for match in pattern.finditer(text):
            name = match.group(1).strip()
            if _is_valid_name(name) and name not in seen:
                seen.add(name)
                contacts.append({
                    "full_name": name,
                    "title": "Recruiter",
                    "email": None,
                    "phone": None,
                    "source": "linkedin_jobs",
                })

    for pattern in _CONTACT_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1).strip()
            if not _is_valid_name(name) or name in seen:
                continue
            seen.add(name)
            ctx = text[max(0, match.start() - 50):match.end() + 200]
            contacts.append({
                "full_name": name,
                "title": _infer_title(ctx),
                "email": None,
                "phone": None,
                "source": "job_portal",
            })

    return contacts


def _infer_title(ctx: str) -> str:
    ctx_lower = ctx.lower()
    for keyword, role in _HR_TITLE_MAP:
        if keyword in ctx_lower:
            return role
    return "HR Ansprechpartner"


def _is_valid_name(name: str) -> bool:
    parts = name.split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    for p in parts:
        if not re.match(r"^[A-ZÜÖÄ][a-züöäß\-]+$", p):
            return False
        if p.lower() in _NON_NAME_JOB:
            return False
        if len(p) < 2 or len(p) > 30:
            return False
    return True


def _dedupe(contacts: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result = []
    for c in contacts:
        key = c.get("full_name", "").lower()
        if key and key not in seen:
            seen.add(key)
            result.append(c)
    return result
