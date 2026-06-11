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
