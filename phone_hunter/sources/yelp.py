from __future__ import annotations
"""
Yelp Fusion API phone discovery.
Free tier: 500 requests/day — no credit card needed.
Get key: https://www.yelp.com/developers/v3/manage_app

Endpoints used:
  /v3/businesses/search → find business by name + location → get phone
  /v3/businesses/{id}   → detailed business info including display_phone
"""
import os
import logging
import requests
from utils.http_client import REQUEST_TIMEOUT

logger = logging.getLogger(__name__)
YELP_API_KEY = os.getenv("YELP_API_KEY", "")
BASE = "https://api.yelp.com/v3"


def _headers() -> dict:
    return {"Authorization": f"Bearer {YELP_API_KEY}"}


def find_business_phone(company_name: str, location: str = "") -> list[dict]:
    """
    Returns list of {number, display_phone, business_name, source, confidence}
    """
    if not YELP_API_KEY:
        logger.debug("YELP_API_KEY not set — skipping Yelp")
        return []

    results = []

    # Search for business
    params = {
        "term": company_name,
        "limit": 5,
    }
    if location:
        params["location"] = location
    else:
        params["location"] = "United States"  # Yelp requires location

    try:
        resp = requests.get(f"{BASE}/businesses/search", headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 401:
            logger.warning("Yelp API key invalid or expired")
            return []
        if resp.status_code == 429:
            logger.warning("Yelp API rate limit hit")
            return []
        if resp.status_code != 200:
            logger.debug(f"Yelp search returned {resp.status_code}")
            return []

        data = resp.json()
        businesses = data.get("businesses", [])

        for biz in businesses[:3]:
            biz_name = biz.get("name", "")
            phone = biz.get("phone", "")
            display_phone = biz.get("display_phone", "")

            # Skip if name is too different (avoid false matches)
            if not _names_overlap(company_name, biz_name):
                continue

            if phone or display_phone:
                results.append({
                    "number": phone or display_phone,
                    "display_phone": display_phone or phone,
                    "business_name": biz_name,
                    "address": _format_address(biz),
                    "yelp_url": biz.get("url", ""),
                    "source": "yelp_api",
                    "confidence": 88,
                })

            # Get detailed info for the top match
            if biz.get("id") and len(results) == 1:
                detail = _get_business_detail(biz["id"])
                if detail:
                    results[0].update(detail)

    except requests.RequestException as e:
        logger.error(f"Yelp API error: {e}")

    return results


def _get_business_detail(biz_id: str) -> dict | None:
    """Fetch full business details — has more phone info."""
    try:
        resp = requests.get(
            f"{BASE}/businesses/{biz_id}",
            headers=_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "number": data.get("phone", ""),
                "display_phone": data.get("display_phone", ""),
                "website": data.get("website", ""),
                "hours": _format_hours(data.get("hours")),
            }
    except Exception:
        pass
    return None


def _names_overlap(query: str, result: str) -> bool:
    """Check if the query name and result name share significant words."""
    q_words = set(query.lower().split())
    r_words = set(result.lower().split())
    stop = {"the", "a", "an", "and", "&", "ltd", "llc", "inc", "co", "corp", "limited"}
    q_words -= stop
    r_words -= stop
    if not q_words:
        return True
    overlap = q_words & r_words
    return len(overlap) / len(q_words) >= 0.5


def _format_address(biz: dict) -> str:
    loc = biz.get("location", {})
    parts = [
        loc.get("address1", ""),
        loc.get("city", ""),
        loc.get("state", ""),
        loc.get("country", ""),
    ]
    return ", ".join(p for p in parts if p)


def _format_hours(hours_data) -> str | None:
    if not hours_data:
        return None
    try:
        first = hours_data[0] if isinstance(hours_data, list) else hours_data
        is_open = first.get("is_open_now", None)
        if is_open is not None:
            return "Open now" if is_open else "Currently closed"
    except Exception:
        pass
    return None
