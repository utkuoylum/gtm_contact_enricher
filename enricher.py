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

from models import Contact, PhoneDetail, EnrichmentResult
from utils.domain_finder import find_company_domain
from utils.rater import rate_contact
from scrapers.website_scraper import scrape_company_website
from scrapers.linkedin_scraper import search_linkedin_contacts
from scrapers.google_scraper import google_contact_search, scrape_crunchbase_people
from scrapers.companies_house import find_company_officers
from scrapers.news_scraper import find_executives_in_news
from scrapers.xing_scraper import find_xing_contacts
from scrapers.german_directories import find_german_directory_contacts
from scrapers.openregister import find_german_register_officers
from scrapers.press_scraper import find_press_contacts
from scrapers.job_portal_scraper import find_job_portal_contacts
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

    # 1. Find company domain (skip if caller already knows it)
    if domain:
        domain = domain.lstrip("https://").lstrip("http://").lstrip("www.").rstrip("/")
        logger.info(f"Domain provided: {domain}")
    else:
        logger.info(f"Finding domain for: {company_name}")
        try:
            domain = find_company_domain(company_name, location) or ""
        except Exception as e:
            errors.append(f"Domain lookup: {e}")
    result.domain = domain or None
    logger.info(f"Domain: {domain}")

    # 2. Run people-discovery scrapers + email-hunter + phone-hunter in parallel
    raw_contacts: list[dict] = []
    hunt_result = None
    phone_result = None

    # Detect if this is likely a German/DACH company
    is_dach = _is_dach_location(location) or _is_dach_domain(domain or "")

    people_tasks = {
        "linkedin":        lambda: search_linkedin_contacts(company_name, location, job_category),
        "google":          lambda: google_contact_search(company_name, location, domain or ""),
        "crunchbase":      lambda: scrape_crunchbase_people(company_name),
        "companies_house": lambda: find_company_officers(company_name, location),
        "news":            lambda: find_executives_in_news(company_name, location),
        "phone":           lambda: hunt_company_phone(company_name, domain or "", location),
    }

    # DACH-specific sources (highest quality for German companies)
    if is_dach:
        people_tasks["xing"]               = lambda: find_xing_contacts(company_name, location)
        people_tasks["german_register"]    = lambda: find_german_register_officers(company_name, location)
        people_tasks["german_directories"] = lambda: find_german_directory_contacts(company_name, location)
        people_tasks["press"]              = lambda: find_press_contacts(company_name, location)
        people_tasks["job_portals"]        = lambda: find_job_portal_contacts(company_name, location)

    if domain:
        people_tasks["website"] = lambda: scrape_company_website(domain)
        people_tasks["email_hunter"] = lambda: hunt_domain(domain, company_name)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fn): name for name, fn in people_tasks.items()}
        try:
            for future in as_completed(futures, timeout=150):
                name = futures[future]
                try:
                    res = future.result()
                    if name == "email_hunter":
                        hunt_result = res
                    elif name == "phone":
                        phone_result = res
                    else:
                        raw_contacts.extend(res or [])
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
                        else:
                            raw_contacts.extend(res or [])
                        sources_used.append(name)
                    except Exception as e:
                        errors.append(f"{name}: {e}")
                else:
                    errors.append(f"{name} timed out")
                    future.cancel()

    logger.info(f"People found: {len(raw_contacts)}, email_hunter: {hunt_result is not None}, phone: {phone_result is not None}")

    # 2b. Attach phone hunt results to the main result
    if phone_result and phone_result.company_main:
        pi = phone_result.company_main
        result.company_phone = pi.e164 or pi.international or pi.raw
        result.company_phone_detail = _phone_info_to_model(pi)
        errors.extend(phone_result.errors)

    # 3. Extract email intelligence from hunt_result
    pattern = hunt_result.pattern if hunt_result else None
    verified_email_map: dict[str, str] = {}  # email → smtp_status
    if hunt_result:
        for ec in hunt_result.contacts:
            verified_email_map[ec.email] = ec.smtp_status

    # 4. Deduplicate people
    deduped = _deduplicate(raw_contacts)
    logger.info(f"After dedup: {len(deduped)} people")

    # 5. Enrich each person with email (using our own hunter)
    if domain:
        _enrich_emails_with_hunter(deduped, domain, pattern, hunt_result, errors)

    # 6. Bulk-verify all newly assigned emails not yet in verified_email_map
    unverified = [c for c in deduped if c.get("email") and c.get("email") not in verified_email_map]
    if unverified:
        emails_to_verify = [c["email"] for c in unverified]
        try:
            vr_list = verify_emails_bulk(emails_to_verify)
            for vr in vr_list:
                verified_email_map[vr.email] = vr.status
        except Exception as e:
            errors.append(f"bulk_verify: {e}")

    # 7. Prepare company phone for contact attachment
    company_phone_str = result.company_phone or None
    company_phone_detail = result.company_phone_detail

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
        email = raw.get("email")
        smtp_status = verified_email_map.get(email) if email else None
        email_verified = (smtp_status in ("valid", "catch_all")) if smtp_status else None

        full_name = raw.get("full_name", "Unknown")
        direct_info = direct_line_map.get(full_name)

        c = Contact(
            full_name=full_name,
            title=raw.get("title"),
            company=company_name,
            email=email,
            email_verified=email_verified,
            # Company main number as fallback; individual phone from scraper if richer
            phone=company_phone_str,
            phone_detail=company_phone_detail,
            direct_phone=direct_info[0] if direct_info else None,
            direct_phone_detail=_phone_info_to_model(direct_info[1]) if direct_info else None,
            linkedin_url=raw.get("linkedin_url"),
            source=raw.get("source", "unknown"),
            rating=rating,
            rating_reason=reason,
        )
        contacts.append(c)

    contacts.sort(key=lambda c: (c.rating, 0 if c.email else 1, 0 if c.email_verified else 1))

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

        # Use our email hunter's per-person finder
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
