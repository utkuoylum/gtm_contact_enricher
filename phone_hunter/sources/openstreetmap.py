from __future__ import annotations
"""
OpenStreetMap Nominatim API — free, no API key required.
Rate limit: 1 req/second (enforced by polite_sleep).

OSM has surprisingly good coverage of businesses, especially in Europe.
The `extratags=1` parameter returns phone, website, opening_hours etc.

Also queries Wikidata for major companies (often have phone numbers).
"""
import re
import logging
import requests
from utils.http_client import polite_sleep, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

NOMINATIM_BASE = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "ContactEnrichmentBot/1.0 (contact enrichment tool)"}


def find_phone_osm(company_name: str, location: str = "") -> list[dict]:
    """
    Returns list of {number, source, confidence, business_name, address}
    """
    results = []

    # Build query
    query = company_name
    if location:
        query = f"{company_name}, {location}"

    # OSM Nominatim search
    try:
        params = {
            "q": query,
            "format": "jsonv2",
            "limit": 5,
            "extratags": 1,
            "addressdetails": 1,
        }
        resp = requests.get(NOMINATIM_BASE, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        polite_sleep(1.1)  # OSM requires 1s between requests

        if resp.status_code == 200:
            places = resp.json()
            for place in places[:3]:
                extratags = place.get("extratags", {}) or {}
                phone = extratags.get("phone") or extratags.get("contact:phone")
                if phone:
                    results.append({
                        "number": phone,
                        "business_name": place.get("display_name", "").split(",")[0],
                        "address": place.get("display_name", ""),
                        "source": "openstreetmap",
                        "confidence": 82,
                    })
                    break
    except Exception as e:
        logger.debug(f"OSM Nominatim error: {e}")

    # Wikidata SPARQL — for major companies
    if not results:
        wikidata_phones = _query_wikidata(company_name)
        results.extend(wikidata_phones)

    return results


def _query_wikidata(company_name: str) -> list[dict]:
    """
    Query Wikidata for company phone (P1329 = phone number).
    Works for well-known companies that have Wikidata entries.
    """
    # Escape for SPARQL
    safe_name = company_name.replace('"', '\\"')
    sparql = f"""
SELECT ?phone ?companyLabel WHERE {{
  ?company wdt:P31 wd:Q4830453 .
  ?company rdfs:label "{safe_name}"@en .
  ?company wdt:P1329 ?phone .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
LIMIT 3
"""
    try:
        resp = requests.get(
            "https://query.wikidata.org/sparql",
            params={"query": sparql, "format": "json"},
            headers={**HEADERS, "Accept": "application/sparql-results+json"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            bindings = data.get("results", {}).get("bindings", [])
            results = []
            for b in bindings:
                phone = b.get("phone", {}).get("value", "")
                if phone:
                    results.append({
                        "number": phone,
                        "business_name": company_name,
                        "source": "wikidata",
                        "confidence": 78,
                    })
            return results
    except Exception as e:
        logger.debug(f"Wikidata query error: {e}")

    return []
