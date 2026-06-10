from __future__ import annotations
"""
OpenRegister.de + OffeneRegister.de + SERP-based German officer search.

Sources (priority order):
  1. SERP snippet mining — search "[company] Geschäftsführer" on DuckDuckGo/Bing.
     Most reliable: snippets almost always name the GF for German companies.
  2. OffeneRegister Datasette API (free, 5M+ companies, CC-BY)
  3. OpenRegister.de API (requires OPENREGISTER_API_KEY)
  4. Northdata.com web scraping (Handelsregister + Bundesanzeiger aggregator)
"""
import os
import re
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
from utils.http_client import REQUEST_TIMEOUT, polite_sleep, get_session, fetch_url, multi_engine_search

logger = logging.getLogger(__name__)

OPENREGISTER_API_KEY = os.getenv("OPENREGISTER_API_KEY", "")
OPENREGISTER_BASE = "https://api.openregister.de"
# OffeneRegister Datasette endpoint (free, CC-BY licensed, 5M+ German companies)
OFFENEREGISTER_DATASETTE = "https://db.offeneregister.de"

_GERMAN_OFFICER_ROLES = [
    ("Geschäftsführer", "Geschäftsführer"),
    ("Geschäftsführerin", "Geschäftsführerin"),
    ("Inhaber", "Inhaber"),
    ("Inhaberin", "Inhaberin"),
    ("Prokurist", "Prokurist"),
    ("Prokuristin", "Prokuristin"),
    ("Vorstand", "Vorstand"),
    ("Gesellschafter", "Gesellschafter"),
    ("Gründer", "Gründer"),
    ("Eigentümer", "Eigentümer"),
    ("Managing Director", "Managing Director"),
]

# Regex patterns to extract Name+Role pairs from SERP snippets
_SNIPPET_PATTERNS = [
    # "Geschäftsführer: Max Müller"
    r"(?:Gesch[äa]ftsf[üu]hrer(?:in)?|Inhaber(?:in)?|Prokurist(?:in)?|Vorstand|Gesellschafter|Eigent[üu]mer)"
    r"\s*[:\-–]\s*"
    r"([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)",
    # "Max Müller (Geschäftsführer)" or "Max Müller, Geschäftsführer"
    r"([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)"
    r"\s*[,\(]?\s*"
    r"(?:Gesch[äa]ftsf[üu]hrer(?:in)?|Inhaber(?:in)?|Prokurist(?:in)?|CEO|Managing Director)",
    # "von Geschäftsführer Max Müller" or "CEO Max Müller"
    r"(?:CEO|CTO|CFO|COO|Managing Director|Geschäftsführer)\s+"
    r"([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)",
]

# These words should NOT appear as name components (includes German roles, boolean ops)
_NON_NAME_WORDS = {
    "gmbh", "ag", "kg", "ug", "srl", "ltd", "inc", "und", "der", "die", "das",
    "mit", "von", "zur", "beim", "für", "über", "mehr", "info", "kontakt",
    "service", "support", "team", "news", "blog", "jobs", "karriere",
    # German officer titles (appear in query text, not names)
    "inhaber", "inhaberin", "prokurist", "prokuristin", "vorstand",
    "geschäftsführer", "gesellschafter", "gründer", "eigentümer",
    # Boolean operators and query keywords
    "or", "and", "not", "in", "site", "mailto",
    # UI words
    "jetzt", "upgraden", "premium", "login", "anmelden", "weitere",
}


def find_german_register_officers(company_name: str, location: str = "") -> list[dict]:
    """
    Query German commercial registers for company officers.
    Returns list of {full_name, title, source} dicts.
    """
    contacts = []
    seen_names: set[str] = set()

    def _add(new_contacts: list[dict]):
        for c in new_contacts:
            key = c.get("full_name", "").lower()
            if key and key not in seen_names and len(key.split()) >= 2:
                seen_names.add(key)
                contacts.append(c)

    # 1. SERP snippet mining (might be blocked locally, always reliable on VPS)
    _add(_serp_search_german_officers(company_name, location))

    # 2. OffeneRegister via Datasette
    if len(contacts) < 3:
        _add(_query_offeneregister_datasette(company_name, location))

    # 3. OpenRegister (requires API key)
    if OPENREGISTER_API_KEY and len(contacts) < 3:
        _add(_query_openregister(company_name, location))

    # 4. Northdata — always run as it's the most reliable free German source
    _add(_scrape_northdata_suggest(company_name))

    return contacts


def _serp_search_german_officers(company_name: str, location: str) -> list[dict]:
    """
    Mine SERP snippets for German officer names.
    "Wenatex Geschäftsführer" → snippet usually says "Max Müller, Geschäftsführer"
    """
    contacts = []
    session = get_session()

    queries = [
        f'"{company_name}" Geschäftsführer',
        f'"{company_name}" Inhaber OR Prokurist OR Vorstand',
        f'{company_name} site:northdata.com OR site:handelsregister.de OR site:unternehmensregister.de',
    ]

    for query in queries[:2]:  # First 2 are most reliable
        html = multi_engine_search(query, session)
        if not html:
            polite_sleep(0.5)
            continue

        polite_sleep(0.5)
        soup = BeautifulSoup(html, "html.parser")

        # Collect all text snippets from search results
        snippets: list[str] = []
        for el in soup.find_all(["div", "p", "span", "li"]):
            text = el.get_text(separator=" ", strip=True)
            # Only process snippets that mention the company
            company_keyword = company_name.split()[0].lower()
            if company_keyword in text.lower() and 30 < len(text) < 500:
                snippets.append(text)

        # Also process all text for cross-snippet patterns
        full_text = soup.get_text(separator="\n")
        snippets.append(full_text)

        role_found: dict[str, str] = {}

        for snippet in snippets:
            for pattern in _SNIPPET_PATTERNS:
                for match in re.finditer(pattern, snippet, re.IGNORECASE):
                    name = match.group(1).strip() if match.lastindex >= 1 else ""
                    if not name:
                        continue
                    if _is_valid_german_name(name):
                        # Determine role from surrounding context
                        role = _role_from_context(snippet, name)
                        key = name.lower()
                        if key not in role_found:
                            role_found[key] = role or "Geschäftsführer"
                            contacts.append({
                                "full_name": name,
                                "title": role or "Geschäftsführer",
                                "email": None,
                                "phone": None,
                                "source": "german_serp",
                            })

        if contacts:
            break  # Found names, no need for more queries

    return contacts[:5]


def _is_valid_german_name(name: str) -> bool:
    """Validate that extracted text looks like a German person name."""
    parts = name.strip().split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    for part in parts:
        if not part[0].isupper():
            return False
        # Must be mostly alphabetic (allow hyphens, umlauts)
        if not re.match(r"^[A-Za-zÄÖÜäöüß\-'\.]+$", part):
            return False
        if part.lower() in _NON_NAME_WORDS:
            return False
        if len(part) < 2 or len(part) > 30:
            return False
    return True


def _role_from_context(text: str, name: str) -> str | None:
    idx = text.lower().find(name.lower())
    if idx == -1:
        context = text[:200]
    else:
        context = text[max(0, idx - 50): idx + len(name) + 80]

    for role_key, role_label in _GERMAN_OFFICER_ROLES:
        if role_key.lower() in context.lower():
            return role_label
    return None


def _query_offeneregister_datasette(company_name: str, location: str) -> list[dict]:
    """
    OffeneRegister via Datasette API (free, CC-BY).
    Datasette endpoint: https://db.offeneregister.de/
    """
    contacts = []
    try:
        # Datasette full-text search
        resp = requests.get(
            f"{OFFENEREGISTER_DATASETTE}/offeneregister/company.json",
            params={"_search": company_name, "_size": 5},
            timeout=10,
            headers={"Accept": "application/json", "User-Agent": "ContactEnrichmentBot/1.0"},
        )

        if resp.status_code == 200:
            data = resp.json()
            rows = data.get("rows", [])
            columns = data.get("columns", [])

            for row in rows[:3]:
                row_dict = dict(zip(columns, row)) if columns else row
                name_raw = row_dict.get("name", "") or row_dict.get("company_name", "")
                if not name_raw:
                    continue
                # Get officers via related endpoint
                company_id = row_dict.get("id") or row_dict.get("company_id")
                if company_id:
                    officers = _get_offeneregister_officers(company_id)
                    contacts.extend(officers)
                    if contacts:
                        break

    except Exception as e:
        logger.debug(f"OffeneRegister Datasette error: {e}")

    return contacts


def _get_offeneregister_officers(company_id: str) -> list[dict]:
    """Get officers for a specific company from OffeneRegister."""
    contacts = []
    try:
        resp = requests.get(
            f"{OFFENEREGISTER_DATASETTE}/offeneregister/officer.json",
            params={"company_id": company_id, "_size": 10},
            timeout=10,
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 200:
            data = resp.json()
            rows = data.get("rows", [])
            columns = data.get("columns", [])
            for row in rows:
                row_dict = dict(zip(columns, row)) if columns else row
                name = row_dict.get("name", "")
                position = row_dict.get("position", "Geschäftsführer")
                if name and len(name.split()) >= 2:
                    contacts.append({
                        "full_name": _fix_german_name(name),
                        "title": _translate_german_position(position),
                        "email": None,
                        "phone": None,
                        "source": "offeneregister",
                    })
    except Exception as e:
        logger.debug(f"OffeneRegister officers error: {e}")
    return contacts


def _query_offeneregister(company_name: str, location: str) -> list[dict]:
    """Legacy: kept for compatibility, now delegates to Datasette."""
    return _query_offeneregister_datasette(company_name, location)


def _query_openregister(company_name: str, location: str) -> list[dict]:
    """
    OpenRegister.de API (requires OPENREGISTER_API_KEY).
    Returns management + ownership structure for German companies.
    """
    contacts = []
    headers = {
        "Authorization": f"Bearer {OPENREGISTER_API_KEY}",
        "Accept": "application/json",
    }

    try:
        # Search for company
        resp = requests.get(
            f"{OPENREGISTER_BASE}/company",
            params={"q": company_name, "country": "de"},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return []

        results = resp.json().get("results", [])
        if not results:
            return []

        company_id = results[0].get("id")
        if not company_id:
            return []

        polite_sleep(0.5)

        # Get company details with officers
        detail_resp = requests.get(
            f"{OPENREGISTER_BASE}/company/{company_id}",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if detail_resp.status_code != 200:
            return []

        detail = detail_resp.json()
        for officer in detail.get("management", []):
            name = officer.get("name", "")
            if len(name.split()) < 2:
                continue
            role = officer.get("role", "Geschäftsführer")
            contacts.append({
                "full_name": _fix_german_name(name),
                "title": _translate_german_position(role),
                "email": None,
                "phone": None,
                "source": "openregister",
            })

    except Exception as e:
        logger.debug(f"OpenRegister error: {e}")

    return contacts


def _scrape_northdata_suggest(company_name: str) -> list[dict]:
    """
    Northdata two-step approach:
    1. Use suggest API to find the company detail URL
    2. Scrape the German detail page (northdata.de) which shows Geschäftsführer
    """
    contacts = []
    session = get_session()

    try:
        # Step 1: Suggest API — find company slug
        resp = requests.get(
            "https://www.northdata.de/_api/v1/suggest",
            params={"query": company_name, "language": "de"},
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "de-DE,de;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            },
            timeout=REQUEST_TIMEOUT,
        )

        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            company_keyword = company_name.split()[0].lower()

            # Find the matching company link
            detail_url = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                link_text = a.get_text(strip=True)
                # Must match company name AND be a registry detail URL
                if (company_keyword in link_text.lower() and
                        href.startswith("/") and
                        re.search(r"HRB|HRA|Amtsgericht|CHE-|CVR|KVK|040688|143199|207322", href)):
                    detail_url = f"https://www.northdata.de{href}"
                    break

            if detail_url:
                polite_sleep(0.8)
                contacts = _scrape_northdata_web(company_name, detail_url, session)

    except Exception as e:
        logger.debug(f"Northdata suggest error: {e}")

    # Fallback: try direct URL construction
    if not contacts:
        contacts = _scrape_northdata_web(company_name, None, session)

    return contacts


def _scrape_northdata_web(company_name: str, detail_url: str | None, session) -> list[dict]:
    """
    Scrape Northdata.de company detail page.
    The German version shows Geschäftsführer inline in the page text.
    """
    contacts = []

    if detail_url:
        html = fetch_url(detail_url, session)
    else:
        # Fallback: search page
        search_url = f"https://www.northdata.de/_api/v1/suggest?query={quote_plus(company_name)}&language=de"
        html = fetch_url(search_url, session)

    if not html:
        return []

    polite_sleep(0.8)
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")

    # Pattern: "Geschäftsführer: Michael Wernicke" — exactly how Northdata formats it
    role_patterns = [
        (r"Gesch[äa]ftsf[üu]hrer(?:in)?\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)", "Geschäftsführer"),
        (r"Prokurist(?:in)?\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)", "Prokurist"),
        (r"Vorstand\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)", "Vorstand"),
        (r"Inhaber(?:in)?\s*:?\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+(?: [A-ZÜÖÄ][a-züöäß\-]+)?)", "Inhaber"),
        (r"Aktuelle[r]? gesetzliche[r]? Vertreter\s*\n\s*Gesch[äa]ftsf[üu]hrer(?:in)?\s*\n\s*([A-ZÜÖÄ][a-züöäß\-]+ [A-ZÜÖÄ][a-züöäß\-]+)", "Geschäftsführer"),
    ]

    # Split text at "Nicht mehr" — names after this marker are former officers
    former_boundary = re.search(r"Nicht mehr\s+Gesch[äa]ftsf[üu]hrer", text, re.IGNORECASE)
    active_text = text[:former_boundary.start()] if former_boundary else text

    seen: set[str] = set()
    _UI_WORDS = {"Jetzt", "Upgraden", "Premium", "Login", "Anmelden", "Weitere", "Suche", "Mehr"}
    for pattern, role in role_patterns:
        # No IGNORECASE so [A-ZÜÖÄ] stays uppercase-only (prevents "Jetzt upgraden" false positive)
        for match in re.finditer(pattern, active_text, re.MULTILINE):
            name = match.group(1).strip()
            # Clean soft hyphens and non-breaking spaces that Northdata uses
            name = name.replace("\xad", "").replace("\xa0", " ").strip()
            parts = name.split()
            if (len(parts) >= 2 and
                    all(p[0].isupper() for p in parts) and
                    not any(p in _UI_WORDS for p in parts) and
                    name not in seen):
                seen.add(name)
                contacts.append({
                    "full_name": name,
                    "title": role,
                    "email": None,
                    "phone": None,
                    "source": "northdata_web",
                })

    return contacts[:5]


def _fix_german_name(name: str) -> str:
    """Convert 'MUELLER, Hans' → 'Hans Mueller' format."""
    if "," in name:
        parts = name.split(",", 1)
        last = parts[0].strip().title()
        first = parts[1].strip().title()
        return f"{first} {last}"
    return name.title()


def _translate_german_position(position: str) -> str:
    """Keep German titles as-is (they're useful for rating), but clean format."""
    mapping = {
        "geschaftsfuhrer": "Geschäftsführer",
        "geschaeftsfuehrer": "Geschäftsführer",
        "managing_director": "Geschäftsführer",
        "managing director": "Geschäftsführer",
        "prokurist": "Prokurist",
        "vorstand": "Vorstand",
        "inhaber": "Inhaber",
        "gesellschafter": "Gesellschafter",
        "director": "Director",
    }
    pos_lower = (position or "").lower().replace("-", "_").replace(" ", "_")
    return mapping.get(pos_lower, position.title() if position else "Geschäftsführer")
