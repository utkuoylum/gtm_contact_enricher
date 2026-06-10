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

    # 1. Bing News
    bing_results = _bing_news_search(company_name, session)
    all_text_blocks.extend(bing_results)

    # 2. Google News RSS
    gnews_results = _google_news_rss(company_name, session)
    all_text_blocks.extend(gnews_results)

    # 3. PR Newswire
    if len(all_text_blocks) < 3:
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


def _is_valid_name(name: str) -> bool:
    parts = name.split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    # All parts should start with uppercase and be mostly alpha
    for p in parts:
        if not p[0].isupper():
            return False
        if not re.match(r"^[A-Za-z\-'\.]+$", p):
            return False
    # Reject common false positives
    false_positives = {"New York", "United States", "United Kingdom", "Los Angeles",
                       "San Francisco", "Press Release", "Chief Executive"}
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
