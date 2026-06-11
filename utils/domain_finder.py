from __future__ import annotations
import re
import logging
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, polite_sleep, multi_engine_search

logger = logging.getLogger(__name__)

EXCLUDED_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "wikipedia.org", "bloomberg.com", "crunchbase.com",
    "glassdoor.com", "indeed.com", "zoominfo.com", "google.com",
    "bing.com", "yahoo.com", "amazon.com", "apple.com", "microsoft.com",
    "xing.com", "kununu.com", "stepstone.de", "jobs.de", "monster.de",
}

# Legal entity suffixes to strip before slug generation
_LEGAL_SUFFIXES = re.compile(
    r"\b(gmbh|ag|kg|ohg|gbr|ug|srl|spa|sarl|bv|nv|ltd|llc|inc|corp|"
    r"co|plc|lp|llp|pte|pvt|pty|sa|as|ab|oy|sro|kft|zrt)\b",
    re.IGNORECASE,
)

# Country → preferred TLDs
_LOCATION_TLDS = {
    "germany": [".de"], "deutschland": [".de"], "hamburg": [".de"],
    "berlin": [".de"], "munich": [".de"], "münchen": [".de"],
    "frankfurt": [".de"], "cologne": [".de"], "köln": [".de"],
    "austria": [".at"], "österreich": [".at"], "vienna": [".at"], "wien": [".at"],
    "switzerland": [".ch"], "schweiz": [".ch"], "zürich": [".ch"],
    "france": [".fr"], "paris": [".fr"],
    "spain": [".es"], "madrid": [".es"], "barcelona": [".es"],
    "italy": [".it"], "milan": [".it"], "rome": [".it"],
    "netherlands": [".nl"], "amsterdam": [".nl"],
    "poland": [".pl"], "warsaw": [".pl"],
    "turkey": [".com.tr", ".tr"], "istanbul": [".com.tr"], "ankara": [".com.tr"],
    "uk": [".co.uk"], "london": [".co.uk"], "england": [".co.uk"],
    "australia": [".com.au"], "sydney": [".com.au"],
}


def _country_tlds(location: str) -> list[str]:
    loc_lower = location.lower()
    for key, tlds in _LOCATION_TLDS.items():
        if key in loc_lower:
            return tlds
    return []


def _normalize_umlauts(text: str) -> str:
    """Convert German umlauts to ASCII for domain slug generation."""
    return (text
            .replace("ä", "ae").replace("Ä", "ae")
            .replace("ö", "oe").replace("Ö", "oe")
            .replace("ü", "ue").replace("Ü", "ue")
            .replace("ß", "ss"))


def _clean_slug(company_name: str) -> str:
    """Strip legal suffixes and punctuation, return a clean slug."""
    cleaned = _LEGAL_SUFFIXES.sub("", company_name).strip(" ,.-&")
    return re.sub(r"[^a-z0-9]", "", _normalize_umlauts(cleaned).lower())


_GERMAN_LOCATION_WORDS = {
    "deutschland", "germany", "berlin", "hamburg", "münchen", "munich",
    "frankfurt", "köln", "cologne", "düsseldorf", "stuttgart", "hannover",
    "nürnberg", "nuremberg", "leipzig", "bremen", "dresden", "dortmund",
    "austria", "österreich", "wien", "vienna", "graz", "salzburg",
    "switzerland", "schweiz", "zürich", "zurich", "genf", "basel", "bern",
}


def find_company_domain(company_name: str, location: str = "") -> str | None:
    """Find the primary website domain of a company."""
    location_lower = location.lower()
    is_dach_search = any(w in location_lower for w in _GERMAN_LOCATION_WORDS)

    # Phase 0: Ask Claude from training knowledge — fastest, most reliable for known companies.
    # Claude already knows "Park Plaza Berlin" → "parkplazagermany.com" without any web search.
    # For DACH companies: only accept .de/.at/.ch domains from Claude. If Claude returns a .com
    # for a German company, proceed to SERP (avoids confusing "Koro" with US koro.com).
    _dach_tlds = (".de", ".at", ".ch")
    try:
        from utils.claude_extractor import claude_domain_from_knowledge, claude_available
        if claude_available():
            known = claude_domain_from_knowledge(company_name, location)
            if known and _domain_resolves(known):
                if is_dach_search and not any(known.endswith(t) for t in _dach_tlds):
                    logger.debug(f"Claude returned non-DACH domain {known} for DACH company — trying SERP first")
                else:
                    logger.info(f"Domain (Claude knowledge): {known}")
                    return known
    except Exception:
        pass

    # Phase 1: SERP — run all queries and collect all found domains (don't return on first hit).
    # Impressum query comes first: it targets the *local* site, not the global brand site.
    queries = [
        f'"{company_name}" {location} Impressum Kontakt' if location else f'"{company_name}" Impressum Kontakt',
        f'"{company_name}" official website',
        f'"{company_name}" {location} website' if location else f'"{company_name}" website',
    ]
    candidates: list[str] = []
    for query in queries:
        domain = _search_google_for_domain(query, company_name, location)
        if domain and domain not in candidates:
            candidates.append(domain)

    # Phase 2: TLD guessing — ONLY for slugs ≥ 5 chars.
    # Short slugs ("park", "info") match thousands of unrelated domains like park.de.
    if not candidates:
        slug_candidates = [s for s in _build_slug_candidates(company_name) if len(s) >= 5]
        country_tlds = _country_tlds(location)
        base_tlds = [".com", ".de", ".at", ".ch", ".net", ".org", ".co.uk", ".io", ".eu"]
        all_tlds = list(dict.fromkeys(country_tlds + base_tlds))
        seen: set[str] = set()
        for slug in slug_candidates:
            for tld in all_tlds:
                key = slug + tld
                if key not in seen and _domain_resolves(key):
                    candidates.append(key)
                    break
                seen.add(key)
            if candidates:
                break

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # Multiple candidates: pick the most local/relevant one.
    # For DACH companies, prefer German/local domains over global brand sites.
    if is_dach_search:
        # 1. Prefer .de / .at / .ch
        for tld in (".de", ".at", ".ch"):
            local = [d for d in candidates if d.endswith(tld)]
            if local:
                return local[0]
        # 2. Prefer domains whose name contains a DACH location word or "germany"/"austria"/"schweiz"
        boost_words = ["germany", "deutschland", "austria", "oesterreich", "schweiz", "swiss"]
        local = [d for d in candidates if any(w in d for w in boost_words)]
        if local:
            return local[0]

    # Let Claude decide among the remaining candidates
    try:
        from utils.claude_extractor import pick_best_domain, claude_available
        if claude_available():
            choice = pick_best_domain(company_name, location, candidates)
            if choice:
                return choice
    except Exception:
        pass

    return candidates[0]


def _build_slug_candidates(company_name: str) -> list[str]:
    """
    Generate ordered list of slug candidates to try.

    'Wenatex Das Schlafsystem GmbH' →
      ['wenatex', 'wenatexdasschlafsystem', 'wenatexschlafsystem']

    Rationale: the first meaningful word is almost always the brand name.
    The full slug (minus legal suffix) is a secondary candidate.
    """
    # Strip legal suffix, normalize umlauts (ä→ae, ö→oe, ü→ue, ß→ss)
    cleaned = _LEGAL_SUFFIXES.sub("", company_name).strip(" ,.-&")
    cleaned = re.sub(r"\s*[&+/]\s*", " ", cleaned).strip()  # "A & B" → "A B"
    cleaned_ascii = _normalize_umlauts(cleaned)

    candidates = []

    def _slug(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    # 1. First word only (brand name — highest priority for complex names)
    words = cleaned_ascii.split()
    if words:
        first_word_slug = _slug(words[0])
        if len(first_word_slug) >= 3:
            candidates.append(first_word_slug)

    # 2. Full cleaned slug (without legal suffix)
    full_slug = _slug(cleaned_ascii)
    if full_slug and full_slug not in candidates and len(full_slug) <= 25:
        candidates.append(full_slug)

    # 3. First two words (catches "Weber Maschinenbau" → "webermaschinenbau")
    if len(words) >= 2:
        two_word_slug = _slug(" ".join(words[:2]))
        if two_word_slug not in candidates and len(two_word_slug) <= 20:
            candidates.append(two_word_slug)

    # 4. Hyphenated first two words (catches "jaeger-lustig")
    if len(words) >= 2:
        w0, w1 = _slug(words[0]), _slug(words[1])
        if w0 and w1:
            hyphen_slug = w0 + "-" + w1
            if hyphen_slug not in candidates and len(hyphen_slug) <= 25:
                candidates.append(hyphen_slug)

    return candidates


def _search_google_for_domain(query: str, company_name: str, location: str = "") -> str | None:
    session = get_session()
    html = multi_engine_search(query, session)
    if not html:
        return None

    polite_sleep(0.5)
    soup = BeautifulSoup(html, "html.parser")
    serp_text = soup.get_text(separator=" ")

    # --- Claude-first: ask Claude to identify the domain from the SERP text ---
    try:
        from utils.claude_extractor import find_domain_from_serp, claude_available
        if claude_available():
            claude_domain = find_domain_from_serp(company_name, location, serp_text)
            if claude_domain and _domain_resolves(claude_domain):
                logger.debug(f"Claude SERP domain: {claude_domain}")
                return claude_domain
    except Exception:
        pass

    # --- Fallback: regex slug matching ---
    seen = set()
    # Use first-word slug for matching — more reliable for "Wenatex Das Schlafsystem GmbH"
    slug_candidates_list = _build_slug_candidates(company_name)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Google wraps links like /url?q=https://...
        match = re.search(r"/url\?q=(https?://[^&]+)", href)
        if match:
            href = match.group(1)
        if not href.startswith("http"):
            continue
        parsed = urlparse(href)
        domain = parsed.netloc.lstrip("www.")
        if not domain or domain in EXCLUDED_DOMAINS or domain in seen:
            continue
        seen.add(domain)

        # Match: slug candidate appears in domain's first label.
        # Use only slugs that are ≥ 5 chars to avoid "park" matching "park.de",
        # "info" matching "infosystems.de", etc. Fall back to all slugs only if
        # every candidate is short (single-word brand like "Adidas").
        domain_base = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
        specific_slugs = [s for s in slug_candidates_list if len(s) >= 5]
        match_slugs = specific_slugs if specific_slugs else slug_candidates_list
        matched = False
        for s in match_slugs:
            # s in domain_base: slug is a substring of domain (parkplaza → parkplazaberlin.com) ✓
            # domain_base in s: domain is a substring of slug — only valid if domain_base
            # is ≥5 chars, otherwise short domains like "park" falsely match "parkplaza".
            if s and (s in domain_base or (len(domain_base) >= 5 and domain_base in s)):
                matched = True
                break
        if matched:
            return domain

    # No slug match found — return None, let TLD guessing take over
    return None


def _domain_resolves(domain: str) -> bool:
    session = get_session()
    try:
        resp = session.get(f"https://{domain}", timeout=5, allow_redirects=True)
        return resp.status_code < 400
    except Exception:
        return False


def extract_email_from_text(text: str) -> list[str]:
    pattern = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
    emails = re.findall(pattern, text)
    # Filter out image/icon filenames that match the pattern
    return [e for e in emails if not any(e.endswith(ext) for ext in [".png", ".jpg", ".gif"])]


def extract_phone_from_text(text: str) -> list[str]:
    """Extract international and local phone numbers."""
    pattern = r"(?:\+?\d{1,3}[\s\-.]?)?\(?\d{2,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}"
    phones = re.findall(pattern, text)
    cleaned = []
    for p in phones:
        digits = re.sub(r"\D", "", p)
        if 7 <= len(digits) <= 15:
            cleaned.append(p.strip())
    return list(set(cleaned))
