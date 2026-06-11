from __future__ import annotations
"""
Claude AI fallback extractor for contact enrichment.

Called ONLY when all regex/heuristic scrapers return 0 contacts from a page.
Claude reads the raw text and extracts structured contact data in one API call.

Cost management:
  - Text is truncated to MAX_CHARS before sending (prevents runaway costs)
  - Only called as a last resort (never as first attempt)
  - Model configurable: Sonnet (default) → Haiku (~8x cheaper) via CLAUDE_MODEL env var
  - Token usage is logged at DEBUG level for monitoring

Typical cost per call:
  - Sonnet:  ~2000 input + ~300 output tokens = ~$0.007
  - Haiku:   ~2000 input + ~300 output tokens = ~$0.0006
"""

import json
import logging
import re
from typing import Optional

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)

# Max characters of page text sent to Claude. ~6000 chars ≈ ~1500 tokens.
# Impressum pages are typically 500-3000 chars; press releases 2000-8000.
MAX_CHARS = 6000

# Claude is only available if API key is set
_CLAUDE_AVAILABLE: bool = bool(ANTHROPIC_API_KEY)

try:
    import anthropic as _anthropic
    _client: Optional[_anthropic.Anthropic] = (
        _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
    )
except ImportError:
    _anthropic = None  # type: ignore[assignment]
    _client = None
    _CLAUDE_AVAILABLE = False


def claude_available() -> bool:
    return _CLAUDE_AVAILABLE and _client is not None


# ── Contact extraction ─────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """\
You are a B2B contact data extractor for a German job placement agency.
Extract ONLY real, named human contacts from the text — people who work at the company.

Return a JSON array. Each object must have:
  full_name  : string  — full name only (e.g. "Michael Wernicke")
  title      : string | null  — job title (e.g. "Geschäftsführer", "HR Manager")
  email      : string | null  — email address if present in text
  phone      : string | null  — phone number if present in text
  source_hint: string  — one word describing where you found it (e.g. "impressum", "pressekontakt", "team_page")

Rules:
- Include Geschäftsführer, Inhaber, Vorstand, Personalleiter, HR roles, Recruiter, Prokurist
- Do NOT include generic contacts like "info@", "kontakt@" without a named person
- Do NOT invent data — only extract what is explicitly stated in the text
- Do NOT include company names, addresses, or department names as contacts
- If no real named contacts exist, return []
- Respond with ONLY the JSON array, no explanation"""

_DOMAIN_SYSTEM = """\
You are a domain resolution assistant. Given a company name and location, select the most likely official company domain from the candidates.

Return ONLY a JSON object: {"domain": "example.de", "confidence": 0.9, "reason": "one sentence"}
Rules:
- Prefer .de for German companies, .at for Austrian, .ch for Swiss
- Prefer the domain that best matches the company name
- Prefer established TLDs over unusual ones
- If none match well, return {"domain": null, "confidence": 0, "reason": "..."}"""


def extract_contacts_from_text(
    text: str,
    company_name: str = "",
    source_hint: str = "unknown",
) -> list[dict]:
    """
    Ask Claude to extract named contacts from page text.

    Used as last-resort fallback when regex extraction returns 0 results.
    Text is truncated to MAX_CHARS to control costs.

    Returns list of contact dicts with keys: full_name, title, email, phone, source.
    """
    if not claude_available():
        return []

    # Truncate to control cost — most relevant info is near the top
    truncated = text[:MAX_CHARS]
    if len(text) > MAX_CHARS:
        truncated += "\n[... text truncated ...]"

    context = f"Company: {company_name}\n\n" if company_name else ""
    user_msg = f"{context}Extract contacts from this page text:\n\n{truncated}"

    try:
        resp = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            system=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        logger.debug(
            f"Claude extract_contacts ({CLAUDE_MODEL}): "
            f"{resp.usage.input_tokens} in / {resp.usage.output_tokens} out tokens"
        )

        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []

        contacts = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = (item.get("full_name") or "").strip()
            if not name or len(name) < 4:
                continue
            contacts.append({
                "full_name": name,
                "title": item.get("title") or None,
                "email": item.get("email") or None,
                "phone": item.get("phone") or None,
                "source": f"claude_{item.get('source_hint', source_hint)}",
                "year_found": None,  # Claude doesn't know publication date
            })
        return contacts

    except json.JSONDecodeError as e:
        logger.debug(f"Claude returned non-JSON: {e}")
    except Exception as e:
        logger.warning(f"Claude API error in extract_contacts: {e}")

    return []


# ── Domain disambiguation ──────────────────────────────────────────────────────

def pick_best_domain(
    company_name: str,
    location: str,
    candidates: list[str],
) -> Optional[str]:
    """
    When multiple domains match a company name, ask Claude to pick the best one.

    Returns the chosen domain string, or None if Claude can't decide.
    Cost: ~200 input + ~50 output tokens (~$0.001 on Sonnet).
    """
    if not claude_available() or not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    candidate_list = "\n".join(f"- {d}" for d in candidates[:10])
    user_msg = (
        f"Company: {company_name}\n"
        f"Location: {location}\n"
        f"Candidate domains:\n{candidate_list}\n\n"
        "Which domain is most likely the official website for this company?"
    )

    try:
        resp = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=128,
            system=_DOMAIN_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        domain = parsed.get("domain")
        if domain and isinstance(domain, str):
            logger.debug(
                f"Claude domain pick: {domain} (confidence={parsed.get('confidence')}) — {parsed.get('reason')}"
            )
            return domain
    except Exception as e:
        logger.debug(f"Claude domain pick error: {e}")

    return None


# ── Impressum smart parse ──────────────────────────────────────────────────────

def parse_impressum_with_claude(html_text: str, company_name: str = "") -> list[dict]:
    """
    Dedicated Impressum parser using Claude.

    German Impressum pages are legally required to list responsible persons
    but use highly varied formats. Claude handles all of them.

    Only called when the regex-based _parse_impressum() returns 0 results.
    """
    return extract_contacts_from_text(html_text, company_name, source_hint="impressum")


# ── SERP domain finder ─────────────────────────────────────────────────────────

_SERP_DOMAIN_SYSTEM = (
    "You are a company domain researcher. Given a company name, location, and search results, "
    "identify the company's PRIMARY contact website for that specific location. "
    "CRITICAL: For companies in Germany/Austria/Switzerland, prefer the LOCAL German site over "
    "global brand sites. Example: for 'Park Plaza Berlin' prefer 'parkplazagermany.com' over "
    "'parkplaza.com'. For 'Hilton Frankfurt' prefer a German/Frankfurt-specific site. "
    "The right domain is where you'd find local contact info, Impressum, and actual local staff — "
    "not the global booking engine. "
    "Return ONLY the bare domain (e.g. 'parkplazagermany.com'). No explanation. If unsure return null."
)

_SERP_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9\-\.]+\.[a-z]{2,}$")


def find_domain_from_serp(
    company_name: str,
    location: str,
    serp_text: str,
) -> Optional[str]:
    """
    Ask Claude to identify the official company domain from SERP text.

    Returns a bare domain like 'parkplaza.com', or None if uncertain.
    Cost: ~1000 input + ~20 output tokens.
    """
    if not claude_available():
        return None

    user_msg = (
        f"Company: {company_name}\nLocation: {location}\n\n"
        f"Search results:\n{serp_text[:4000]}"
    )

    try:
        resp = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=80,
            system=_SERP_DOMAIN_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip().lower()
        logger.debug(
            f"Claude find_domain_from_serp: raw={raw!r} "
            f"({resp.usage.input_tokens} in / {resp.usage.output_tokens} out)"
        )
        # Claude may return "null" / "none" / empty when unsure
        if not raw or raw in ("null", "none", "n/a"):
            return None
        # Strip accidental URL scheme or www prefix
        raw = re.sub(r"^https?://", "", raw).lstrip("www.").rstrip("/")
        if _SERP_DOMAIN_RE.match(raw):
            return raw
    except Exception as e:
        logger.debug(f"Claude find_domain_from_serp error: {e}")

    return None


# ── SERP contact extractor ─────────────────────────────────────────────────────

_SERP_CONTACT_SYSTEM = (
    "You are a contact data extractor for B2B sales intelligence. "
    "From search result snippets, extract names and job titles of people who work at the given company. "
    "Focus on: CEOs, founders, managing directors, HR directors, HR managers, recruiters. "
    "Return a JSON array of objects: [{full_name, title, email}]. "
    "email can be null. "
    "Only include people clearly at this specific company. "
    "Return [] if none found. "
    "Return ONLY valid JSON, no explanation."
)


def extract_contacts_from_serp(
    serp_text: str,
    company_name: str,
    location: str = "",
) -> list[dict]:
    """
    Ask Claude to extract named decision-makers from SERP text.

    Returns a list of contact dicts with keys: full_name, title, email,
    source="claude_serp", year_found=None, phone=None.
    """
    if not claude_available():
        return []

    user_msg = (
        f"Company: {company_name}\nLocation: {location}\n\n"
        f"Search result text:\n{serp_text[:5000]}"
    )

    try:
        resp = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=_SERP_CONTACT_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        logger.debug(
            f"Claude extract_contacts_from_serp: "
            f"{resp.usage.input_tokens} in / {resp.usage.output_tokens} out"
        )

        # Strip markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []

        contacts = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = (item.get("full_name") or "").strip()
            if not name or len(name) < 4:
                continue
            contacts.append({
                "full_name": name,
                "title": item.get("title") or None,
                "email": item.get("email") or None,
                "phone": None,
                "source": "claude_serp",
                "year_found": None,
            })
        return contacts

    except json.JSONDecodeError as e:
        logger.debug(f"Claude extract_contacts_from_serp non-JSON: {e}")
    except Exception as e:
        logger.warning(f"Claude API error in extract_contacts_from_serp: {e}")

    return []


# ── Contact evaluator — confidence scoring + employment confirmation ───────────

_EVALUATE_SYSTEM = """\
You are a B2B contact quality evaluator for a recruitment agency.
Given contacts found for a company, assign each a confidence score and confirm employment.

Confidence score (0–100) — how likely this person currently works at the company:
  85-100: LinkedIn/Xing profile at this company, Impressum, or Handelsregister
  65-84:  Company website team/about page, press article < 1 year ago, named + titled
  40-64:  Google SERP mention with name+title, press article 1-2 years old
  10-39:  Older data, unclear link to company, or title missing
  0:      Not a real human name (navigation items, company names, brand words)

employment_confirmed = true ONLY if source is one of:
  linkedin, xing, website_card, website_schema, impressum, german_register, job_portal, northdata

Rules:
- Score ALL entries — do not remove any
- Contacts with the same person (different spellings) → keep one with higher confidence, set the other to 0
- Keep ALL original fields and ADD two new fields: confidence (int 0-100), employment_confirmed (bool)
- If full_name is not a real human name (navigation item, company name, brand word), set confidence=0
Return ONLY valid JSON array ordered by confidence DESC, no explanation."""


def evaluate_contacts(
    contacts: list[dict],
    company_name: str,
    location: str = "",
    job_category: str = "",
) -> list[dict] | None:
    """
    Score contacts 0-100 for current employment confidence, filter to top 5.

    Adds 'confidence' and 'employment_confirmed' to each contact.
    Returns None on any error (caller should fall back to unscored contacts).
    """
    if not claude_available() or not contacts:
        return None

    user_msg = (
        f"Company: {company_name}\nLocation: {location}\nJob category: {job_category}\n\n"
        f"Contacts to evaluate:\n{json.dumps(contacts[:20], ensure_ascii=False)}"
    )

    try:
        resp = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=_EVALUATE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        logger.debug(
            f"Claude evaluate_contacts: "
            f"{resp.usage.input_tokens} in / {resp.usage.output_tokens} out"
        )
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return None

        result = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            item.setdefault("confidence", 0)
            item.setdefault("employment_confirmed", False)
            result.append(item)

        # Sort by confidence DESC — caller decides the cutoff (max_contacts)
        result.sort(key=lambda x: -x.get("confidence", 0))
        return result

    except json.JSONDecodeError as e:
        logger.debug(f"Claude evaluate_contacts non-JSON: {e}")
    except Exception as e:
        logger.warning(f"Claude API error in evaluate_contacts: {e}")

    return None


# ── Contact synthesizer / quality controller ───────────────────────────────────

_SYNTHESIZE_SYSTEM = (
    "You are a contact data quality controller for B2B sales. "
    "Review this contact list found for a company and remove ALL false positives. "
    "\n\nREMOVE entries where full_name is NOT a real human first+last name, including:\n"
    "- Website navigation items: 'My Account', 'My Reservations', 'Access Restricted'\n"
    "- Loyalty/rewards program names: 'Radisson Rewards', 'Transfer Points', 'Rewards Corporate Program'\n"
    "- Booking interface text: 'Travel Agent', 'Travel Arrenger', 'My Profile'\n"
    "- Brand names or hotel chain names as person names: 'Radisson For You'\n"
    "- Company names, department names, or product names as person names\n"
    "- Any entry where full_name contains a pronoun (My, Your, Our), preposition (For, By), "
    "or generic word (Account, Rewards, Points, Program, Booking, Agent, Restricted)\n"
    "\nKEEP only entries where full_name is unambiguously a real human name (e.g. 'Maria Müller', 'Thomas Weber').\n"
    "NORMALIZE: standardize job titles (keep German if German company). "
    "Return cleaned list as JSON array with SAME fields as input. "
    "Return ONLY valid JSON."
)


def synthesize_contacts(
    contacts: list[dict],
    company_name: str,
    location: str = "",
) -> list[dict]:
    """
    Post-process all found contacts: remove false positives, normalize titles.

    Takes a list of raw contact dicts (up to 25), returns the cleaned list.
    On any error returns the original contacts unchanged.
    """
    if not claude_available() or not contacts:
        return contacts

    user_msg = (
        f"Company: {company_name}\nLocation: {location}\n\n"
        f"Contacts:\n{json.dumps(contacts[:25], ensure_ascii=False)}"
    )

    try:
        resp = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=3000,
            system=_SYNTHESIZE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = resp.content[0].text.strip()
        logger.debug(
            f"Claude synthesize_contacts: "
            f"{resp.usage.input_tokens} in / {resp.usage.output_tokens} out"
        )

        # Strip markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError as e:
        logger.debug(f"Claude synthesize_contacts non-JSON: {e}")
    except Exception as e:
        logger.warning(f"Claude API error in synthesize_contacts: {e}")

    # On any error return original unchanged
    return contacts
