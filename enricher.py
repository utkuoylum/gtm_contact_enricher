from __future__ import annotations
"""
Main orchestration: given company_name + location + job_category,
finds hiring decision-makers and enriches them with verified emails.

Email pipeline (no Hunter.io dependency):
  1. hunt_domain() → deep crawl + GitHub + Google + WHOIS + job boards
  2. detect_pattern() → infer {first}.{last} convention
  3. find_person_email() → generate candidates + SMTP verify
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

from models import Contact, PhoneDetail, EnrichmentResult, CompanyContactInfo
from utils.domain_finder import find_company_domain
from utils.rater import rate_contact, recency_adjustment
from scrapers.website_scraper import scrape_company_website, get_company_generic_email
from scrapers.linkedin_scraper import search_linkedin_contacts
from scrapers.google_scraper import google_contact_search, scrape_crunchbase_people
from scrapers.companies_house import find_company_officers
from scrapers.news_scraper import find_executives_in_news
from scrapers.xing_scraper import find_xing_contacts
from scrapers.german_directories import find_german_directory_contacts
from scrapers.openregister import find_german_register_officers
from scrapers.press_scraper import find_press_contacts
from scrapers.job_portal_scraper import find_job_portal_contacts
from scrapers.gemini_scraper import get_company_initial_info, gemini_available
from scrapers.bundesanzeiger_scraper import find_bundesanzeiger_contacts
from scrapers.kununu_scraper import find_kununu_contacts
from scrapers.pdl_scraper import search_pdl_contacts, pdl_available
from scrapers.icypeas_scraper import enrich_missing_emails_icypeas, icypeas_available
from scrapers.hunter_scraper import search_hunter_contacts, hunter_available
from email_hunter import hunt_domain, find_person_email
from email_hunter.smtp_verifier import verify_emails_bulk
from phone_hunter import hunt_company_phone, hunt_direct_line

logger = logging.getLogger(__name__)


def _phone_info_to_model(info) -> PhoneDetail | None:
    if not info:
        return None
    return PhoneDetail(
        raw=info.raw,
        e164=info.e164 or None,
        international=info.international or None,
        national=info.national or None,
        country_code=info.country_code or None,
        number_type=info.number_type,
        carrier_name=info.carrier_name or None,
        valid=info.valid,
        source=info.source,
        confidence=info.confidence,
    )


def enrich(company_name: str, location: str = "", job_category: str = "", max_contacts: int = 10,
           find_direct_lines: bool = False, domain: str = "") -> EnrichmentResult:
    result = EnrichmentResult(company_name=company_name)
    errors: list[str] = []
    sources_used: list[str] = []

    # 0. Gemini initial search — fast web lookup for company facts before scraping starts.
    #    Provides: industry, employee_count, website (domain hint), location hint.
    gemini_info: dict = {}
    employee_count: int | None = None
    if gemini_available():
        try:
            gemini_info = get_company_initial_info(company_name, location)
            employee_count = gemini_info.get("employee_count")
            sources_used.append("gemini_initial")
        except Exception as e:
            errors.append(f"gemini_initial: {e}")

    result.employee_count = employee_count

    # Determine contact search strategy based on employee count.
    from config import LARGE_COMPANY_THRESHOLD, STAFFING_SEARCH_TITLES, STAFFING_TITLE_KEYWORDS
    large_company = (employee_count or 0) >= LARGE_COMPANY_THRESHOLD

    # 1. Find company domain (prefer caller-supplied → Gemini website → heuristics)
    if domain:
        domain = _normalize_domain(domain)
        logger.info(f"Domain provided: {domain}")
    else:
        # 1a. Use Gemini-discovered website as authoritative domain hint
        if gemini_info.get("website"):
            candidate = _normalize_domain(gemini_info["website"])
            if candidate and _domain_plausible(candidate, company_name):
                domain = candidate
                logger.info(f"Domain (Gemini): {domain}")
            else:
                logger.info(f"Gemini website '{gemini_info['website']}' failed plausibility; falling back")

        # 1b. Heuristic domain finder as final fallback
        if not domain:
            logger.info(f"Finding domain for: {company_name}")
            try:
                domain = find_company_domain(company_name, location) or ""
            except Exception as e:
                errors.append(f"Domain lookup: {e}")
            if domain and not _domain_plausible(domain, company_name):
                errors.append(f"domain_guess_low_confidence: {domain}")
                logger.warning(f"Domain guess '{domain}' has no overlap with '{company_name}'")

    result.domain = domain or None
    logger.info(f"Domain: {domain} | employees: {employee_count} | large_company: {large_company}")

    # 2. Run people-discovery scrapers + email-hunter + phone-hunter in parallel
    raw_contacts: list[dict] = []
    hunt_result = None
    phone_result = None

    # Detect if this is likely a German/DACH company
    is_dach = _is_dach_location(location) or _is_dach_domain(domain or "") or _is_german_company(company_name)

    # Pass staffing titles to LinkedIn so it prioritises those queries first.
    # Always provide them — small companies also try staffing titles first.
    _staffing_titles = STAFFING_SEARCH_TITLES

    people_tasks = {
        "linkedin":        lambda: search_linkedin_contacts(company_name, location, job_category, _staffing_titles),
        "google":          lambda: google_contact_search(company_name, location, domain or "", job_category),
        "crunchbase":      lambda: scrape_crunchbase_people(company_name),
        "companies_house": lambda: find_company_officers(company_name, location),
        "news":            lambda: find_executives_in_news(company_name, location),
        "phone":           lambda: hunt_company_phone(company_name, domain or "", location),
    }

    # Apollo is disabled; block intentionally empty.

    if hunter_available():
        # Hunter.io: domain-search gives real emails of named employees.
        # Run with domain if known, else fall back to company name lookup.
        _h_domain = domain  # captured at task-creation time
        people_tasks["hunter"] = lambda: search_hunter_contacts(
            company_name, domain=_h_domain, location=location, job_category=job_category
        )

    # PDL: title-based API search (API key gerektirir, DACH filtreli)
    if pdl_available():
        people_tasks["pdl"] = lambda: search_pdl_contacts(
            company_name, location, job_category
        )

    # DACH-specific sources (highest quality for German companies)
    if is_dach:
        people_tasks["xing"]               = lambda: find_xing_contacts(company_name, location)
        people_tasks["german_register"]    = lambda: find_german_register_officers(company_name, location)
        people_tasks["german_directories"] = lambda: find_german_directory_contacts(company_name, location)
        people_tasks["press"]              = lambda: find_press_contacts(company_name, location)
        people_tasks["job_portals"]        = lambda: find_job_portal_contacts(company_name, location)
        people_tasks["bundesanzeiger"]     = lambda: find_bundesanzeiger_contacts(company_name, location)
        people_tasks["kununu"]             = lambda: find_kununu_contacts(company_name, location)

    if domain:
        people_tasks["website"] = lambda: scrape_company_website(domain, company_name)
        people_tasks["email_hunter"] = lambda: hunt_domain(domain, company_name)
        people_tasks["company_email"] = lambda: get_company_generic_email(domain, company_name, location)

    executor = ThreadPoolExecutor(max_workers=12)
    futures = {executor.submit(fn): name for name, fn in people_tasks.items()}
    company_generic_email: str | None = None
    try:
        for future in as_completed(futures, timeout=40):
            name = futures[future]
            try:
                res = future.result()
                if name == "email_hunter":
                    hunt_result = res
                elif name == "phone":
                    phone_result = res
                elif name == "company_email":
                    company_generic_email = res
                else:
                    found = res or []
                    logger.info(f"Source '{name}': {len(found)} contacts")
                    raw_contacts.extend(found)
                sources_used.append(name)
            except Exception as e:
                errors.append(f"{name}: {e}")
                logger.error(f"{name} error: {e}", exc_info=True)
    except FuturesTimeoutError:
        for future, name in futures.items():
            if future.done():
                try:
                    res = future.result()
                    if name == "email_hunter":
                        hunt_result = res
                    elif name == "phone":
                        phone_result = res
                    elif name == "company_email":
                        company_generic_email = res
                    else:
                        raw_contacts.extend(res or [])
                    sources_used.append(name)
                except Exception as e:
                    errors.append(f"{name}: {e}")
            else:
                errors.append(f"{name} timed out")
    finally:
        # Don't block waiting for stragglers — threads that missed the deadline
        # are abandoned. cancel_futures cancels queued-but-not-started tasks.
        executor.shutdown(wait=False, cancel_futures=True)

    logger.info(f"People found: {len(raw_contacts)}, email_hunter: {hunt_result is not None}, phone: {phone_result is not None}")

    # 2b. Build company_contact_info from phone hunter + generic email scrape
    _company_phone_str: str | None = None
    _company_phone_detail = None
    if phone_result and phone_result.company_main:
        pi = phone_result.company_main
        _company_phone_str = pi.e164 or pi.international or pi.raw
        _company_phone_detail = _phone_info_to_model(pi)
        errors.extend(phone_result.errors)

    result.company_contact_info = CompanyContactInfo(
        phone=_company_phone_str,
        phone_detail=_company_phone_detail,
        email=company_generic_email or None,
        website=f"https://{domain}" if domain else None,
    )

    # 3. Extract email intelligence from hunt_result
    pattern = hunt_result.pattern if hunt_result else None
    verified_email_map: dict[str, str] = {}  # email → smtp_status
    if hunt_result:
        for ec in hunt_result.contacts:
            verified_email_map[ec.email] = ec.smtp_status

    # 4. Filter out non-persons (company names returned as contacts) then deduplicate
    logger.debug(f"Raw contacts before filter: {[(c.get('full_name'), c.get('source')) for c in raw_contacts]}")
    raw_contacts = [c for c in raw_contacts if _is_valid_person(c.get("full_name", ""), company_name)]
    deduped = _deduplicate(raw_contacts)
    logger.info(f"After dedup: {len(deduped)} people")

    # 4b+4c. Single Claude call: remove false positives + score confidence
    if deduped:
        try:
            from utils.claude_extractor import clean_and_score_contacts, claude_available
            if claude_available():
                slim = [
                    {
                        "full_name": c.get("full_name", ""),
                        "title": c.get("title"),
                        "email": c.get("email"),
                        "source": c.get("source", ""),
                        "year_found": c.get("year_found"),
                        "has_email": bool(c.get("email")),
                        "has_linkedin_url": bool(c.get("linkedin_url")),
                    }
                    for c in deduped
                ]
                cleaned, scored = clean_and_score_contacts(slim, company_name, location, job_category)
                if cleaned:
                    surviving_names = {c.get("full_name", "").lower() for c in cleaned}
                    score_map = {c.get("full_name", "").lower(): c for c in cleaned}
                    deduped = [
                        {
                            **c,
                            "title": score_map.get(c.get("full_name", "").lower(), {}).get("title", c.get("title")),
                            "confidence": score_map.get(c.get("full_name", "").lower(), {}).get("confidence", 0),
                            "employment_confirmed": score_map.get(c.get("full_name", "").lower(), {}).get("employment_confirmed", False),
                        }
                        for c in deduped
                        if c.get("full_name", "").lower() in surviving_names
                    ]
                    deduped.sort(key=lambda c: -c.get("confidence", 0))
        except Exception as e:
            errors.append(f"claude_clean_score: {e}")

    # 4d. Apply staffing-relevance filter based on company size.
    # Large companies (≥ threshold): keep ONLY event/HR/people/recruit/ops contacts.
    # Small companies: prefer those contacts; fall back to all if fewer than 3 found.
    deduped = _apply_staffing_filter(deduped, large_company, STAFFING_TITLE_KEYWORDS)

    # 5. Enrich each person with email (using our own hunter)
    if domain:
        _enrich_emails_with_hunter(deduped, domain, pattern, hunt_result, errors)

    # 5b. Icypeas email enrichment — Apollo people/match'in yerini tutar.
    if domain and icypeas_available():
        from config import ICYPEAS_ENRICH_TOP_N
        enrich_missing_emails_icypeas(deduped, domain, verified_email_map, errors, ICYPEAS_ENRICH_TOP_N)

    # 6. Bulk-verify newly assigned emails — skip emails from trusted sources
    _TRUSTED_SOURCES = {"linkedin", "xing", "german_register", "northdata", "hunter"}
    for c in deduped:
        src = c.get("source", "")
        if c.get("email") and src in _TRUSTED_SOURCES:
            verified_email_map[c["email"]] = "valid"

    unverified = [c for c in deduped if c.get("email") and c.get("email") not in verified_email_map]
    if unverified:
        emails_to_verify = [c["email"] for c in unverified]
        try:
            vr_list = verify_emails_bulk(emails_to_verify)
            for vr in vr_list:
                verified_email_map[vr.email] = vr.status
        except Exception as e:
            errors.append(f"bulk_verify: {e}")

    # 7b. Optional: hunt direct lines for top-rated contacts
    region = _infer_region(domain or "", location)
    direct_line_map: dict[str, tuple] = {}  # full_name → (phone_str, PhoneInfo)
    if find_direct_lines and domain:
        for raw in deduped[:5]:  # Only top 5 to save time
            name = raw.get("full_name", "")
            if not name:
                continue
            try:
                direct_infos = hunt_direct_line(name, company_name, domain,
                                                raw.get("title", ""), location, region)
                if direct_infos:
                    best = direct_infos[0]
                    direct_line_map[name] = (best.e164 or best.international or best.raw, best)
            except Exception as e:
                errors.append(f"direct_line({name}): {e}")

    # 8. Rate contacts and build final output
    contacts = []
    for raw in deduped:
        rating, reason = rate_contact(raw.get("title"), job_category)
        rec_adj, rec_note = recency_adjustment(raw.get("source", ""), raw.get("year_found"))

        email = raw.get("email")
        smtp_status = verified_email_map.get(email) if email else None
        email_verified = (smtp_status in ("valid", "catch_all")) if smtp_status else None

        full_name = raw.get("full_name", "Unknown")
        direct_info = direct_line_map.get(full_name)

        # Individual phone: only if it's a confirmed direct line from hunt_direct_line.
        # Company main number is already in result.company_phone — attaching it to every
        # contact implies a direct line when it's just the switchboard.
        direct_phone_str = direct_info[0] if direct_info else None
        direct_phone_detail = _phone_info_to_model(direct_info[1]) if direct_info else None

        c = Contact(
            full_name=full_name,
            title=raw.get("title"),
            company=company_name,
            email=email,
            email_verified=email_verified,
            phone=direct_phone_str,
            phone_detail=direct_phone_detail,
            direct_phone=direct_phone_str,
            direct_phone_detail=direct_phone_detail,
            linkedin_url=raw.get("linkedin_url"),
            source=raw.get("source", "unknown"),
            rating=rating,
            rating_reason=reason,
            confidence=raw.get("confidence", 0),
            employment_confirmed=raw.get("employment_confirmed", False),
            data_year=raw.get("year_found"),
            recency_note=rec_note,
        )
        # Attach recency score as a private sort key (not in model)
        c._recency_adj = rec_adj  # type: ignore[attr-defined]
        contacts.append(c)

    # Sort: primary = confidence (Claude-assessed), secondary = title authority + recency, tertiary = has email
    contacts.sort(key=lambda c: (
        -(c.confidence),
        c.rating + getattr(c, "_recency_adj", 0.0),
        0 if c.email else 1,
        0 if c.email_verified else 1,
    ))

    result.contacts = contacts[:max_contacts]
    result.total_found = len(contacts)
    result.sources_used = list(set(sources_used))
    result.errors = errors
    return result


_DACH_CITIES = {
    "hamburg", "berlin", "münchen", "munich", "frankfurt", "köln", "cologne",
    "düsseldorf", "dortmund", "stuttgart", "hannover", "nuremberg", "nürnberg",
    "leipzig", "bremen", "dresden", "wien", "vienna", "zürich", "zurich",
    "genf", "geneva", "basel", "graz", "salzburg", "innsbruck", "linz",
    "germany", "deutschland", "austria", "österreich", "switzerland", "schweiz",
    "dach", "de", "at", "ch",
}


def _is_dach_location(location: str) -> bool:
    loc_lower = location.lower()
    return any(city in loc_lower for city in _DACH_CITIES)


def _is_dach_domain(domain: str) -> bool:
    if not domain:
        return False
    tld = domain.lower().split(".")[-1]
    return tld in ("de", "at", "ch")


# German legal entity suffixes that indicate a DACH-registered company,
# regardless of location string or domain TLD (e.g. wenatex.com).
_GERMAN_LEGAL_FORMS = re.compile(
    r"\b(GmbH|AG|KG|UG|e\.K\.|eK|SE|eG|OHG|GbR|GmbH\s*&\s*Co\.?\s*KG)\b",
    re.IGNORECASE,
)


def _is_german_company(company_name: str) -> bool:
    """Return True if the company name contains a German legal form."""
    return bool(_GERMAN_LEGAL_FORMS.search(company_name))


def _normalize_domain(domain: str) -> str:
    """'https://www.example.com/' → 'example.com' (proper prefix removal, not lstrip)."""
    d = domain.strip()
    d = re.sub(r"^https?://", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^www\.", "", d, flags=re.IGNORECASE)
    return d.split("/")[0].strip()


def _domain_plausible(domain: str, company_name: str) -> bool:
    """
    True if the domain's first label shares a ≥4-char fragment with the company
    name slug. Catches junk guesses ('Jäger & Lustig' → jaeger.de is fine,
    'i solutions and more' → isolutions.ch is NOT unless fragment matches).
    """
    base = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    slug = re.sub(r"[^a-z0-9]", "", _GERMAN_LEGAL_FORMS.sub("", company_name).lower()
                  .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss"))
    if not base or not slug:
        return False
    if base in slug or slug in base:
        return True
    # Longest common substring ≥ 4 chars
    for length in range(min(len(base), len(slug)), 3, -1):
        for start in range(len(base) - length + 1):
            if base[start:start + length] in slug:
                return True
    return False



def _apply_staffing_filter(
    contacts: list[dict],
    large_company: bool,
    keywords: list[str],
) -> list[dict]:
    """
    Filter contacts by staffing-relevance based on company size.

    Large company (≥ threshold): return ONLY keyword-matching contacts.
    Small company: return keyword-matching contacts if ≥ 3 found; otherwise all contacts
    (preserves the existing 5-contact fallback for micro/small firms with no HR dept).
    """
    matching = [
        c for c in contacts
        if _title_matches_staffing(c.get("title") or "", keywords)
    ]

    if large_company:
        logger.info(
            f"Large company mode: keeping {len(matching)}/{len(contacts)} staffing-relevant contacts"
        )
        return matching

    # Small company: prefer staffing contacts if we have enough
    if len(matching) >= 3:
        logger.info(
            f"Small company staffing filter: {len(matching)}/{len(contacts)} contacts matched, using staffing subset"
        )
        return matching

    logger.info(
        f"Small company fallback: only {len(matching)} staffing contacts found, keeping all {len(contacts)}"
    )
    return contacts


def _title_matches_staffing(title: str, keywords: list[str]) -> bool:
    """Return True if the title contains any staffing-relevant keyword (case-insensitive)."""
    title_lower = title.lower()
    return any(kw in title_lower for kw in keywords)


def _infer_region(domain: str, location: str) -> str:
    from phone_hunter.validator import region_from_domain, region_from_location
    if location:
        r = region_from_location(location)
        if r != "US":
            return r
    if domain:
        return region_from_domain(domain)
    return "US"


def _enrich_emails_with_hunter(
    contacts: list[dict],
    domain: str,
    pattern,
    hunt_result,
    errors: list[str],
):
    """
    For each contact without email:
    1. Check if hunt_result already has a verified email for matching name
    2. Use find_person_email() (pattern + SMTP) as fallback
    """
    # Build a name→email map from hunt_result contacts
    hunt_email_by_local: dict[str, str] = {}
    if hunt_result:
        for ec in hunt_result.contacts:
            if ec.smtp_status in ("valid", "catch_all"):
                hunt_email_by_local[ec.local_part.lower()] = ec.email

    for contact in contacts:
        if contact.get("email"):
            continue
        full_name = contact.get("full_name", "")
        parts = full_name.split()
        if len(parts) < 2:
            continue
        first, last = parts[0], parts[-1]

        # Check hunt_result for a matching name
        from email_hunter.pattern_detector import normalize
        fi = normalize(first)
        la = normalize(last)
        for local, email in hunt_email_by_local.items():
            if fi in local and la in local:
                contact["email"] = email
                break
        if contact.get("email"):
            continue

        # Use our email hunter's per-person finder — ONLY when we have a confirmed
        # email pattern. Without one, find_person_email crawls the entire site to
        # discover the pattern, taking 2+ minutes per contact (× 15 contacts = hang).
        if not pattern:
            continue
        try:
            found = find_person_email(first, last, domain, pattern=pattern, max_verify=5)
            if found and found.get("email") and found.get("confidence", 0) >= 30:
                contact["email"] = found["email"]
        except Exception as e:
            errors.append(f"find_person_email({full_name}): {e}")


def _deduplicate(raw: list[dict]) -> list[dict]:
    seen_emails: dict[str, dict] = {}
    seen_names: dict[str, dict] = {}
    result = []

    for item in raw:
        name = _normalize_name(item.get("full_name", ""))
        email = (item.get("email") or "").lower().strip()

        if email and email in seen_emails:
            _merge_into(seen_emails[email], item)
            continue
        if name and name in seen_names:
            existing = seen_names[name]
            _merge_into(existing, item)
            if email:
                seen_emails[email] = existing
            continue

        entry = dict(item)
        result.append(entry)
        if email:
            seen_emails[email] = entry
        if name:
            seen_names[name] = entry

    return result


def _merge_into(existing: dict, new: dict):
    for key in ["email", "phone", "title", "linkedin_url"]:
        if not existing.get(key) and new.get(key):
            existing[key] = new[key]
    priority = {
        "website_schema": 0, "linkedin_google": 1, "website_card": 2,
        "presseportal_contact": 3, "website_email": 4, "crunchbase": 5,
        "northdata_web": 6, "northdata": 6, "impressum": 7,
        "press_release": 8, "google_serp": 9, "job_portal": 10,
        "website_text": 11, "press_serp": 12,
    }
    if priority.get(new.get("source", ""), 99) < priority.get(existing.get("source", ""), 99):
        existing["source"] = new["source"]


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())


_COMPANY_NAME_NOISE = re.compile(
    r"\b(GmbH|AG|KG|UG|e\.K\.|eK|SE|eG|OHG|GbR|Ltd\.?|LLC|Inc\.?|Corp\.?|"
    r"GmbH\s*&\s*Co\.?\s*KG|Kontakt|Contact|Info|Support|Service|"
    r"Hotel|Hotels|Gruppe|Group|Holding|Management|Solutions|Systems|Technologies)\b",
    re.IGNORECASE,
)

# Lowercase particles that appear in legitimate personal names
_NAME_PARTICLES = {"von", "van", "de", "der", "den", "di", "du", "del", "da", "le", "la", "zu"}

# Words that cannot be any part of a real person's name.
# Catches navigation menu items, booking UI text, loyalty program names etc.
# that the website scraper's regex sometimes extracts as false "names".
_NOT_A_NAME_WORD = frozenset({
    # Pronouns / possessives
    "my", "your", "our", "their", "his", "her", "its", "you",
    # Prepositions / articles that never appear as a name component
    "for", "to", "by", "at", "in", "on", "of", "the", "an",
    # Website/UI states
    "access", "restricted", "new", "all", "best", "top",
    # Account / navigation
    "account", "profile", "settings", "login", "logout", "register", "password",
    "search", "navigation",
    # Loyalty program / booking
    "rewards", "points", "transfer", "redeem", "earn", "bonus",
    "corporate", "program", "programme", "offer", "deal",
    # Travel / hotel
    "agent", "arranger", "arrenger", "booking", "reservation", "reservations",
    # Major hotel chain brand names that cannot be a person's first name
    "radisson", "marriott", "hilton", "hyatt", "sheraton", "westin", "ibis",
    # HTML/meta junk that leaks into scraped text
    "description", "keywords",
    # Impressum / legal page artifacts
    "impressum", "imprint", "datenschutz", "kontakt", "contact", "info", "about",
    "legal", "privacy", "disclaimer", "copyright",
    # Country/geography names that appear as scraping artifacts (e.g. "Germany Impressum")
    "germany", "deutschland", "austria", "österreich", "switzerland", "schweiz",
    "china", "japan", "france", "italy", "spain", "sweden", "netherlands",
    "australia", "india", "korea", "taiwan", "hongkong", "poland",
    # Generic role/entity words that are NOT person names
    "foundation", "investment", "investor", "partner", "partners",
    "holding", "venture", "capital", "fund", "group", "management",
    "consulting", "technology", "technologies", "international", "global",
    "enterprise", "limited", "gmbh", "asia", "europe", "pacific",
    # News headline / press release fragments that leak into scraped names
    "strategic", "signs", "veteran", "agreement", "announces", "launches",
    "expands", "acquires", "appoints", "names", "hires", "joins", "leaves",
    # Standalone prepositions/conjunctions that never appear as a name word
    "as", "and", "or", "with", "for", "to", "by", "at", "in", "on", "of",
})


def _is_valid_person(name: str, company_name: str) -> bool:
    """Return False if the name looks like a company name rather than a real person."""
    if not name or len(name) < 4:
        return False
    # Reject if name contains company legal forms or generic labels
    if _COMPANY_NAME_NOISE.search(name):
        return False
    # Reject if name is identical (or near-identical) to company name
    if name.lower().strip() == company_name.lower().strip():
        return False
    # Reject if name contains a large portion of the company name words
    company_words = {w.lower() for w in company_name.split() if len(w) > 3}
    name_words = {w.lower() for w in name.split() if len(w) > 3}
    if company_words and len(name_words & company_words) >= min(2, len(company_words)):
        return False
    # Must look like a personal name: 2-5 words
    parts = [p for p in name.strip().split() if p]
    if len(parts) < 2 or len(parts) > 5:
        return False
    # Reject if any word is an obvious non-name word (nav items, UI text, brand names)
    parts_lower = {p.lower() for p in parts}
    if parts_lower & _NOT_A_NAME_WORD:
        logger.debug(f"_is_valid_person rejected '{name}': contains non-name word")
        return False
    # Each word must start uppercase, EXCEPT known name particles (von, van, de, etc.)
    for p in parts:
        if p.lower() in _NAME_PARTICLES:
            continue
        if not p[0].isupper():
            logger.debug(f"_is_valid_person rejected '{name}': part '{p}' not capitalized")
            return False
    return True
