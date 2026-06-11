from __future__ import annotations
"""
News & press release scraper — finds executive names from:
  1. Bing News (free, no key)
  2. Google News RSS (free)
  3. PR Newswire search (free public pages)

Press releases almost always name the CEO/PR contact, making this
a reliable source for finding decision-makers at any company size.
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep, multi_engine_search
from utils.domain_finder import extract_email_from_text

logger = logging.getLogger(__name__)

# Common roles mentioned in press releases
_EXEC_ROLE_PATTERNS = [
    r"(?:CEO|Chief Executive Officer),?\s+([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)",
    r"([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?),?\s+(?:CEO|Chief Executive Officer)",
    r"([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?),?\s+(?:Founder|Co-Founder|Managing Director|MD)",
    r"(?:Managing Director|MD),?\s+([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)",
    r"([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?),?\s+(?:HR Director|Head of HR|Chief People Officer|CHRO|VP of HR|VP HR)",
    r"(?:HR Director|Chief People Officer|CHRO|Head of HR),?\s+([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)",
    r"([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?),?\s+(?:Director|VP|Vice President|President)\s+of\s+\w+",
    r"(?:said|says|commented|stated|added|explained)\s+([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?),?\s+(?:CEO|MD|Director|Founder|Head|Chief|VP)",
    r"contact[:\s]+([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)",
]

_ROLE_CONTEXT_PATTERN = re.compile(
    r"([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)"
    r",?\s+"
    r"(CEO|CTO|CFO|COO|CHRO|CPO|Founder|Co-Founder|Managing Director|MD|"
    r"Director|VP|Vice President|President|Head of|Chief|Owner|Partner|"
    r"HR Director|HR Manager|Talent|Recruiting)"
)


def find_executives_in_news(company_name: str, location: str = "") -> list[dict]:
    """Search news/press releases for executives. Returns list of person dicts."""
    session = get_session()
    all_text_blocks: list[str] = []

    # Detect DACH company
    _dach_locs = {
        "hamburg", "berlin", "münchen", "munich", "frankfurt", "köln",
        "düsseldorf", "stuttgart", "hannover", "germany", "deutschland",
        "austria", "österreich", "switzerland", "schweiz",
    }
    is_dach = any(d in location.lower() for d in _dach_locs) if location else False

    # 1. Bing News
    bing_results = _bing_news_search(company_name, session)
    all_text_blocks.extend(bing_results)

    # 2. Google News RSS
    gnews_results = _google_news_rss(company_name, session)
    all_text_blocks.extend(gnews_results)

    # 3. German-specific: Google News DE + Presseportal SERP
    if is_dach:
        de_results = _google_news_rss_de(company_name, session)
        all_text_blocks.extend(de_results)
        pp_results = _presseportal_serp(company_name, session)
        all_text_blocks.extend(pp_results)

    # 4. PR Newswire (fallback for non-DACH)
    if not is_dach and len(all_text_blocks) < 3:
        pr_results = _prnewswire_search(company_name, session)
        all_text_blocks.extend(pr_results)

    # Parse executives from collected text
    contacts = _extract_executives(all_text_blocks, company_name)
    return contacts[:10]


def _bing_news_search(company_name: str, session) -> list[str]:
    query = quote_plus(f'"{company_name}" CEO OR director OR "managing director" OR "HR"')
    url = f"https://www.bing.com/news/search?q={query}&format=rss"
    html = fetch_url(url, session)
    if not html:
        url = f"https://www.bing.com/news/search?q={query}"
        html = fetch_url(url, session)
    if not html:
        return []

    polite_sleep(0.8)
    soup = BeautifulSoup(html, "lxml-xml" if "<?xml" in html[:100] else "html.parser")
    snippets = []

    # RSS items
    for item in soup.find_all(["item", "article"]):
        title = item.find(["title", "h3", "h4"])
        desc = item.find(["description", "summary", "p"])
        text = f"{title.get_text() if title else ''} {desc.get_text() if desc else ''}"
        if company_name.lower().split()[0] in text.lower():
            snippets.append(text)

    # HTML news cards
    for card in soup.select(".news-card, .b_algo, article"):
        text = card.get_text(separator=" ", strip=True)
        if company_name.lower().split()[0] in text.lower():
            snippets.append(text)

    return snippets[:10]


def _google_news_rss(company_name: str, session) -> list[str]:
    query = quote_plus(f'"{company_name}" CEO OR director')
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    html = fetch_url(url, session)
    if not html:
        return []

    polite_sleep(0.8)
    soup = BeautifulSoup(html, "lxml-xml")
    snippets = []
    for item in soup.find_all("item"):
        title = item.find("title")
        desc = item.find("description")
        text = f"{title.get_text() if title else ''} {desc.get_text() if desc else ''}"
        snippets.append(text)

    return snippets[:10]


def _prnewswire_search(company_name: str, session) -> list[str]:
    query = quote_plus(company_name)
    url = f"https://www.prnewswire.com/rss/news-releases-list.rss?company={query}"
    html = fetch_url(url, session)
    if not html:
        return []

    polite_sleep(0.5)
    soup = BeautifulSoup(html, "lxml-xml")
    snippets = []
    for item in soup.find_all("item"):
        desc = item.find("description")
        if desc:
            snippets.append(desc.get_text())

    return snippets[:5]


def _google_news_rss_de(company_name: str, session) -> list[str]:
    """Google News RSS in German — finds DACH press coverage."""
    query = quote_plus(f'"{company_name}" Geschäftsführer OR Inhaber OR CEO OR Personalleiter')
    url = f"https://news.google.com/rss/search?q={query}&hl=de&gl=DE&ceid=DE:de"
    html = fetch_url(url, session)
    if not html:
        return []

    polite_sleep(0.5)
    soup = BeautifulSoup(html, "lxml-xml")
    snippets = []
    for item in soup.find_all("item"):
        title = item.find("title")
        desc = item.find("description")
        text = f"{title.get_text() if title else ''} {desc.get_text() if desc else ''}"
        snippets.append(text)

    return snippets[:8]


def _presseportal_serp(company_name: str, session) -> list[str]:
    """Mine Presseportal.de snippets via SERP for executive mentions."""
    query = f'site:presseportal.de "{company_name}" Geschäftsführer OR Pressekontakt'
    html = multi_engine_search(query, session)
    if not html:
        return []

    polite_sleep(0.5)
    soup = BeautifulSoup(html, "html.parser")
    snippets = []
    company_kw = company_name.lower().split()[0]

    for result in soup.select(".b_algo, .g, [class*='result']"):
        text = result.get_text(separator=" ", strip=True)
        if company_kw in text.lower():
            snippets.append(text)

    # Also add raw page text
    full_text = soup.get_text(separator="\n")
    if company_kw in full_text.lower():
        snippets.append(full_text[:3000])

    return snippets[:5]


def _extract_executives(text_blocks: list[str], company_name: str) -> list[dict]:
    """Extract names+roles from text blocks."""
    found: dict[str, dict] = {}  # name → person dict

    for text in text_blocks:
        # Clean HTML entities
        text = re.sub(r"<[^>]+>", " ", text)
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = re.sub(r"\s+", " ", text)

        emails = extract_email_from_text(text)

        # Try explicit role patterns first
        for pattern in _EXEC_ROLE_PATTERNS:
            for match in re.finditer(pattern, text):
                name = match.group(1).strip()
                if _is_valid_name(name):
                    if name not in found:
                        found[name] = {
                            "full_name": name,
                            "title": _guess_role_from_context(text, name),
                            "email": None,
                            "phone": None,
                            "source": "news_press_release",
                        }
                    # Try to attach email
                    if not found[name]["email"]:
                        name_parts = name.lower().split()
                        for email in emails:
                            local = email.split("@")[0].lower()
                            if any(p in local for p in name_parts if len(p) > 2):
                                found[name]["email"] = email
                                break

        # Broader pattern: "Name, Role" pairs
        for match in _ROLE_CONTEXT_PATTERN.finditer(text):
            name = match.group(1).strip()
            role_hint = match.group(2).strip()
            if _is_valid_name(name) and name not in found:
                found[name] = {
                    "full_name": name,
                    "title": role_hint,
                    "email": None,
                    "phone": None,
                    "source": "news_press_release",
                }

    return list(found.values())


# Common English/German words that are NOT name components
_NON_NAME_WORDS = {
    "new", "old", "big", "small", "great", "good", "bad", "high", "low",
    "the", "and", "for", "with", "from", "this", "that", "those", "these",
    "our", "your", "their", "its", "has", "have", "will", "been", "being",
    "letter", "urging", "outside", "inside", "under", "over", "more", "less",
    "press", "release", "report", "update", "today", "yesterday", "monday",
    "tuesday", "wednesday", "thursday", "friday", "about", "also", "only",
    "just", "than", "then", "when", "where", "while", "which", "there",
    "market", "company", "group", "global", "world", "national", "international",
    "north", "south", "east", "west", "central", "united", "states", "city",
    "york", "angeles", "francisco", "london", "berlin", "munich", "frankfurt",
    "chief", "executive", "officer", "director", "manager", "head",  # titles, not names
    "sales", "marketing", "finance", "legal", "human", "resources",
    "new", "year", "quarter", "annual", "monthly", "weekly", "daily",
}


def _is_valid_name(name: str) -> bool:
    parts = name.split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    # All parts should start with uppercase and be mostly alpha
    for p in parts:
        if not p[0].isupper():
            return False
        if not re.match(r"^[A-Za-zÄÖÜäöüß\-'\.]+$", p):
            return False
        # Reject if any part is a known non-name word
        if p.lower() in _NON_NAME_WORDS:
            return False
        # Reject very short or very long parts
        if len(p) < 2 or len(p) > 25:
            return False
    # Reject known false positive phrases
    false_positives = {
        "New York", "United States", "United Kingdom", "Los Angeles",
        "San Francisco", "Press Release", "Chief Executive",
        "Managing Director", "Human Resources",
    }
    return name not in false_positives


def _guess_role_from_context(text: str, name: str) -> str | None:
    idx = text.find(name)
    if idx == -1:
        return None
    context = text[max(0, idx - 20): idx + len(name) + 80]
    roles = [
        "CEO", "Chief Executive Officer", "Managing Director", "MD",
        "Founder", "Co-Founder", "CFO", "CTO", "COO", "CHRO",
        "HR Director", "Chief People Officer", "VP", "Vice President",
        "Director", "President", "Owner", "Partner", "Head of",
    ]
    for role in roles:
        if role.lower() in context.lower():
            return role
    return None
