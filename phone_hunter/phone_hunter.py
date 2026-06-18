from __future__ import annotations
"""
Phone Hunter orchestrator — builds phone lookup from scratch.

Two stages:
  A) Company main number (high success rate)
     1. Company website schema.org / microdata
     2. Google/Bing SERP knowledge panel
     3. Yelp Fusion API
     4. Yellow Pages / Yell.com
     (All run in parallel; stops at first reliable result)

  B) Individual direct lines (low success rate but worth trying)
     — Triggered per person, runs separately from the main number lookup
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutTO
from dataclasses import dataclass, field

from phone_hunter.sources.website_microdata import find_phones_on_website
from phone_hunter.sources.serp_phone import find_phones_via_serp
from phone_hunter.sources.yelp import find_business_phone
from phone_hunter.sources.yellow_pages import find_phone_in_directories
from phone_hunter.sources.google_maps import find_phone_google_maps
from phone_hunter.sources.openstreetmap import find_phone_osm
from phone_hunter.sources.direct_line_hunter import find_direct_lines
from phone_hunter.sources.abstractapi import enrich_phone
from phone_hunter.validator import (
    parse_phone, dedupe_phones, PhoneInfo,
    region_from_domain, region_from_location,
)

logger = logging.getLogger(__name__)


@dataclass
class PhoneHuntResult:
    company_main: PhoneInfo | None = None           # Best company number
    company_alternatives: list[PhoneInfo] = field(default_factory=list)
    direct_lines: list[PhoneInfo] = field(default_factory=list)   # Per-person
    sources_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def hunt_company_phone(
    company_name: str,
    domain: str = "",
    location: str = "",
) -> PhoneHuntResult:
    """
    Find and validate the company's main phone number(s).
    Runs 4 sources in parallel, deduplicates, validates, returns best result.
    """
    result = PhoneHuntResult()
    region = _infer_region(domain, location)
    raw_phones: list[dict] = []
    errors: list[str] = []
    sources_used: list[str] = []

    tasks = {}
    if domain:
        tasks["website"] = lambda: find_phones_on_website(domain)
    tasks["serp"]      = lambda: find_phones_via_serp(company_name, location, domain)
    tasks["yelp"]      = lambda: find_business_phone(company_name, location)
    tasks["directory"] = lambda: find_phone_in_directories(company_name, location, region)
    tasks["google_maps"] = lambda: find_phone_google_maps(company_name, location)
    tasks["osm"]       = lambda: find_phone_osm(company_name, location)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fn): name for name, fn in tasks.items()}
        try:
            for future in as_completed(futures, timeout=60):
                name = futures[future]
                try:
                    phones = future.result() or []
                    raw_phones.extend(phones)
                    sources_used.append(name)
                except Exception as e:
                    errors.append(f"{name}: {e}")
                    logger.debug(f"phone {name} error: {e}")
        except FutTO:
            for future, name in futures.items():
                if future.done():
                    try:
                        raw_phones.extend(future.result() or [])
                        sources_used.append(name)
                    except Exception as e:
                        errors.append(f"{name}: {e}")
                else:
                    future.cancel()
                    errors.append(f"{name}: timed out")

    # Parse + validate all found numbers
    parsed: list[PhoneInfo] = []
    for raw in raw_phones:
        number_str = raw.get("number", "")
        source = raw.get("source", "unknown")
        confidence = raw.get("confidence", 50)

        info = parse_phone(number_str, default_region=region)
        if info:
            info.source = source
            info.confidence = confidence
            parsed.append(info)

    # Deduplicate by E.164
    deduped = dedupe_phones(parsed)

    # Optionally enrich top number with AbstractAPI (carrier, line type)
    for info in deduped[:2]:
        if info.e164:
            enriched = enrich_phone(info.e164)
            if enriched:
                info.carrier_name = enriched.get("carrier", info.carrier_name)
                info.number_type = enriched.get("line_type", info.number_type)
                # AbstractAPI says valid=True → boost confidence
                if enriched.get("valid"):
                    info.confidence = min(info.confidence + 10, 100)

    if deduped:
        result.company_main = deduped[0]
        result.company_alternatives = deduped[1:]

    result.sources_used = list(set(sources_used))
    result.errors = errors
    return result


def hunt_direct_line(
    full_name: str,
    company_name: str,
    domain: str = "",
    title: str = "",
    location: str = "",
    region: str = "US",
) -> list[PhoneInfo]:
    """
    Try to find a direct phone number for a specific person.
    Returns empty list if nothing found (common — direct lines are rarely public).
    """
    raw_results = find_direct_lines(full_name, company_name, domain, title, location)
    parsed = []
    for raw in raw_results:
        info = parse_phone(raw.get("number", ""), default_region=region)
        if info:
            info.source = raw.get("source", "unknown")
            info.confidence = raw.get("confidence", 40)
            parsed.append(info)
    return dedupe_phones(parsed)


def _infer_region(domain: str, location: str) -> str:
    if location:
        r = region_from_location(location)
        if r != "US":
            return r
    if domain:
        return region_from_domain(domain)
    return "US"
