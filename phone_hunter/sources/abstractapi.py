from __future__ import annotations
"""
AbstractAPI Phone Validation (optional enhancement).
Free: 100 calls/month — returns carrier type, line type, country, validity.
Get key: https://app.abstractapi.com/api/phone-validation

This is NOT for finding phones — it's for ENRICHING phones we already found
with carrier name, line type (mobile vs landline), and real-time validity.
"""
import os
import logging
import requests
from utils.http_client import REQUEST_TIMEOUT

logger = logging.getLogger(__name__)
ABSTRACT_API_KEY = os.getenv("ABSTRACT_PHONE_API_KEY", "")
BASE = "https://phonevalidation.abstractapi.com/v1/"


def enrich_phone(number: str) -> dict | None:
    """
    Enrich a phone number with carrier, type, location info.
    Returns dict or None if API not configured / call fails.

    Response shape:
    {
      "phone": "+14155552671",
      "valid": true,
      "format": {"international": "+1 415-555-2671", "local": "(415) 555-2671"},
      "country": {"code": "US", "name": "United States", "prefix": "+1"},
      "location": "California",
      "type": "mobile",
      "carrier": "T-Mobile"
    }
    """
    if not ABSTRACT_API_KEY:
        return None

    try:
        resp = requests.get(
            BASE,
            params={"api_key": ABSTRACT_API_KEY, "phone": number},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "valid": data.get("valid", False),
                "line_type": data.get("type", "unknown"),      # mobile, landline, voip, etc.
                "carrier": data.get("carrier", ""),
                "country": data.get("country", {}).get("name", ""),
                "country_code": data.get("country", {}).get("code", ""),
                "location": data.get("location", ""),
                "international_format": data.get("format", {}).get("international", ""),
                "local_format": data.get("format", {}).get("local", ""),
            }
        if resp.status_code == 429:
            logger.warning("AbstractAPI phone validation rate limit hit (100/month free tier)")
        elif resp.status_code == 401:
            logger.warning("AbstractAPI key invalid")
    except requests.RequestException as e:
        logger.debug(f"AbstractAPI error: {e}")

    return None
