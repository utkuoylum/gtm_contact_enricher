from __future__ import annotations
"""
Gemini-powered initial company research.

Uses Gemini 2.0 Flash with Google Search grounding to discover basic facts
about a company before the main scraping pipeline starts:
  - industry / sector
  - approximate employee count
  - company website (used as domain hint)
  - headquarters location
  - one-line description

The employee count drives the contact search strategy in enricher.py:
  - < LARGE_COMPANY_THRESHOLD  → prefer staffing-relevant titles, fall back to all
  - ≥ LARGE_COMPANY_THRESHOLD  → ONLY staffing-relevant titles (event/HR/recruit/ops)
"""
import json
import logging
import re
import os

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


def gemini_available() -> bool:
    return bool(GEMINI_API_KEY)


def get_company_initial_info(company_name: str, location: str = "") -> dict:
    """
    Search the web via Gemini to discover basic company facts.

    Returns a dict with keys:
      industry, employee_count (int|None), website, location, description
    Returns {} on failure or when Gemini is not configured.
    """
    if not gemini_available():
        return {}

    try:
        from google import genai
        from google.genai.types import Tool, GenerateContentConfig, GoogleSearch
    except ImportError:
        logger.warning("google-genai not installed; skipping Gemini initial search")
        return {}

    loc_hint = f' located in "{location}"' if location else ""
    prompt = (
        f'Search the web for the company "{company_name}"{loc_hint}.\n'
        "Return a JSON object with ONLY these fields:\n"
        '  "industry": string (sector/industry, e.g. "Hospitality", "Staffing", "Retail"),\n'
        '  "employee_count": integer or null'
        ' (best estimate of total headcount — if only a range is known, use the midpoint),\n'
        '  "website": string or null (company homepage URL, full https://... form),\n'
        '  "location": string or null (headquarters city and country),\n'
        '  "description": string (one sentence about what the company does).\n'
        "Respond with ONLY the JSON object. No markdown fences, no explanation."
    )

    # Try models in order of preference. Use stable aliases where possible.
    _MODELS = ["gemini-2.5-flash", "gemini-2.0-flash-001", "gemini-1.5-flash"]
    text = ""
    for model_name in _MODELS:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=GenerateContentConfig(
                    tools=[Tool(google_search=GoogleSearch())],
                    temperature=0.1,
                ),
            )
            text = (response.text or "").strip()
            if text:
                break
        except Exception as e:
            err_str = str(e)
            # Retry with next model on quota, rate-limit, or model-not-found errors
            if any(code in err_str for code in ("429", "404", "RESOURCE_EXHAUSTED", "NOT_FOUND", "no longer available")):
                logger.warning(f"Gemini {model_name} unavailable, trying next: {err_str[:100]}")
                continue
            logger.warning(f"Gemini initial search failed for '{company_name}': {e}")
            return {}

    if not text:
        logger.warning(
            f"Gemini: all models exhausted for '{company_name}'. "
            "Check API key at aistudio.google.com — key should start with 'AIzaSy'."
        )
        return {}

    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    data: dict = {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        if not data:
            logger.warning(f"Gemini response not valid JSON for '{company_name}': {text[:300]}")
            return {}

    result = {
        "industry": _str_or_none(data.get("industry")),
        "employee_count": _int_or_none(data.get("employee_count")),
        "website": _str_or_none(data.get("website")),
        "location": _str_or_none(data.get("location")),
        "description": _str_or_none(data.get("description")),
    }
    logger.info(
        f"Gemini initial info for '{company_name}': "
        f"employees={result['employee_count']}, "
        f"industry={result['industry']}, "
        f"website={result['website']}"
    )
    return result


def _str_or_none(v) -> str | None:
    if not v or not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


def _int_or_none(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        # Strip commas/dots used as thousands separators, take first number
        cleaned = v.replace(",", "").replace(".", "")
        m = re.match(r"(\d+)", cleaned)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None
