from __future__ import annotations
"""
Phone number validation and normalization using Google's libphonenumber (phonenumbers lib).
- Parse raw strings into structured phone objects
- Format to E.164, international, national
- Detect type (mobile, fixed_line, VOIP, toll_free)
- Detect carrier (offline — based on number range, not real-time)
- Detect country
"""
import re
import logging
from dataclasses import dataclass, field

import phonenumbers
from phonenumbers import (
    PhoneNumberType,
    geocoder,
    carrier,
    timezone,
    NumberParseException,
    is_valid_number,
    is_possible_number,
    format_number,
    PhoneNumberFormat,
    number_type,
    parse as pn_parse,
)

logger = logging.getLogger(__name__)

# Map phonenumbers type enum → readable string
TYPE_LABELS = {
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE: "fixed_line",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_or_mobile",
    PhoneNumberType.TOLL_FREE: "toll_free",
    PhoneNumberType.PREMIUM_RATE: "premium_rate",
    PhoneNumberType.SHARED_COST: "shared_cost",
    PhoneNumberType.VOIP: "voip",
    PhoneNumberType.PERSONAL_NUMBER: "personal",
    PhoneNumberType.PAGER: "pager",
    PhoneNumberType.UAN: "uan",
    PhoneNumberType.UNKNOWN: "unknown",
    PhoneNumberType.VOICEMAIL: "voicemail",
}

# Rough mapping of TLD/country-code → ISO country
TLD_TO_REGION = {
    "co.uk": "GB", "uk": "GB", "ie": "IE", "de": "DE", "fr": "FR",
    "es": "ES", "it": "IT", "nl": "NL", "be": "BE", "ch": "CH",
    "at": "AT", "se": "SE", "no": "NO", "dk": "DK", "fi": "FI",
    "pl": "PL", "pt": "PT", "tr": "TR", "au": "AU", "nz": "NZ",
    "ca": "CA", "in": "IN", "sg": "SG", "hk": "HK", "ae": "AE",
    "sa": "SA", "za": "ZA", "br": "BR", "mx": "MX", "ar": "AR",
    "jp": "JP", "kr": "KR", "cn": "CN", "com": "US",
}


@dataclass
class PhoneInfo:
    raw: str
    e164: str = ""
    international: str = ""
    national: str = ""
    country_code: str = ""          # ISO alpha-2, e.g. "GB"
    dial_code: int = 0              # e.g. 44
    region_description: str = ""    # e.g. "United Kingdom"
    number_type: str = "unknown"
    carrier_name: str = ""
    timezones: list[str] = field(default_factory=list)
    valid: bool = False
    source: str = ""
    confidence: int = 0             # 0–100


def parse_phone(raw: str, default_region: str = "US") -> PhoneInfo | None:
    """
    Parse a raw phone string into a PhoneInfo object.
    Returns None if the string cannot be parsed as a valid phone number.
    """
    if not raw or len(re.sub(r"\D", "", raw)) < 7:
        return None

    info = PhoneInfo(raw=raw)

    # Try with the given region, then without (international format)
    parsed = None
    for region in [default_region, None]:
        try:
            parsed = pn_parse(raw, region)
            if is_valid_number(parsed):
                break
            elif is_possible_number(parsed):
                break
        except NumberParseException:
            continue

    if not parsed:
        return None

    info.valid = is_valid_number(parsed)
    if not info.valid and not is_possible_number(parsed):
        return None

    try:
        info.e164 = format_number(parsed, PhoneNumberFormat.E164)
        info.international = format_number(parsed, PhoneNumberFormat.INTERNATIONAL)
        info.national = format_number(parsed, PhoneNumberFormat.NATIONAL)
        info.dial_code = parsed.country_code
        info.country_code = phonenumbers.region_code_for_number(parsed) or ""
        info.region_description = geocoder.description_for_number(parsed, "en") or ""
        info.number_type = TYPE_LABELS.get(number_type(parsed), "unknown")
        info.carrier_name = carrier.name_for_number(parsed, "en") or ""
        tz = timezone.time_zones_for_number(parsed)
        info.timezones = list(tz)
    except Exception as e:
        logger.debug(f"phonenumbers enrichment error for '{raw}': {e}")

    return info


def dedupe_phones(phones: list[PhoneInfo]) -> list[PhoneInfo]:
    """Deduplicate by E.164 form, keeping highest-confidence entry."""
    seen: dict[str, PhoneInfo] = {}
    for p in phones:
        key = p.e164 or p.raw
        if key not in seen or p.confidence > seen[key].confidence:
            seen[key] = p
    # Sort: valid first, then by confidence desc
    return sorted(seen.values(), key=lambda p: (0 if p.valid else 1, -p.confidence))


def region_from_domain(domain: str) -> str:
    """Guess the default phone region from a company's domain TLD."""
    parts = domain.lower().split(".")
    if len(parts) >= 2:
        tld = ".".join(parts[-2:])
        if tld in TLD_TO_REGION:
            return TLD_TO_REGION[tld]
        tld1 = parts[-1]
        if tld1 in TLD_TO_REGION:
            return TLD_TO_REGION[tld1]
    return "US"


def region_from_location(location: str) -> str:
    """Guess region from a location string like 'London, UK' or 'Berlin'."""
    loc = location.lower()
    country_hints = {
        "uk": "GB", "united kingdom": "GB", "england": "GB", "london": "GB",
        "germany": "DE", "berlin": "DE", "munich": "DE", "frankfurt": "DE",
        "france": "FR", "paris": "FR", "lyon": "FR",
        "spain": "ES", "madrid": "ES", "barcelona": "ES",
        "italy": "IT", "rome": "IT", "milan": "IT",
        "netherlands": "NL", "amsterdam": "NL",
        "australia": "AU", "sydney": "AU", "melbourne": "AU",
        "canada": "CA", "toronto": "CA", "vancouver": "CA",
        "india": "IN", "mumbai": "IN", "bangalore": "IN", "delhi": "IN",
        "singapore": "SG", "hong kong": "HK",
        "uae": "AE", "dubai": "AE", "abu dhabi": "AE",
        "turkey": "TR", "istanbul": "TR", "ankara": "TR",
        "usa": "US", "united states": "US", "new york": "US", "san francisco": "US",
    }
    for hint, region in country_hints.items():
        if hint in loc:
            return region
    return "US"
